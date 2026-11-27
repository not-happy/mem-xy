"""Async worker entrypoint: `python -m app.worker`.

The handler registry grows as stages land — P2 registers `route` / `write_facts` /
`apply_decision`; P5 adds `summarize` (plan §5.6).
"""

from __future__ import annotations

import logging
import signal
import threading

from .core import db
from .core.config import get_settings
from .core.llm import get_llm_client
from .queue.jobs import run_worker

log = logging.getLogger(__name__)


def _handle_route(job: dict, conn) -> None:
    """★ **故意不接 `conn`**：`route_session` 自管三段式事务边界（读→LLM→写），
    LLM 调用在它内部的**事务外**发生（§5.5）。把 LLM 关在 worker 的连接/事务里
    正是 §5.5 明确禁止的。"""
    from .pipeline import router
    from .queue import jobs

    payload = job["payload"]
    job_id = job.get("id")
    # ★ 一个会话的路由动辄 8 分钟以上（实测单次 LLM ≈53s，平均 9 个窗口/会话），
    #   远超 `queue.lease_seconds=300`。租约一过期，别的 worker 就会抢走这条过期任务重跑。
    #   两层防护：① 每个窗口开头续一次租约（崩了仍然 300s 内可回收，活着就不会被抢）；
    #   ② pending_items 有 dedup_hash 幂等键 + complete/fail 校验所有权，
    #      即便真的被抢，重复执行也排不出第二套待办卡（decisions-log #70 的缺口已补）。
    #   顺手把 {window, windows_total} 写进 jobs.progress——大会话跑几十分钟，
    #   没这个进度 UI 上完全看不出它在动（migration 0004）。
    def on_window(idx: int, total: int) -> None:
        if not job_id:
            return
        # ★ 带 started_at 所有权令牌：租约万一还是被回收了，续租不会误续到
        #   接管者手里那次执行上（详见 jobs.renew_lease）。
        jobs.renew_lease(job_id, started_at=job.get("started_at"))
        jobs.set_progress(job_id, {"window": idx, "windows_total": total})

    router.route_session(payload["session_id"], source=payload.get("source", "claude-code"),
                         limit=payload.get("limit"),
                         on_window=on_window if job_id else None)


def _handle_write_facts(job: dict, conn) -> None:
    from .pipeline import facts
    from .queue import jobs

    payload = job["payload"]
    job_id = job.get("id")
    # ★ A7：write_facts 的 LLM 判重是全流程最慢的 job（每个"桶×块"一次 LLM 调用，
    #   慢时几十秒一块），route 那种窗口级续租盖不住这里——租约一过期，第二个 worker
    #   就会认领重跑（事件双写、计数双加）。每个判重块开始前续一次租约 +
    #   把 {bucket, buckets_total} 写进 progress；幂等由 UNIQUE(source_msg, content_hash)
    #   和 memory_events 唯一索引兜底（即便真被抢，重放也不产生第二行）。
    def on_bucket(idx: int, total: int) -> None:
        if not job_id:
            return
        jobs.renew_lease(job_id, started_at=job.get("started_at"))
        jobs.set_progress(job_id, {"bucket": idx, "buckets_total": total})

    stats = facts.write_facts(payload, on_bucket=on_bucket if job_id else None)
    if stats is not None:
        log.info("write_facts job %s: %s", job_id, stats.as_dict())


def _handle_apply_decision(job: dict, conn) -> None:
    """批准 → 落库。不调 LLM，但**同样自管事务**：失败标记必须比抛错先落盘。"""
    from .pipeline import review

    review.apply_decision(job["payload"])


def _handle_apply_proposal(job: dict, conn) -> None:
    """提案采纳/否决的落库。reject 分支要算 embedding（否决记录），所以自管事务、
    不接 worker 的连接——理由同 _handle_route。"""
    from .pipeline import proposals

    proposals.apply_proposal(job["payload"])


def _handle_summarize(job: dict, conn) -> None:
    """观察池 → doc_proposals。★ 自管连接，理由同 _handle_route：里面要调 LLM。
    payload 可空（全量扫一遍候选分区），也可带 org/project/section/force 只处理一个。"""
    from .pipeline import summarize

    payload = job.get("payload") or {}
    stats = summarize.run(org_key=payload.get("org_key"),
                          project_key=payload.get("project_key"),
                          section_key=payload.get("section_key"),
                          force=bool(payload.get("force")))
    log.info("summarize job %s: scanned=%d proposed=%d empty=%d locked=%d skipped=%d",
             job.get("id"), stats.scanned, stats.proposed, stats.empty,
             stats.locked, stats.skipped)


