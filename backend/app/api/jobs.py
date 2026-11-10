"""任务队列端点（`GET /v1/jobs`）——回填/写路径进度的可视化数据源。

★ 只读。jobs 表是异步写路径队列（§4：route / write_facts / summarize）。
  payload 可能很大（write_facts 带全部抽取正文），这里只摘 session_id /
  project_key / items 条数，不整包回传；last_error 入库前已 redact（§2.1），
  仍截 300 字符防止列表页被长堆栈撑爆。
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..core import db
from .memories import _guard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")

Status = Literal["pending", "running", "done", "failed", "cancelled"]


@router.get("/jobs")
def list_jobs(
        status: Status | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500)) -> JSONResponse:
    """状态汇总 + 任务列表（running → pending → 其余按 id 倒序）。"""
    return _guard(_jobs, status, limit)


def _jobs(status: str | None, limit: int) -> dict:
    summary = {r["status"]: r["n"] for r in db.query(
        "SELECT status, count(*)::int AS n FROM jobs GROUP BY status")}
    items = db.query("""
        SELECT id, kind, status, attempts, max_attempts, org_key,
               payload->>'session_id'  AS session_id,
               payload->>'project_key' AS project_key,
               coalesce(jsonb_array_length(payload->'items'), 0)::int AS n_items,
               progress, created_at, started_at, finished_at,
               left(coalesce(last_error, ''), 300) AS last_error
          FROM jobs
         WHERE (%(status)s::text IS NULL OR status = %(status)s)
         ORDER BY (status = 'running') DESC, (status = 'pending') DESC, id DESC
         LIMIT %(limit)s
    """, {"status": status, "limit": limit})
    return {"summary": summary, "items": items}


@router.get("/jobs/{job_id}")
def get_job(job_id: int) -> JSONResponse:
    """单个 job 的执行过程：元信息 + 逐窗口进度 + 归属它的 LLM 调用时间线。"""
    return _guard(_job_detail, job_id)


def _job_detail(job_id: int) -> dict:
    job = db.query_one("""
        SELECT id, kind, status, attempts, max_attempts, org_key,
               payload->>'session_id'  AS session_id,
               payload->>'project_key' AS project_key,
               coalesce(jsonb_array_length(payload->'items'), 0)::int AS n_items,
               progress, created_at, started_at, finished_at, lease_until,
               coalesce(last_error, '') AS last_error
          FROM jobs WHERE id = %s
    """, (job_id,))
    if job is None:
        return {"job": None, "calls": []}
    calls = db.query("""
        SELECT id, task, ok, input_tokens, output_tokens, latency_ms,
               left(coalesce(error, ''), 300) AS error, created_at
          FROM llm_usage WHERE job_id = %s ORDER BY id
    """, (job_id,))
    return {"job": job, "calls": calls}
