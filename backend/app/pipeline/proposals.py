"""§5.7 提案裁决：`doc_proposals` 的 accept / reject + 落库 job。

和 `review.py`（`pending_items` 的裁决）同构，但状态更少——`doc_proposals` 的 CHECK
只有 `pending/accepted/rejected/expired`，没有 `writing/failed`。所以"成功才算数"由
**job 自己的状态**兜底：accept/reject 只负责抢占式翻状态 + 排 `apply_proposal` job，
真正的写入（living_doc upsert / 否决 observation）在 job 里做，job 失败会重试、
永久失败会停在 `jobs.status='failed'`（UI 可见），不会静默丢。

```
pending ──人采纳──► accepted ──排 job──► apply_proposal：upsert living_doc(confirmed) + history
pending ──人否决──► rejected ──排 job──► apply_proposal：写 raw_observations(polarity='negative')
```

★ **为什么 reject 也走 job 而不是同步写**：否决要写一条带 embedding 的 negative
  observation（比对② 靠它做向量检索）。算 embedding 要么把 HTTP 请求堵在冷启动上
  （实测 11–33s），要么 503 挡住让人重试——都不如丢进 worker 里慢慢算。
  否决记录是"冷启动噪声期收敛的唯一机制"（§4.3.2），但它服务的是**以后**的比对，
  晚几百毫秒落库没有任何影响。
★ **accept 写的是 `confirmed`，不是 `draft`**：人已经审过并采纳了，这正是 §5.7 第二道
  人审的意义；confirmed 才会被 §6.5 bootstrap 注入。draft 是"还没过审"的中间态，
  采纳了就该直接生效。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from psycopg import Connection

from ..core import db
from ..core.embedding import get_embedding_service
from ..core.hashing import content_hash
from ..prompts import summarize as summarize_prompt
from ..queue import jobs

log = logging.getLogger(__name__)


@contextmanager
def _tx(conn: Connection | None):
    """翻状态 + 排 job 必须同生同死，否则会出"accepted 但没有 job"的遗留单。"""
    if conn is not None:
        yield conn
    else:
        with db.transaction() as own:
            yield own


def accept(proposal_id: int, content: str | None = None, *,
           conn: Connection | None = None) -> int | None:
    """人点了"采纳"。`content` 非空 = 手改后采纳（§7.1）。返回排出的 job id；
    抢不到（已被处理）返回 None。

    ★ 手改与否决定 `living_doc_history.changed_by`：原文采纳是 `summarizer`
      （内容是 AI 合成的），改过再采纳是 `human`（最终文本是人定的）。
    """
    with _tx(conn) as tx:
        row = tx.execute(
            "UPDATE doc_proposals SET status='accepted'"
            " WHERE id=%s AND status='pending' RETURNING *", (proposal_id,)).fetchone()
        if row is None:
            return None
        override = (content or "").strip()
        final = override or row["new_content"]
        changed_by = "human" if override and override != row["new_content"] else "summarizer"
        job_id = jobs.enqueue(
            "apply_proposal",
            {"proposal_id": proposal_id, "content": final, "changed_by": changed_by},
            org_key=row["org_key"], dedup_key=f"proposal:{proposal_id}", conn=tx)
        tx.execute("UPDATE doc_proposals SET job_id=%s WHERE id=%s", (job_id, proposal_id))
    return job_id


def reject(proposal_id: int, *, conn: Connection | None = None) -> int | None:
    """人点了"否决"。翻状态 + 排 job 去写否决记录（§4.3.2 / #67）。返回 job id 或 None。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "UPDATE doc_proposals SET status='rejected'"
            " WHERE id=%s AND status='pending' RETURNING *", (proposal_id,)).fetchone()
        if row is None:
            return None
        job_id = jobs.enqueue(
            "apply_proposal", {"proposal_id": proposal_id},
            org_key=row["org_key"], dedup_key=f"proposal:{proposal_id}", conn=tx)
        tx.execute("UPDATE doc_proposals SET job_id=%s WHERE id=%s", (job_id, proposal_id))
    return job_id


