"""摄取端点（§7.1）：`POST /v1/ingest/scan`。

★ 两种调用方，两种时序要求，所以一个端点两条路径：

| 调用方 | 要什么 | 走法 |
|---|---|---|
| Stop hook | 发完就走，**绝不阻塞会话结束** | 默认：排一个 `scan` job，立刻回 202 |
| UI / CLI | 要看到"这次扫进来多少条" | `?sync=true`：就地跑完再回 200 + 统计 |

hook 那条路必须是异步的：全量解析 195MB 要 15s（首次），而 hook 的预算是 400ms。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..queue import jobs
from .read import _error

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


@router.post("/ingest/scan")
def ingest_scan(sync: bool = Query(default=False),
                include_subagents: bool = Query(default=False)) -> JSONResponse:
    from ..ingest import scan as scan_mod

    payload = {"include_subagents": include_subagents}
    if not sync:
        # ★ dedup_key 不带时间戳：一轮对话结束可能连着触发好几次 Stop，
        #   排一堆一样的扫描 job 只是白烧 CPU（扫描本身幂等，但没必要）。
        job_id = jobs.enqueue("scan", payload, dedup_key="scan:claude-code")
        return JSONResponse(
            {"queued": job_id is not None, "job_id": job_id,
             "note": "扫描在队列里跑，`GET /v1/jobs` 或 worker 日志看结果"},
            status_code=202)

    try:
        return JSONResponse(scan_mod.scan_and_queue(include_subagents=include_subagents))
    except Exception as exc:  # noqa: BLE001
        log.exception("sync scan failed")
        return _error(500, "scan_failed", str(exc))
