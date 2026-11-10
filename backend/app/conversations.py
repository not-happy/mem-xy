"""会话存档查询：`conversation_log`（P1 摄取落的原始对话）的浏览口径。

★ 只读聚合 + 分页明细，不碰模型。和 `metrics.py` 同构：业务 SQL 在这儿，
  `app/api/conversations.py` 只是薄适配器。

会话 = 一次聊天（transcript 一个文件），`session_id` 是它的幂等主键之一。
列表给"哪次聊天、在哪个项目、聊了多少、什么时候"；明细按落库序回放，
`turn_no` 保留窗口抽取用的轮次编号，UI 拿它画轮次分隔。
"""

from __future__ import annotations

import logging

from .core import db

log = logging.getLogger(__name__)


def sessions(*, org_key: str | None = None, limit: int = 100) -> list[dict]:
    """会话列表：按最后一条消息时间倒序。org_key 给了就只列该分区的会话。"""
    return db.query("""
        SELECT session_id, max(source) AS source,
               max(org_key) AS org_key, max(project_key) AS project_key, max(cwd) AS cwd,
               count(*)::int AS msgs,
               count(*) FILTER (WHERE role = 'user')::int AS user_msgs,
               coalesce(sum(tokens_est), 0)::bigint AS tokens_est,
               min(msg_ts) AS first_ts, max(msg_ts) AS last_ts
          FROM conversation_log
         WHERE (%(org)s::text IS NULL OR org_key = %(org)s)
         GROUP BY session_id
         ORDER BY last_ts DESC NULLS LAST
         LIMIT %(limit)s
    """, {"org": org_key, "limit": limit})


def messages(session_id: str, *, offset: int = 0, limit: int = 100) -> dict:
    """一次会话的消息流（落库序 = 时间序）。分页给 UI「加载更多」。"""
    total = db.query_one(
        "SELECT count(*)::int AS n FROM conversation_log WHERE session_id = %s",
        (session_id,))["n"]
    items = db.query("""
        SELECT id, role, turn_no, tokens_est, msg_ts, content
          FROM conversation_log
         WHERE session_id = %s
         ORDER BY id
         LIMIT %s OFFSET %s
    """, (session_id, limit, offset))
    return {"session_id": session_id, "total": total,
            "offset": offset, "items": items}