def apply_proposal(payload: dict) -> dict:
    """worker handler：把裁决真正落库。★ 自管事务（同 review.apply_decision 的理由）。"""
    proposal_id = payload["proposal_id"]
    row = db.query_one("SELECT * FROM doc_proposals WHERE id=%s", (proposal_id,))
    if row is None:
        log.warning("doc_proposals#%s 不存在，跳过", proposal_id)
        return {"action": "missing"}
    if row["status"] == "accepted":
        return _apply_accept(row, payload)
    if row["status"] == "rejected":
        return _apply_reject(row)
    return {"action": "skipped", "status": row["status"]}


def _apply_accept(row: dict, payload: dict) -> dict:
    """upsert living_doc(confirmed) + 写 living_doc_history。纯 SQL，一个短事务。"""
    final = payload.get("content") or row["new_content"]
    changed_by = payload.get("changed_by") or "summarizer"
    source_ids = row["source_ids"] or []
    with db.transaction() as tx:
        # ★ A10：先 FOR UPDATE 锁住 living_doc 行，把"两个 worker 同时重放同一提案"
        #   串行化——原来的顺序是先查 history 再锁，两个并发执行都能通过预查，
        #   然后各 version+1（TOCTOU）。锁上之后再查 history，后到者一定看到先到者
        #   写入的行（或被下面的部分唯一索引兜底拦住）。
        existing = tx.execute(
            "SELECT id, content, authority, version FROM living_doc"
            " WHERE org_key=%(org)s AND section_key=%(sec)s"
            "   AND project_key IS NOT DISTINCT FROM %(proj)s FOR UPDATE",
            {"org": row["org_key"], "sec": row["section_key"],
             "proj": row["project_key"]}).fetchone()
        # 幂等保护：job 可能在"写完但没来得及 complete"后被其它 worker 回收重放。
        # history 里已有这个 proposal_id 就说明落过了，别再 version+1、别再叠 history。
        if tx.execute("SELECT 1 FROM living_doc_history WHERE proposal_id=%s LIMIT 1",
                      (row["id"],)).fetchone():
            return {"action": "already_applied", "proposal_id": row["id"]}
        if existing:
            tx.execute(
                "UPDATE living_doc SET content=%s, authority='confirmed', origin='summarized',"
                "       source_ids=%s, version=version+1, updated_at=now() WHERE id=%s",
                (final, source_ids, existing["id"]))
            doc_id, new_version = existing["id"], existing["version"] + 1
            old_content, old_authority = existing["content"], existing["authority"]
        else:
            doc_id = tx.execute(
                "INSERT INTO living_doc (org_key, project_key, section_key, content,"
                "                         authority, origin, source_ids, version)"
                " VALUES (%s,%s,%s,%s,'confirmed','summarized',%s,1) RETURNING id",
                (row["org_key"], row["project_key"], row["section_key"], final,
                 source_ids)).fetchone()["id"]
            new_version, old_content, old_authority = 1, None, None
        # ★ A10：partial UNIQUE(proposal_id)（migration 0007）兜底——即便上面两道
        #   都被并发穿过，这里最多插入一行，后到者 rowcount=0 走 already_applied。
        #   （ON CONFLICT 的冲突目标必须与部分索引谓词匹配。）
        history = tx.execute(
            "INSERT INTO living_doc_history (living_doc_id, version, old_content, new_content,"
            "                               old_authority, new_authority, proposal_id, changed_by)"
            " VALUES (%s,%s,%s,%s,%s,'confirmed',%s,%s)"
            " ON CONFLICT (proposal_id) WHERE proposal_id IS NOT NULL DO NOTHING"
            " RETURNING id",
            (doc_id, new_version, old_content, final, old_authority, row["id"], changed_by)
        ).fetchone()
        if history is None:
            return {"action": "already_applied", "proposal_id": row["id"]}
    return {"action": "applied", "proposal_id": row["id"], "living_doc_id": doc_id,
            "version": new_version, "changed_by": changed_by}


