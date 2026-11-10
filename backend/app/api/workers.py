"""后台 worker 启停端点（`GET/POST /v1/workers*`）——任务队列页的启停按钮数据源。

★ 只在 localhost 的管理 UI 用；start 超上限回 400，stop 幂等（没 worker 也 200）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import workers_ctl
from ..library import LibraryError
from .memories import _guard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


class StartBody(BaseModel):
    count: int = Field(default=2, ge=1, le=workers_ctl.MAX_WORKERS)


class EnsureBody(BaseModel):
    target: int = Field(default=2, ge=0, le=workers_ctl.MAX_WORKERS)


@router.get("/workers")
def list_workers() -> JSONResponse:
    return _guard(lambda: {"workers": workers_ctl.status(),
                           "max": workers_ctl.MAX_WORKERS})


@router.post("/workers/start")
def start_workers(body: StartBody) -> JSONResponse:
    def run() -> dict:
        try:
            return {"workers": workers_ctl.start(body.count)}
        except ValueError as e:
            raise LibraryError(400, "worker_cap", str(e)) from None
    return _guard(run)


@router.post("/workers/ensure")
def ensure_workers(body: EnsureBody) -> JSONResponse:
    """把存活 worker 补齐到 target 个（幂等，已达标一个不动）。

    ★ watchdog（计划任务）专用入口：worker 崩了之后无人拉起是**静默故障**
      （2026-11-02 真实踩过：批准积压数日无告警），守护脚本每 5 分钟打一次这里。
    """
    return _guard(lambda: {"workers": workers_ctl.ensure(body.target)})


@router.post("/workers/stop")
def stop_workers() -> JSONResponse:
    return _guard(lambda: {"killed": workers_ctl.stop_all(),
                           "workers": workers_ctl.status()})
