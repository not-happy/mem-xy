"""§5.7 人工裁决状态机 + §4.3.2 的两次向量比对。

```
pending ──人批准──► approved ──排 job──► writing ──成功──► done
                      ▲                    │
                      └────失败可重试────── failed
pending ──人否决──► rejected          （超 expires_at）──► expired
approved ──两次比对命中──► pending（★ 状态图里没画，见 `_bounce`）
```

★ 必须有 `writing`/`failed` 两态：批准后写入失败（LLM 超时）若无这两态，
  这条会**永久静默丢失**。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager

from psycopg import Connection

from ..core import db
from ..core.config import get_settings
from ..core.embedding import get_embedding_service
from ..core.hashing import content_hash
from ..core.redact import redact
from ..prompts import route as route_prompt
from ..queue import jobs
from .facts import _event
from .summarize import enqueue_summarize

log = logging.getLogger(__name__)


@contextmanager
def _tx(conn: Connection | None):
    """批准 + 排 job + 回写 route_decisions 必须同生同死，否则会出现
    "状态是 approved 但没有 job" 这种永远不会被处理的遗留单。"""
    if conn is not None:
        yield conn
    else:
        with db.transaction() as own:
            yield own


def approve(pending_id: int, resolution: dict | None = None, *,
            resolved_by: str = "human", conn: Connection | None = None) -> int | None:
    """人点了"通过"。返回排出去的 job id；抢不到（已被别人处理）返回 None。

    `resolution` 是人在卡片上改过的最终结果——`kind` 可能从预选的 `observation`
    被改回 `atomic_fact`（§5.4「一眼就能改回」说的就是这个）。
    """
    resolution = resolution or {}
    with _tx(conn) as tx:
        # 抢占式批准：影响行数 = 1 才算抢到。两个 UI 标签页同时点"通过"只有一个能成。
        row = tx.execute(
            "UPDATE pending_items SET status='approved', resolution=%s::jsonb,"
            "       resolved_by=%s, resolved_at=now()"
            " WHERE id=%s AND status='pending' RETURNING *",
            (json.dumps(resolution, ensure_ascii=False), resolved_by, pending_id),
        ).fetchone()
        if row is None:
            return None
        job_id = jobs.enqueue("apply_decision",
                              {"pending_id": pending_id, "resolved_by": resolved_by,
                               "resolution": resolution},
                              org_key=row["org_key"], dedup_key=f"pending:{pending_id}",
                              conn=tx)
        _record_adjudication(tx, row, resolution, final_kind=resolution.get("kind"))
    return job_id


def reject(pending_id: int, *, note: str | None = None, resolved_by: str = "human",
           conn: Connection | None = None) -> bool:
    """人点了"否决"。不写任何记忆——第一道审核否决只意味着"这不是规矩线索"。

    ★ 与 §5.7 的 `doc_proposals.rejected` 不同：那边要额外写
    `raw_observations(polarity='negative')`，因为那是"成文规范被否决"，
    是系统该记住的边界；这边只是分类判错，记下来反而会污染否决记录。
    """
    with _tx(conn) as tx:
        row = tx.execute(
            "UPDATE pending_items SET status='rejected', resolved_by=%s, resolved_at=now(),"
            "       resolution=%s::jsonb WHERE id=%s AND status='pending' RETURNING *",
            (resolved_by, json.dumps({"note": note} if note else {}, ensure_ascii=False),
             pending_id),
        ).fetchone()
        if row is None:
            return False
        _record_adjudication(tx, row, {"note": note}, final_kind="discard")
    return True


def _record_adjudication(conn: Connection, row: dict, resolution: dict, *,
                         final_kind: str) -> None:
    """★ 每次人工裁决都要回写 `route_decisions`（§5.7）——那是 §2.3 改判率的原始数据，
    也是 改进 few-shot 的素材。少写一行，改判率就永远算不出来。"""
    suggestion = _as_dict(row["suggestion"])
    decision_id = suggestion.get("decision_id")
    if not decision_id:
        return
    final_kind = final_kind or suggestion.get("kind")
    conn.execute(
        "UPDATE route_decisions SET human_output=%s::jsonb, overridden=%s WHERE id=%s",
        (json.dumps(resolution, ensure_ascii=False),
         final_kind != suggestion.get("model_kind"), decision_id))


def apply_decision(payload: dict) -> dict:
    """worker handler：把批准后的裁决真正落库。

    ★ **自管事务，不用 worker 递进来的连接**（和 `facts.write_facts` 同一个理由）：
      写入失败时必须先把 `status='failed'` 落下来**再**抛错。要是失败标记和写入
      共用一个事务，抛错会把它一起回滚，结果仍是"静默丢失"——正是 §5.7
      加 `writing`/`failed` 两态要防的那件事。
    """
    pending_id = payload["pending_id"]
    row = _claim_for_writing(pending_id)
    if row is None:
        return {"action": "skipped"}

    merged = {**_as_dict(row["suggestion"]), **(payload.get("resolution") or {})}
    kind = merged.get("kind") or "observation"

    # ★ 向量在事务**外**算（library.py / proposals.py 的同一条纪律，EXPERT-REVIEW B2）：
    #   模型冷时 encode_one 要 15–33s，关进事务就是 idle-in-transaction 占着连接。
    vectors = _prepare_vectors(row, merged) if kind == "observation" else None

    # ★ B4-②：merge_pair 卡上人改过合并文案（不等于 A/B 任一原文）就要重算向量，
    #   同样在事务外。原样批准（文案 = content_a/content_b）时零 encode。
    new_embedding = None
    if kind == "merge_pair":
        new_content = merged.get("content")
        if new_content and new_content not in (merged.get("content_a"),
                                               merged.get("content_b")):
            new_embedding = get_embedding_service().encode_one(new_content)

    try:
        with db.transaction() as tx:
            if kind == "atomic_fact":
                outcome = _hand_to_writer(tx, row, merged)
            elif kind == "discard":
                outcome = {"action": "discarded_by_human"}
            elif kind == "merge_pair":
                outcome = _merge_pair(tx, row, merged, new_embedding)
            else:
                outcome = _write_observation(tx, row, merged, vectors)
            # ★ 退回人工的那几条**不能**标 done——`_bounce` 刚把状态写回 `pending`，
            #   这里再盖一层 done 就等于"系统悄悄丢弃"，而丢弃是不可逆的。
            if outcome.get("action") != "bounced":
                tx.execute("UPDATE pending_items SET status='done', resolution=%s::jsonb"
                           " WHERE id=%s",
                           (json.dumps(outcome, ensure_ascii=False), pending_id))
    except Exception as exc:  # noqa: BLE001 — 落到 failed，绝不能静默丢（§5.7）
        db.execute("UPDATE pending_items SET status='failed', last_error=%s WHERE id=%s",
                   (redact(f"{type(exc).__name__}: {exc}")[:2000], pending_id))
        raise
    return outcome


def _claim_for_writing(pending_id: int) -> dict | None:
    """approved → writing。返回 None 表示这一单不该由我处理（重放 / 状态不对）。

    `writing` 必须在**独立事务**里提交：进程在这一步之后崩掉，行会停在 `writing`，
    而 `writing` 是可重入的状态，job 重试能接着往下走。
    """
    with db.transaction() as tx:
        row = tx.execute("SELECT * FROM pending_items WHERE id=%s FOR UPDATE",
                         (pending_id,)).fetchone()
        if row is None:
            log.warning("pending_items#%s 不存在，跳过", pending_id)
            return None
        if row["status"] == "done":
            return None  # job 重放
        if row["status"] not in ("approved", "writing", "failed"):
            log.warning("pending_items#%s 状态是 %s，不能写入", pending_id, row["status"])
            return None
        tx.execute("UPDATE pending_items SET status='writing' WHERE id=%s", (pending_id,))
    return row


def _hand_to_writer(conn: Connection, row: dict, merged: dict) -> dict:
    """人把预选的 observation 改回 atomic_fact ⇒ 交给四动作写入器，不自己写 memories。"""
    if not merged.get("content"):
        _bounce(conn, row["id"], "no_content")
        return {"action": "bounced", "reason": "no_content"}
    # ★ B7（W8）：scope=project 但这张卡片没有 project_key 时，write_facts 会把它
    #   静默跳过（facts._facts_from 的防线拦整批 CHECK 冲突），卡片显示"完成"、
    #   实际没入库——正是要消灭的静默丢弃。这里提前退回人工：要么先补归属
    #   （归属页定 cwd 的 project_key），要么把 scope 改成 user/org 级再批。
    scope = merged.get("scope") or "project"
    if scope == "project" and not row["project_key"]:
        _bounce(conn, row["id"], "no_project_key",
                {"content": merged["content"], "scope": scope,
                 "hint": "先在归属页给这个目录定 project_key，或把 scope 改成 user/org"})
        return {"action": "bounced", "reason": "no_project_key"}
    job_id = jobs.enqueue(
        "write_facts",
        {"session_id": f"pending:{row['id']}", "source": "human-review",
         "prompt_ver": row["prompt_ver"] or route_prompt.PROMPT_VER,
         "org_key": row["org_key"], "project_key": row["project_key"],
         "items": [{"source_msg": row["source_msg"], "content": merged["content"],
                    "scope": merged.get("scope") or "project",
                    "subject": merged.get("subject") or "project",
                    "role": merged.get("role") or "fact",
                    "keywords": merged.get("keywords") or [],
                    "why": merged.get("why")}]},
        org_key=row["org_key"], dedup_key=f"write_facts:pending:{row['id']}", conn=conn)
    return {"action": "handed_to_write_facts", "job_id": job_id}


def _merge_pair(conn: Connection, row: dict, merged: dict,
                new_embedding: list[float] | None) -> dict:
    """★ B4-②：近重复合并裁决的落库分支（卡由 app/dedup_scan.py 发）。

    默认保留 id 较小的一条（老记忆带得动 mention 历史），软删另一条——与
    facts.DELETE 同款软删（status='deleted'，内容留在 memory_events 可追溯）。
    人改过合并文案时（resolution.content ≠ A/B 原文），保留行的 content /
    content_hash / keywords（两行并集，W6 教训：不许全量替换成单行词表）/
    mention_count（两行提及合并）一起更新，向量用事务外预算的 new_embedding。
    """
    ids = merged.get("memory_ids") or []
    if len(ids) != 2 or ids[0] == ids[1]:
        _bounce(conn, row["id"], "bad_merge_pair", {"memory_ids": ids})
        return {"action": "bounced", "reason": "bad_merge_pair"}
    keep_id, drop_id = sorted(int(i) for i in ids)

    # FOR UPDATE 锁两行：并发 write_facts 正好在碰它们时不能插队。
    keeper = conn.execute("SELECT * FROM memories WHERE id=%s FOR UPDATE",
                          (keep_id,)).fetchone()
    driver = conn.execute("SELECT * FROM memories WHERE id=%s FOR UPDATE",
                          (drop_id,)).fetchone()
    if (keeper is None or driver is None
            or keeper["status"] != "active" or driver["status"] != "active"):
        # 扫描之后这对已经被动过（多半被另一张卡合并掉了）→ 退回人工，
        # 卡上还能看到当前状态，别替人做"丢了就算了"的决定。
        _bounce(conn, row["id"], "pair_not_active",
                {"keep_id": keep_id, "drop_id": drop_id,
                 "kept_status": keeper["status"] if keeper else None,
                 "dropped_status": driver["status"] if driver else None})
        return {"action": "bounced", "reason": "pair_not_active"}

    new_content = merged.get("content") or keeper["content"]
    content_changed = new_content != keeper["content"]
    if content_changed:
        conn.execute(
            "UPDATE memories SET content=%s, content_hash=%s, embedding=%s,"
            "                    keywords=%s, revision_count=revision_count+1,"
            "                    mention_count=mention_count+%s,"
            "                    updated_at=now(), last_seen_at=now()"
            " WHERE id=%s",
            (new_content, content_hash(new_content), new_embedding,
             sorted(set((keeper["keywords"] or []) + (driver["keywords"] or []))),
             driver["mention_count"] or 0, keep_id))
        _event(conn, keep_id, "UPDATE", keeper["content"], new_content,
               keeper["source_msg"])
    else:
        # 原样合并：文案不动，只把另一条的提及与热度并过来。
        conn.execute(
            "UPDATE memories SET mention_count=mention_count+%s,"
            "                    last_seen_at=now(), updated_at=now()"
            " WHERE id=%s",
            (driver["mention_count"] or 0, keep_id))
        _event(conn, keep_id, "NOOP", None, None, keeper["source_msg"])

    deleted = conn.execute(
        "UPDATE memories SET status='deleted', updated_at=now()"
        " WHERE id=%s AND status='active'", (drop_id,))
    if deleted.rowcount == 0:
        # 上面的 FOR UPDATE 已经验过 active，走到这里说明有并发改动了状态；
        # 事务会带着保留行的更新一起提交，但要在结果里如实说明。
        log.warning("merge_pair：drop_id=%s 在软删时已不是 active（并发改动）", drop_id)
    _event(conn, drop_id, "DELETE", driver["content"], None, driver["source_msg"])
    return {"action": "merged", "kept_id": keep_id, "deleted_id": drop_id,
            "content_updated": content_changed}


def _prepare_vectors(row: dict, merged: dict) -> dict | None:
    """§4.3.2 比对① 要用的向量与 living_doc 行，全部在**事务外**准备好。

    返回 None 表示不必准备（content/section 缺失，事务内会走 `_bounce` 退回人工）。
    living_doc 的读取也挪到事务外：两次比对本来就"只是预筛，不是判决"，
    单机单用户下前后几秒的读取时差不影响结论。
    """
    content = merged.get("content")
    section_key = merged.get("section_key")
    if not content or not section_key:
        return None
    service = get_embedding_service()
    vectors: dict = {"content": service.encode_one(content)}
    doc = db.query_one(
        "SELECT id, content FROM living_doc"
        " WHERE org_key = %(org)s AND section_key = %(sec)s"
        "   AND project_key IS NOT DISTINCT FROM %(proj)s",
        {"org": row["org_key"], "sec": section_key, "proj": row["project_key"]})
    if doc:
        vectors["doc"] = {"row": doc, "embedding": service.encode_one(doc["content"])}
    return vectors


def _write_observation(conn: Connection, row: dict, merged: dict,
                       vectors: dict | None = None) -> dict:
    """§4.3.2 的两次比对都在落库之前做。★ 只查库，不读任何文件。"""
    if not merged.get("content"):
        # 这张卡片多半来自 llm_parse_failed，人还没把内容补上
        _bounce(conn, row["id"], "no_content")
        return {"action": "bounced", "reason": "no_content"}
    section_key = merged.get("section_key")
    if not section_key:
        # 栏位没定下来就落库，§4.3.2 的两次比对会在错误分区里做（#50）。退回人工。
        _bounce(conn, row["id"], "section_unmatched")
        return {"action": "bounced", "reason": "section_unmatched"}

    content = merged["content"]
    if vectors is None:
        # 兜底：调用方没预计算（正常路径 apply_decision 在事务外算好传进来）
        vectors = _prepare_vectors(row, merged) or {}
    embedding = vectors.get("content") or get_embedding_service().encode_one(content)
    blocked = _blocked_by(conn, embedding=embedding, doc_info=vectors.get("doc"),
                          org_key=row["org_key"], section_key=section_key)
    if blocked:
        _bounce(conn, row["id"], blocked["reason"], blocked)
        return {"action": "bounced", **blocked}

    inserted = conn.execute(
        """
        INSERT INTO raw_observations (org_key, project_key, section_key, content,
                                      content_hash, polarity, keywords, embedding,
                                      source_msg, prompt_ver)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_msg, content_hash) DO NOTHING
        RETURNING id
        """,
        (row["org_key"], row["project_key"], section_key, content, content_hash(content),
         # ★ 恒为 positive：`negative` 是人工否决记录的专用记号，比对② 就查它。
         #   路由出来的线索哪怕是"系统不支持婴儿票"这种否定句，也是 positive——
         #   否则它会反过来把以后所有同类线索挡成"你以前否决过"（真跑踩过）。
         "positive", merged.get("keywords") or [], embedding,
         row["source_msg"], row["prompt_ver"] or route_prompt.PROMPT_VER),
    ).fetchone()
    if inserted is None:
        return {"action": "already_recorded"}   # UNIQUE(source_msg, content_hash) 兜住重放
    # ★ 攒够即汇总（§4.3）：线索落库成功就在同一事务里排 summarize job，同生同死。
    #   job 内部自判 trigger_new/trigger_mature，不够数便宜返回、不调 LLM。
    enqueue_summarize(org_key=row["org_key"], project_key=row["project_key"],
                      section_key=section_key, conn=conn)
    return {"action": "recorded", "observation_id": inserted["id"], "section_key": section_key}


def _blocked_by(conn: Connection, *, embedding: list[float], doc_info: dict | None,
                org_key: str | None, section_key: str) -> dict | None:
    """① vs `living_doc` 同 section 的现有内容 → 已有，不必再提
       ② vs `raw_observations(polarity='negative')` → 你已经说过不要了

    ★ **阈值 0.72 只是预筛，不是判决**。2026-10-23 实测（bge-m3，中文短句）：
      同一条规矩的两种说法 0.767–0.888，"补充了细节的超集" 0.802–0.875，
      不相干的 0.331–0.459。相关/不相干之间有很宽的空档，
      但**"重复"与"超集"完全重叠**——余弦分不开这两者。
      所以命中之后不自动 NOOP/discard，而是退回人工看一眼。
      误挡的代价是多审一条；自动丢弃的代价是那条规矩永远不会被注入，且没有指标能发现。

    ★ `doc_info` 是 `_prepare_vectors` 在**事务外**查好行、算好向量的 living_doc
      （EXPERT-REVIEW B2：冷模型 encode 15–33s，不能关在事务里）。
      living_doc **没有 embedding 列**（成文规范是短文本，一个分区只有一行，
      现算比维护一列向量便宜，也不会出现"改了 content 忘了改向量"的漂移）。
    """
    floor = get_settings().dedup_prefilter

    if doc_info:
        score = _cosine(embedding, doc_info["embedding"])
        if score >= floor:
            return {"reason": "already_in_living_doc",
                    "matched_id": doc_info["row"]["id"],
                    "matched_content": doc_info["row"]["content"],
                    "score": round(score, 4)}

    db.enable_iterative_scan(conn)
    negative = conn.execute(
        "SELECT id, content, 1 - (embedding <=> %(v)s::vector) AS score"
        " FROM raw_observations"
        " WHERE polarity = 'negative' AND embedding IS NOT NULL"
        "   AND org_key = %(org)s AND section_key = %(sec)s"
        " ORDER BY embedding <=> %(v)s::vector LIMIT 1",
        {"v": embedding, "org": org_key, "sec": section_key},
    ).fetchone()
    if negative and negative["score"] >= floor:
        return {"reason": "rejected_before", "matched_id": negative["id"],
                "matched_content": negative["content"], "score": round(negative["score"], 4)}
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    """`encode()` 出来的是 L2 归一化向量，所以点积就是余弦。"""
    return float(sum(x * y for x, y in zip(a, b)))


def _bounce(conn: Connection, pending_id: int, reason: str, detail: dict | None = None) -> None:
    """退回人工。★ 这是 §5.7 状态图里没画的一条边（approved → pending），
    但不画它就会变成"系统悄悄丢弃"，而丢弃是不可逆的。"""
    conn.execute(
        "UPDATE pending_items SET status='pending', resolved_at=NULL, resolved_by=NULL,"
        " reason=%s, resolution=%s::jsonb WHERE id=%s",
        (reason, json.dumps(detail or {}, ensure_ascii=False), pending_id))


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else json.loads(value or "{}")


def override_stats(conn: Connection | None = None) -> list[dict]:
    """★ P2 验收项：改判率**必须按 kind 分开统计**（§2.3）。
    合在一起看会被数量最多的那一类带着走，而最危险的正是"高置信却判错"。"""
    return _query_all(
        "SELECT prompt_ver, kind,"
        "       count(*)::int AS judged,"
        # ★ FILTER 必须写在 `::int` **前面**：`count(*)::int FILTER (...)` 是语法错误，
        #   转型绑定得比 FILTER 紧。
        "       count(*) FILTER (WHERE human_output IS NOT NULL)::int AS reviewed,"
        "       count(*) FILTER (WHERE overridden)::int AS overridden,"
        "       round(avg(confidence), 3) AS avg_confidence,"
        "       round(avg(confidence) FILTER (WHERE overridden), 3) AS avg_conf_when_wrong"
        "  FROM route_decisions GROUP BY prompt_ver, kind ORDER BY prompt_ver, kind", conn)


def queue_stats(org_key: str | None = None, conn: Connection | None = None) -> dict:
    """status/stage → 数量。

    ★ 传 `org_key` 才和 `list_pending` 的 `total`/`items` 同口径——Inbox 首页的红点
      用这个分组计数，不带 org 过滤时会出现「首页看得见、列表看不见」（分区选 dida，
      数字却是全库的）。缺省全库聚合留给 MCP / CLI 这类无分区概念的调用方。
    """
    sql = ("SELECT status, stage, count(*)::int AS n FROM pending_items"
           "{where} GROUP BY status, stage ORDER BY status, stage")
    params = None
    if org_key:
        sql = sql.format(where=" WHERE org_key = %(org)s")
        params = {"org": org_key}
    else:
        sql = sql.format(where="")
    rows = _query_all(sql, conn, params)
    return {f"{r['status']}/{r['stage']}": r["n"] for r in rows}


_PENDING_COLUMNS = ("id, stage, status, reason, org_key, project_key, raw_content,"
                    " suggestion, resolution, prompt_ver, last_error, created_at")


def list_pending(*, stage: str | None = None, status: str | None = None,
                 org_key: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """待办列表（`GET /v1/pending` 与 MCP `memory_pending` 共用）。

    ★ 默认只看 `pending` + `failed`——`failed` 是"批过了但落库炸了"，它比 pending 更该被看见，
      而 `queue_stats` 的分组计数看不出这一点。
    """
    where: list[str] = []
    params: dict = {"limit": limit, "offset": offset}
    if status:
        where.append("status = %(status)s")
        params["status"] = status
    else:
        where.append("status IN ('pending','failed')")
    if stage:
        where.append("stage = %(stage)s")
        params["stage"] = stage
    if org_key:
        where.append("org_key = %(org)s")
        params["org"] = org_key
    clause = " WHERE " + " AND ".join(where)
    total = db.query_one(f"SELECT count(*) AS n FROM pending_items{clause}", params)["n"]
    rows = db.query(f"SELECT {_PENDING_COLUMNS} FROM pending_items{clause}"
                    " ORDER BY id LIMIT %(limit)s OFFSET %(offset)s", params)
    return {"total": total, "limit": limit, "offset": offset,
            "by_status": queue_stats(org_key), "items": rows}


def _query_all(sql: str, conn: Connection | None, params: dict | None = None) -> list[dict]:
    if conn is not None:
        return conn.execute(sql, params).fetchall()
    return db.query(sql, params)