def _handle_scan(job: dict, conn) -> None:
    """Stop hook 触发的那一下：扫 transcript → 给有新素材的会话排 `route` job。

    ★ 同样不接 `conn`：`scan_once` 自己按批开事务，而且全程不调 LLM，
      但一个会话一个事务是刻意的（一个文件坏了不能拖垮整批）。
    """
    from .ingest import scan

    payload = job.get("payload") or {}
    result = scan.scan_and_queue(include_subagents=bool(payload.get("include_subagents")))
    log.info("scan job %s: %s", job.get("id"), result)


def _handle_cleanup(job: dict, conn) -> None:
    """★ A9：保留期清理（app/cleanup.py 模块注释讲全口径）。自管事务。"""
    from . import cleanup as cleanup_mod

    with db.transaction() as tx:
        deleted = cleanup_mod.cleanup(tx)
    log.info("cleanup job %s: %s", job.get("id"), deleted)


def _handle_dedup_scan(job: dict, conn) -> None:
    """★ B4-②：近重复合并扫描（app/dedup_scan.py 模块注释讲全口径）。自管事务。"""
    from . import dedup_scan as dedup_mod

    with db.transaction() as tx:
        result = dedup_mod.scan(tx)
    log.info("dedup_scan job %s: %s", job.get("id"), result)


def _handle_auto_review(job: dict, conn) -> None:
    """★ 双判共识制：待审卡的第二遍 LLM 复判（app/pipeline/auto_review.py）。
    LLM 在事务外（§5.5）——不接 conn，auto_review.handle 自管事务。"""
    from .pipeline import auto_review

    result = auto_review.handle(job["payload"])
    log.info("auto_review job %s: %s", job.get("id"), result)


def maybe_enqueue_cleanup() -> None:
    """★ A9：worker 起来时看一眼要不要补当日 cleanup job（24h 节流 + 当日去重）。"""
    from . import cleanup as cleanup_mod
    from .queue import jobs

    try:
        with db.transaction() as tx:
            job_id = cleanup_mod.maybe_enqueue_daily(tx)
        if job_id:
            log.info("enqueued daily cleanup job %s", job_id)
    except Exception:  # noqa: BLE001 — 调度失败不该影响 worker 起来干活
        log.exception("maybe_enqueue_cleanup failed")


def maybe_enqueue_dedup_scan() -> None:
    """★ B4-②：worker 起来时看一眼要不要补当日近重复合并扫描（同 cleanup 口径）。"""
    from . import dedup_scan as dedup_mod

    try:
        with db.transaction() as tx:
            job_id = dedup_mod.maybe_enqueue_daily(tx)
        if job_id:
            log.info("enqueued daily dedup_scan job %s", job_id)
    except Exception:  # noqa: BLE001 — 调度失败不该影响 worker 起来干活
        log.exception("maybe_enqueue_dedup_scan failed")


HANDLERS: dict = {
    "route": _handle_route,
    "write_facts": _handle_write_facts,
    "apply_decision": _handle_apply_decision,
    "summarize": _handle_summarize,
    "apply_proposal": _handle_apply_proposal,
    "scan": _handle_scan,
    "cleanup": _handle_cleanup,
    "dedup_scan": _handle_dedup_scan,
    "auto_review": _handle_auto_review,
}

_stop = threading.Event()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )
    settings = get_settings()
    db.init_pool(min_size=1, max_size=4)
    try:
        get_llm_client().refresh_endpoints()
    except Exception:  # noqa: BLE001 — DB may not be migrated yet; workers retry per job
        log.exception("could not preload llm_endpoints")

    for sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), lambda *_: _stop.set())

    maybe_enqueue_cleanup()
    maybe_enqueue_dedup_scan()
    log.info("worker starting; %d handler(s) registered", len(HANDLERS))
    run_worker(HANDLERS, should_stop=_stop.is_set)
    db.close_pool()


if __name__ == "__main__":
    main()
