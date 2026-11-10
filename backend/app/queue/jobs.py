"""PG-native job queue: `FOR UPDATE SKIP LOCKED` + lease. No Redis, no Celery (plan §8).

Six resumable breakpoints live in tables; this module owns #3 (`jobs.status` + `lease_until`).
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from typing import Callable

from psycopg import Connection

from ..core import db, llm
from ..core.config import get_settings
from ..core.redact import redact

log = logging.getLogger(__name__)

Handler = Callable[[dict, Connection], None]

CLAIM_SQL = """
UPDATE jobs SET status='running',
                lease_until = now() + make_interval(secs => %(lease)s),
                attempts = attempts + 1,
                started_at = now()
 WHERE id = (
    SELECT id FROM jobs
     WHERE status='pending'
        OR (status='running' AND lease_until < now())   -- zombie lease reclaim
     ORDER BY id
     FOR UPDATE SKIP LOCKED
     LIMIT 1
 )
RETURNING *
"""

ENQUEUE_SQL = """
INSERT INTO jobs (kind, payload, org_key, dedup_key, max_attempts)
VALUES (%(kind)s, %(payload)s::jsonb, %(org_key)s, %(dedup_key)s, %(max_attempts)s)
ON CONFLICT (dedup_key) WHERE status IN ('pending','running') DO NOTHING
RETURNING id
"""


def enqueue(
    kind: str,
    payload: dict,
    *,
    org_key: str | None = None,
    dedup_key: str | None = None,
    conn: Connection | None = None,
    max_attempts: int | None = None,
) -> int | None:
    """Returns the new job id, or None when a live duplicate was already queued.

    Pass `conn` to enqueue inside the caller's transaction (the normal case for
    state transitions like `pending_items -> approved`).
    """
    params = {
        "kind": kind,
        "payload": json.dumps(payload, ensure_ascii=False),
        "org_key": org_key,
        "dedup_key": dedup_key,
        "max_attempts": max_attempts or get_settings().job_max_attempts,
    }
    if conn is not None:
        row = conn.execute(ENQUEUE_SQL, params).fetchone()
        return row["id"] if row else None
    with db.connect() as own:
        row = own.execute(ENQUEUE_SQL, params).fetchone()
        own.commit()
        return row["id"] if row else None


def claim(lease_seconds: int | None = None) -> dict | None:
    settings = get_settings()
    lease = lease_seconds or settings.job_lease_seconds
    with db.connect() as conn:
        row = conn.execute(CLAIM_SQL, {"lease": lease}).fetchone()
        conn.commit()
        return row


def complete(job_id: int, started_at=None) -> bool:
    """Mark done. Returns whether the row was actually updated.

    ★ `started_at` 是所有权令牌（claim 时 SET、reclaim 时重设，续租不动它）：
      租约过期被别的 worker 回收后，卡死的旧 worker 醒来再 complete，会把别人
      **正在跑**的 job 标成 done——之后那个 worker 一 fail，job 又被打回 pending，
      同一条素材被执行第三遍。带上令牌就只会更新仍归自己所有的那次执行。
    """
    if started_at is not None:
        n = db.execute(
            "UPDATE jobs SET status='done', finished_at=now(), lease_until=NULL, last_error=NULL "
            "WHERE id=%s AND started_at=%s",
            (job_id, started_at),
        )
        if n == 0:
            log.warning("job %s complete skipped: 所有权已丢失（租约曾被回收？）", job_id)
        return bool(n)
    return bool(db.execute(
        "UPDATE jobs SET status='done', finished_at=now(), lease_until=NULL, last_error=NULL "
        "WHERE id=%s",
        (job_id,),
    ))


def fail(job_id: int, error: str, started_at=None) -> str:
    """Mark failed, or re-queue for another attempt if the budget is not exhausted.

    `last_error` is redacted: httpx exception strings carry request headers and this
    column is rendered in the debug UI (plan §2.1).

    ★ `started_at` 所有权令牌，语义同 `complete`：丢了所有权的执行不配改写 job 状态。
    """
    message = redact(error) or "unknown error"
    owned = " AND started_at = %(started)s" if started_at is not None else ""
    params = {"err": message[:4000], "id": job_id}
    if started_at is not None:
        params["started"] = started_at
    with db.connect() as conn:
        row = conn.execute(
            f"""
            UPDATE jobs
               SET last_error = %(err)s,
                   lease_until = NULL,
                   status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,
                   finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END
             WHERE id = %(id)s{owned}
            RETURNING status, attempts, max_attempts
            """,
            params,
        ).fetchone()
        conn.commit()
    if row:
        log.warning("job %s -> %s (attempt %s/%s): %s",
                    job_id, row["status"], row["attempts"], row["max_attempts"], message[:300])
        return row["status"]
    if started_at is not None and db.query_one("SELECT 1 FROM jobs WHERE id=%s", (job_id,)):
        log.warning("job %s fail skipped: 所有权已丢失（租约曾被回收？）", job_id)
        return "stolen"
    return "missing"


def renew_lease(job_id: int, lease_seconds: int | None = None, started_at=None) -> None:
    """★ 同样带所有权令牌：被回收后旧 worker 若无条件续租，会把新 worker 手里
    正在跑的 job 租约越续越长，僵尸回收机制对它失效。"""
    lease = lease_seconds or get_settings().job_lease_seconds
    if started_at is not None:
        db.execute(
            "UPDATE jobs SET lease_until = now() + make_interval(secs => %s)"
            " WHERE id = %s AND started_at = %s",
            (lease, job_id, started_at),
        )
        return
    db.execute(
        "UPDATE jobs SET lease_until = now() + make_interval(secs => %s) WHERE id = %s",
        (lease, job_id),
    )


def set_progress(job_id: int, progress: dict) -> None:
    """running job 的逐窗口进度（migration 0004），UI 点任务看执行过程用。

    ★ 尽力而为：进度写失败绝不能反噬主路径（和 llm_usage 记录同口径）。
    """
    try:
        db.execute("UPDATE jobs SET progress = %s::jsonb WHERE id = %s",
                   (json.dumps(progress, ensure_ascii=False), job_id))
    except Exception:  # noqa: BLE001
        log.debug("set_progress failed for job %s", job_id, exc_info=True)


def stats() -> dict:
    rows = db.query("SELECT status, count(*) AS n FROM jobs GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def run_worker(
    handlers: dict[str, Handler],
    *,
    idle_sleep: float = 0.5,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Blocking worker loop. Unknown job kinds go to `failed`, not to a silent skip.

    ★ 只有**空闲**（claim 不到 job）才 sleep。之前每处理完一个 job 也 sleep 1s，
      单 worker 吞吐被自限在 ~1 job/s——回填期队列里排着几百个 job 时，
      这个常数直接乘在总工期上。
    """
    log.info("worker started; handling %s", ",".join(sorted(handlers)))
    while not (should_stop and should_stop()):
        job = claim()
        if job is None:
            time.sleep(idle_sleep)
            continue
        kind = job["kind"]
        handler = handlers.get(kind)
        if handler is None:
            fail(job["id"], f"no handler registered for kind={kind!r}",
                 started_at=job["started_at"])
            continue
        started = time.perf_counter()
        # ★ 绑定 job 归属：handler 里每次 LLM 调用的 llm_usage 行都带 job_id，
        #   任务队列页才能按 job 下钻出调用时间线（migration 0004）。
        ctx_token = llm.set_job(job["id"])
        try:
            # ★ No ambient transaction: LLM calls must happen *outside* one (plan §5.5).
            # Handlers open `with conn.transaction():` around their own DB writes.
            with db.connect() as conn:
                handler(job, conn)
        except Exception as exc:  # noqa: BLE001 — the queue is the retry boundary
            fail(job["id"], f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}",
                 started_at=job["started_at"])
        else:
            complete(job["id"], job["started_at"])
            log.info("job %s (%s) done in %.2fs", job["id"], kind, time.perf_counter() - started)
        finally:
            llm.clear_job(ctx_token)
    log.info("worker stopped")
