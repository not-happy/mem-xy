"""Connection pool + transaction helpers."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def _configure(conn: Connection) -> None:
    register_vector(conn)
    conn.autocommit = False


def init_pool(min_size: int = 1, max_size: int = 8, timeout: float = 30.0) -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    settings = get_settings()
    _pool = ConnectionPool(
        conninfo=settings.dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        kwargs={"row_factory": dict_row, "connect_timeout": 5},
        configure=_configure,
        open=True,
    )
    log.info("db pool opened (min=%s max=%s)", min_size, max_size)
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        return init_pool()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connect() -> Iterator[Connection]:
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[Connection]:
    """One connection, one transaction. LLM calls must happen *outside* this (plan §5.5)."""
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


def query(sql: str, params: Any = None) -> list[dict]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql: str, params: Any = None) -> dict | None:
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: Any = None) -> int:
    with connect() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def enable_iterative_scan(conn: Connection) -> None:
    """§6.3：HNSW 是"先取 ef_search 个候选，再套 WHERE 过滤"，**被过滤掉的不补回来**。
    数据量一大，`LIMIT 5` 经常只返回 1–2 条甚至 0 条，而且不报错——表现为
    "记忆系统好像不记事"，极难定位。pgvector ≥ 0.8 用迭代扫描解决。

    ★ 必须在事务里调（`SET LOCAL`）。值名是 `relaxed_order`，**不是** `relaxed_ordering`：
      写错 PG 会直接报 InvalidParameterValue，合法值只有 off / relaxed_order / strict_order。
    """
    conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    conn.execute("SET LOCAL hnsw.max_scan_tuples = 20000")


def server_version() -> tuple[int, str, str]:
    """Returns (server_version_num, server_version, pgvector_version-or-empty)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT current_setting('server_version_num')::int AS num, "
            "       current_setting('server_version') AS ver"
        ).fetchone()
        ext = conn.execute(
            "SELECT coalesce(max(extversion), '') AS v FROM pg_extension WHERE extname='vector'"
        ).fetchone()
    return row["num"], row["ver"], ext["v"]
