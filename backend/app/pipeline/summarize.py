"""汇总器（§5.6）：把观察池里攒够的线索合成一版规范草稿，交人审。

```
org×project×section 的未消费 positive 线索数达阈值
  → 拉线索 + 当前 living_doc 同行 + 该 section 的否决清单
  → 一次 LLM 合成新 draft
  → doc_proposals(pending, source_ids=线索ids)，线索标 consumed
```

★ 和 §5.5 的四动作写入器同一套纪律：**读一个短事务、LLM 夹在中间、写另一个短事务**，
  绝不把 LLM 关在连接/事务里。
★ 触发阈值方案原文写的是"未消费数 / 现有内容量 ≥ 阈值"这个**比例**，但"现有内容量"
  没有单位、无法实现。按绝对条数落地：无 living_doc 行的新 section 用 `trigger_new`(3)，
  已有行（draft）的成熟 section 用 `trigger_mature`(20)。偏差记进 decisions-log #84。
★ `confirmed`/`pinned` 的 living_doc **在任何 LLM 调用之前**就跳过——那是人工钉死的，
  汇总器不许改（§5.6）。新线索继续攒着，等人工降档再说。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..core import db
from ..core.config import get_settings
from ..core.llm import LLMParseError, get_llm_client
from ..prompts import summarize as summarize_prompt

log = logging.getLogger(__name__)

# 一个 section 一次合成喂给 LLM 的线索上限：成熟 section 可能攒很多，超出部分留给下一轮，
# 免得 prompt 撑爆、也免得一次改动太大不好审。
MAX_OBSERVATIONS_PER_CALL = 60

_GROUP_SQL = """
SELECT org_key, project_key, section_key, count(*)::int AS n
  FROM raw_observations
 WHERE consumed = FALSE AND polarity = 'positive'
   AND (%(org)s::text  IS NULL OR org_key = %(org)s)
   AND (%(proj)s::text IS NULL OR project_key IS NOT DISTINCT FROM %(proj)s)
   AND (%(sec)s::text  IS NULL OR section_key = %(sec)s)
 GROUP BY org_key, project_key, section_key
 ORDER BY n DESC
"""

_OBS_SQL = """
SELECT id, content, keywords
  FROM raw_observations
 WHERE consumed = FALSE AND polarity = 'positive'
   AND org_key = %(org)s AND section_key = %(sec)s
   AND project_key IS NOT DISTINCT FROM %(proj)s
 ORDER BY id
 LIMIT %(limit)s
"""

_DOC_SQL = """
SELECT id, content, authority
  FROM living_doc
 WHERE org_key = %(org)s AND section_key = %(sec)s
   AND project_key IS NOT DISTINCT FROM %(proj)s
"""

_NEG_SQL = """
SELECT content
  FROM raw_observations
 WHERE polarity = 'negative'
   AND org_key = %(org)s AND section_key = %(sec)s
   AND project_key IS NOT DISTINCT FROM %(proj)s
 ORDER BY id DESC LIMIT 30
"""

_PENDING_PROPOSAL_SQL = """
SELECT id FROM doc_proposals
 WHERE org_key = %(org)s AND section_key = %(sec)s
   AND project_key IS NOT DISTINCT FROM %(proj)s AND status = 'pending'
 LIMIT 1
"""

_INSERT_PROPOSAL = """
INSERT INTO doc_proposals (org_key, project_key, section_key, old_content,
                           new_content, status, source_ids)