def _apply_reject(row: dict) -> dict:
    """写一条 negative observation（否决记录，#67）。★ embedding 在事务外算。"""
    text = row["new_content"]
    if not (text or "").strip():
        return {"action": "skipped", "reason": "empty_content"}
    hash_ = content_hash(text)
    # 幂等保护：同一条否决重放不叠第二行（source_msg 为 NULL，UNIQUE 约束兜不住）。
    if db.query_one(
            "SELECT 1 FROM raw_observations WHERE polarity='negative' AND content_hash=%(h)s"
            "   AND org_key=%(org)s AND section_key=%(sec)s"
            "   AND project_key IS NOT DISTINCT FROM %(proj)s",
            {"h": hash_, "org": row["org_key"], "sec": row["section_key"],
             "proj": row["project_key"]}):
        return {"action": "already_applied", "proposal_id": row["id"]}
    embedding = get_embedding_service().encode_one(text)   # 事务外：可能触发冷启动
    with db.transaction() as tx:
        obs_id = tx.execute(
            "INSERT INTO raw_observations (org_key, project_key, section_key, content,"
            "                              content_hash, polarity, keywords, embedding,"
            "                              source_msg, prompt_ver)"
            " VALUES (%s,%s,%s,%s,%s,'negative',%s,%s,NULL,%s) RETURNING id",
            (row["org_key"], row["project_key"], row["section_key"], text, hash_,
             [], embedding, summarize_prompt.PROMPT_VER)).fetchone()["id"]
    return {"action": "vetoed", "proposal_id": row["id"], "observation_id": obs_id}


# ─────────────────────────── 只读：给 UI 列提案 ───────────────────────────

_PROPOSAL_COLUMNS = ("p.id, p.org_key, p.project_key, p.section_key, p.old_content,"
                     " p.new_content, p.status, p.job_id, p.source_ids, p.created_at,"
                     " p.expires_at, s.label, j.status AS job_status, j.last_error")


def list_proposals(*, status: str | None = "pending", org_key: str | None = None,
                   project_key: str | None = None, section_key: str | None = None,
                   limit: int = 50, offset: int = 0) -> dict:
    """待审 diff 列表（`GET /v1/proposals`）。默认只看 `pending`——那是等人裁决的。

    ★ 带上 `job_status`/`last_error`：accepted/rejected 之后真正的写入在 job 里（#86），
      proposal 停在 accepted/rejected，成没成要看它指向的 job。UI 得能把"采纳了但落库炸了"
      这种情况显示出来，否则就是 §5.7 说的静默丢失。
    """
    where: list[str] = ["TRUE"]
    params: dict = {"limit": limit, "offset": offset}
    if status:
        where.append("p.status = %(status)s")
        params["status"] = status
    if org_key:
        where.append("p.org_key = %(org)s")
        params["org"] = org_key
    if project_key:
        where.append("p.project_key IS NOT DISTINCT FROM %(proj)s")
        params["proj"] = project_key
    if section_key:
        where.append("p.section_key = %(sec)s")
        params["sec"] = section_key
    clause = " WHERE " + " AND ".join(where)
    total = db.query_one(f"SELECT count(*) AS n FROM doc_proposals p{clause}", params)["n"]
    rows = db.query(
        f"SELECT {_PROPOSAL_COLUMNS} FROM doc_proposals p"
        f" JOIN doc_sections s USING (section_key)"
        f" LEFT JOIN jobs j ON j.id = p.job_id"
        f"{clause} ORDER BY p.id DESC LIMIT %(limit)s OFFSET %(offset)s", params)
    return {"total": total, "limit": limit, "offset": offset, "items": rows}


def get_proposal(proposal_id: int) -> dict:
    from ..library import not_found

    row = db.query_one(
        f"SELECT {_PROPOSAL_COLUMNS} FROM doc_proposals p"
        f" JOIN doc_sections s USING (section_key)"
        f" LEFT JOIN jobs j ON j.id = p.job_id WHERE p.id = %(id)s", {"id": proposal_id})
    if row is None:
        raise not_found("提案", proposal_id)
    return row

