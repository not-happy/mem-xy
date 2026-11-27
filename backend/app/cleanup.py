"""★ A9（S6）：保留期清理——把"只进不出"的流水表按窗口滚掉。

旧评审 A4 / S6 指出的缺口：transcript 全量入库、recall_log 每轮一行、
llm_usage 每次调用一行、route_decisions 每条判定一行，全部只进不出，
隐私面和库体积单调增长。本模块提供唯一的清理入口 `cleanup()`：

- conversation_log：按 org 的 `retention_days`（orgs 里没配的用
  `cleanup.retention_days` 兜底；配置 0 = 该 org 永不清）。删除窗口的
  时间口径是 `coalesce(msg_ts, ingested_at)`——conversation_log 没有
  created_at，msg_ts 是消息的真实时间戳。
  ★ 硬约束：memories / raw_observations 的 source_msg 对本表有 FK
  （NO ACTION），pending_items / route_decisions / memory_events 的
  source_msg 虽无 FK 但同样指向这里的行——被**任何一张表引用**的
  行一律不删（审计可追溯优先于清库干净）。
- recall_log / llm_usage / route_decisions：固定 90 天滚动
  （`cleanup.older_days`，route_decisions 用 created_at）。
- jobs：done/failed 且 finished_at 过窗的行（队列重放/排障的近 90 天留档）。
- memory_events：**不清**——记忆的审计链，随 memories 走。

调度：`maybe_enqueue_daily()` 查最近一次成功 cleanup，超过 24h 才补一个
cleanup job（dedup_key 按 UTC 日期，天然当日去重）；挂在 worker 的 scan
handler 与启动处，不新增人工环节。
"""

from __future__ import annotations

import datetime as _dt
import logging

from .core import db
from .core.config import get_settings

log = logging.getLogger(__name__)

JOB_KIND = "cleanup"

# 被引用保护表：conversation_log.id 出现在任何一张的 source_msg 里就不删。
_REFERENCED_BY = (
    "SELECT 1 FROM memories             WHERE source_msg = cl.id"
    " UNION ALL SELECT 1 FROM raw_observations WHERE source_msg = cl.id"
    " UNION ALL SELECT 1 FROM pending_items    WHERE source_msg = cl.id"
    " UNION ALL SELECT 1 FROM route_decisions  WHERE source_msg = cl.id"
)

_LOG_TABLES = ("recall_log", "llm_usage", "route_decisions")


def _conversation_log_cut(retention_days: int) -> str:
    """按保留窗口拼 conversation_log 的删除 SQL。引用行排除，见模块 docstring。"""
    return f"""
        DELETE FROM conversation_log cl
        WHERE cl.org_key IS NOT DISTINCT FROM %s
          AND coalesce(cl.msg_ts, cl.ingested_at) < now() - make_interval(days => %s)
          AND NOT EXISTS ({_REFERENCED_BY})
    """


def _older_days_cut(table: str, column: str) -> str:
    return (
        f"DELETE FROM {table} WHERE {column} IS NOT NULL"
        f" AND {column} < now() - make_interval(days => %s)"
    )


def cleanup(conn) -> dict:
    """执行一轮清理，返回逐表删除行数（审计/日志用）。调用方给连接。"""
    settings = get_settings()
    older = settings.cleanup_older_days
    fallback = settings.cleanup_retention_days
    deleted: dict[str, int] = {}

    # ── conversation_log：按 org 保留期（空 retention_days 用兜底值；0 = 永不清）──
    orgs = conn.execute(
        "SELECT org_key, retention_days FROM orgs WHERE enabled").fetchall()
    cuts: list[tuple[str | None, int]] = []
    for row in orgs:
        days = row["retention_days"]
        if days is None:
            days = fallback
        if days <= 0:
            continue
        cuts.append((row["org_key"], days))
    # 没有任何 org 记录的行（org_key IS NULL / 游离行）按兜底值走。
    cuts.append((None, fallback))
    cl_total = 0
    for org_key, days in cuts:
        row = conn.execute(_conversation_log_cut(days), (org_key, days))
        cl_total += row.rowcount or 0
    deleted["conversation_log"] = cl_total

    # ── 流水/日志类：固定 90 天 ──
    for table in _LOG_TABLES:
        row = conn.execute(_older_days_cut(table, "created_at"), (older,))
        deleted[table] = row.rowcount or 0

    # ── jobs：完结（done/failed）且 finished_at 过窗的 ──
    row = conn.execute(
        "DELETE FROM jobs WHERE status IN ('done','failed')"
        " AND finished_at IS NOT NULL"
        " AND finished_at < now() - make_interval(days => %s)",
        (older,))
    deleted["jobs"] = row.rowcount or 0

    log.info("cleanup 完成：%s", deleted)
    return deleted


def maybe_enqueue_daily(conn) -> int | None:
    """距离上次成功 cleanup 超过 24h 才补一个 job；返回 job id（None=不需要）。

    dedup_key 按 UTC 日期——同一天重复调用天然幂等（jobs_dedup 部分唯一索引）。
    """
    today = _dt.datetime.now(_dt.timezone.utc).strftime("cleanup:%Y-%m-%d")
    row = conn.execute(
        "SELECT max(finished_at) AS last_ok FROM jobs"
        " WHERE kind = %s AND status = 'done'", (JOB_KIND,)).fetchone()
    last = row["last_ok"] if row else None
    if last is not None and _dt.datetime.now(_dt.timezone.utc) - last < _dt.timedelta(hours=24):
        return None
    from .queue import jobs as jobs_mod

    return jobs_mod.enqueue(JOB_KIND, {}, dedup_key=today, conn=conn)