VALUES (%(org)s, %(proj)s, %(sec)s, %(old)s, %(new)s, 'pending', %(src)s)
RETURNING id
"""


@dataclass
class SummarizeStats:
    scanned: int = 0
    proposed: int = 0
    empty: int = 0
    skipped: int = 0
    locked: int = 0
    parse_failed: int = 0
    failed: int = 0        # ★ A10：分区级异常（LLM/网络等），记数后继续跑剩余分区
    calls: int = 0
    seconds: float = 0.0
    partitions: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {**self.__dict__}


def candidate_sections(*, org_key: str | None = None, project_key: str | None = None,
                       section_key: str | None = None) -> list[dict]:
    """有未消费 positive 线索的 (org, project, section) 分区，带条数。"""
    return db.query(_GROUP_SQL, {"org": org_key, "proj": project_key, "sec": section_key})


def run(*, org_key: str | None = None, project_key: str | None = None,
        section_key: str | None = None, force: bool = False, llm=None) -> SummarizeStats:
    """扫一遍候选分区，逐个合成。给了 `section_key` 就只处理那一个（UI 手动触发走这条）。

    ★ 阈值判定、锁档判定、pending 去重**只在 `summarize_partition` 里做一遍**——
      手动触发和自动扫描必须走同一条决策路径，否则"手动跑没反应、自动跑却有"这种
      不一致会很难查。这里只负责枚举候选。
    """
    stats = SummarizeStats()
    candidates = candidate_sections(org_key=org_key, project_key=project_key,
                                    section_key=section_key)
    for part in candidates:
        stats.scanned += 1
        # ★ force 只看显式参数，不从 section_key 推断：自动触发（§4.3，线索落库后
        #   排 job）同样带 section_key，推断会让每条线索都强制调 LLM 合成，阈值形同虚设。
        #   手动"强制汇总"由 UI 按钮显式传 force=true（"按阈值汇总"传 false）。
        # ★ A10：单分区炸掉（LLM 网络错、DB 抖动等）不再掀翻整轮——记数后继续
        #   跑剩余分区。分区被炸掉时线索不消费（consume 只在 summarize_partition
        #   的写事务里做），下轮自然会重试同一个分区。
        try:
            result = summarize_partition(
                part["org_key"], part["project_key"], part["section_key"],
                force=force, llm=llm)
        except Exception:  # noqa: BLE001 — 分区容错是本层的职责，异常必须吞在这里
            log.exception("分区汇总失败（跳过，下轮重试）：org=%s project=%s section=%s",
                          part["org_key"], part["project_key"], part["section_key"])
            stats.failed += 1
            continue
        stats.partitions.append(result)
        action = result.get("action")
        if action == "proposed":
            stats.proposed += 1
        elif action == "empty":
            stats.empty += 1
        elif action == "locked":
            stats.locked += 1
        else:
            stats.skipped += 1
        stats.calls += result.get("calls", 0)
        stats.seconds += result.get("seconds", 0.0)
        stats.parse_failed += result.get("parse_failed", 0)
    return stats


def summarize_partition(org_key: str, project_key: str | None, section_key: str, *,
                        force: bool = False, llm=None) -> dict:
    """合成一个分区。返回一个描述结果的 dict（供 run 汇总、也供测试断言）。"""
    settings = get_settings()
    obs = db.query(_OBS_SQL, {"org": org_key, "proj": project_key, "sec": section_key,
                              "limit": MAX_OBSERVATIONS_PER_CALL})
    if not obs:
        return {"action": "no_observations", "section_key": section_key}

    doc = db.query_one(_DOC_SQL, {"org": org_key, "proj": project_key, "sec": section_key})
    if doc and doc["authority"] in ("confirmed", "pinned"):
        # 人工钉死/确认过：汇总器不许改，线索留着不消费（§5.6）。LLM 都不必调。
        return {"action": "locked", "section_key": section_key,
                "authority": doc["authority"], "count": len(obs)}

    threshold = settings.summarize_trigger_mature if doc else settings.summarize_trigger_new
    if not force and len(obs) < threshold:
        return {"action": "below_threshold", "section_key": section_key,
                "count": len(obs), "threshold": threshold}

    pending = db.query_one(_PENDING_PROPOSAL_SQL,
                           {"org": org_key, "proj": project_key, "sec": section_key})
    if pending:
        # 已经有一版等人审了：不再叠一版（叠了人要在两个可能冲突的草稿里挑），
        # 也不消费线索——等这版被裁决掉，下一轮再合。省一次 LLM 调用。
        return {"action": "already_pending", "section_key": section_key,
                "proposal_id": pending["id"], "count": len(obs)}

    negatives = db.query(_NEG_SQL, {"org": org_key, "proj": project_key, "sec": section_key})

    client = llm or get_llm_client()
    started = time.perf_counter()
    parse_failed = 0
    try:
        result = client.structured(
            "summarize",
            system=summarize_prompt.SYSTEM,
            messages=[{"role": "user",
                       "content": _user_message(obs, doc, negatives)}],
            tool=summarize_prompt.TOOL,
            schema=summarize_prompt.SummarizeResult,
        )
    except LLMParseError as exc:
        # 合成不出就整批放弃、线索原样留着：下一轮还能再试，比强行落一版垃圾草稿好。
        parse_failed = 1
        log.error("汇总 %s/%s/%s 输出不合 schema，本轮跳过：%s",
                  org_key, project_key, section_key, exc)
        return {"action": "parse_failed", "section_key": section_key,
                "calls": 1, "parse_failed": 1,
                "seconds": time.perf_counter() - started}
    seconds = time.perf_counter() - started
    content = (result.content or "").strip()
    ids = [row["id"] for row in obs]

    # 写：消费线索 + 落提案，同生同死。★ 只消费这一轮真正喂进去的 ids，
    #   LLM 调用期间新到的线索不会被误标 consumed。
    with db.transaction() as tx:
        tx.execute("UPDATE raw_observations SET consumed = TRUE, consumed_at = now()"
                   " WHERE id = ANY(%s)", (ids,))
        proposal_id = None
        if content:
            proposal_id = tx.execute(_INSERT_PROPOSAL, {
                "org": org_key, "proj": project_key, "sec": section_key,
                "old": doc["content"] if doc else None, "new": content, "src": ids,
            }).fetchone()["id"]

    if not content:
        # LLM 判定"没有可写的"（全噪声 / 已被草稿覆盖 / 都撞否决清单）。
        # 线索照样消费掉——它们已经被评估过了，留着只会每轮重复触发、白烧 LLM。
        return {"action": "empty", "section_key": section_key, "consumed": len(ids),
                "reason": result.reason, "calls": 1, "parse_failed": parse_failed,
                "seconds": seconds}
    return {"action": "proposed", "section_key": section_key, "proposal_id": proposal_id,
            "consumed": len(ids), "reason": result.reason, "calls": 1,
            "parse_failed": parse_failed, "seconds": seconds}


def _user_message(obs: list[dict], doc: dict | None, negatives: list[dict]) -> str:
    lines = [f"- [{row['id']}] {row['content']}"
             + (f"（关键词：{'、'.join(row['keywords'])}）" if row.get("keywords") else "")
             for row in obs]
    if doc:
        head = f"【当前草稿】（authority={doc['authority']}，要在它基础上吸收新线索）\n{doc['content']}"
    else:
        head = "【当前草稿】\n（这个栏目还没有规范，是第一次汇总）"
    veto = ("【已否决，不要再写进来】\n" + "\n".join(f"- {r['content']}" for r in negatives)
            if negatives else "【已否决】\n（无）")
    return f"{head}\n\n【攒着的线索】\n" + "\n".join(lines) + f"\n\n{veto}"


# ─────────────────────────── 观察池：给 UI 的只读 + 撤销否决 ───────────────────────────

_OBS_LIST_COLUMNS = ("o.id, o.org_key, o.project_key, o.section_key, o.content, o.polarity,"
                     " o.keywords, o.consumed, o.consumed_at, o.source_msg, o.created_at,"
                     " s.label")


def list_observations(*, org_key: str | None = None, project_key: str | None = None,
                      section_key: str | None = None, polarity: str | None = None,
                      consumed: bool | None = None, limit: int = 50,
                      offset: int = 0) -> dict:
    """线索池（`GET /v1/observations`）。`polarity='negative'` 就是否决记录——UI 要能查它、
    撤销它（§4.3.2）。★ 不返回 embedding 列：那是 1024 维向量，进 JSON 又没用。"""
    where: list[str] = ["TRUE"]
    params: dict = {"limit": limit, "offset": offset}
    if org_key:
        where.append("o.org_key = %(org)s")
        params["org"] = org_key
    if project_key:
        where.append("o.project_key IS NOT DISTINCT FROM %(proj)s")
        params["proj"] = project_key
    if section_key:
        where.append("o.section_key = %(sec)s")
        params["sec"] = section_key
    if polarity:
        where.append("o.polarity = %(pol)s")
        params["pol"] = polarity
    if consumed is not None:
        where.append("o.consumed = %(consumed)s")
        params["consumed"] = consumed
    clause = " WHERE " + " AND ".join(where)
    total = db.query_one(
        f"SELECT count(*) AS n FROM raw_observations o JOIN doc_sections s USING (section_key)"
        f"{clause}", params)["n"]
    rows = db.query(
        f"SELECT {_OBS_LIST_COLUMNS} FROM raw_observations o"
        f" JOIN doc_sections s USING (section_key)"
        f"{clause} ORDER BY o.id DESC LIMIT %(limit)s OFFSET %(offset)s", params)
    return {"total": total, "limit": limit, "offset": offset, "items": rows}


def revoke_negative(observation_id: int) -> dict:
    """撤销一条否决（`DELETE /v1/observations/{id}/negative`）＝删掉那条 negative 行。

    ★ **只删 negative**：positive 是汇总器的素材，删了等于丢线索；而这个端点的语义是
      "我当初否决错了，把它从否决记录拿出来"。删的是错的 polarity 就得挡下来（400）。
      删除而非软删——否决记录靠 `embedding <=> ` 做比对②，留一条 tombstone 反而会被检索到。
    """
    from ..library import LibraryError, not_found

    with db.transaction() as tx:
        row = tx.execute("SELECT id, polarity FROM raw_observations WHERE id=%s FOR UPDATE",
                         (observation_id,)).fetchone()
        if row is None:
            raise not_found("观察", observation_id)
        if row["polarity"] != "negative":
            raise LibraryError(400, "not_a_veto",
                               f"观察 {observation_id} 是 {row['polarity']}，不是否决项，不能这样撤销")
        tx.execute("DELETE FROM raw_observations WHERE id=%s", (observation_id,))
    return {"action": "revoked", "observation_id": observation_id}


def enqueue_summarize(*, org_key: str | None = None, project_key: str | None = None,
                      section_key: str | None = None, force: bool = False,
                      conn: "Connection | None" = None) -> int | None:
    """排一个 `summarize` job。两个调用方：
    ① 手动触发（`POST /v1/observations/summarize`）；
    ② 线索落库后自动触发（`review._write_observation`，攒够即汇总，§4.3）——
      传 `conn` 让 job 和线索在同一事务里同生同死。

    ★ **不在 HTTP 里直接跑 `run()`**：合成要调 LLM（几十秒），同步跑会把请求堵死，
      也和"写路径全异步"的架构（§2.4）冲突。排个 job 让 worker 去跑，立刻返回 job id。
      dedup_key 按分区收敛：同一分区连排两次只留一个 job（`force` 不进 key，
      否则"先普通点后强制点"会排出两个）。job 内部自判 trigger_new/trigger_mature，
      线索不够数时便宜返回，不产生 LLM 调用——所以每条线索落库后排一次也不烧钱。
    """
    from ..queue import jobs

    scope = f"{org_key or '*'}:{project_key or '*'}:{section_key or '*'}"
    return jobs.enqueue(
        "summarize",
        {"org_key": org_key, "project_key": project_key, "section_key": section_key,
         "force": force},
        org_key=org_key, dedup_key=f"summarize:{scope}", conn=conn)

