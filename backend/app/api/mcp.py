"""MCP 接入探视端点（`GET/POST /v1/mcp*`）——任务队列页 MCP 卡片的数据源。

★ 与 workers 端点最大的不同：这里**没有启动按钮**。stdio MCP（mem_mcp.py）由 AI
  客户端（claude.exe 等）在会话里按需拉起，服务端代启只会造出无主进程——没有客户端
  连着，它挂在 stdin 上空转，cwd 归属也是错的（decisions-log #82/#83 的结论）。
  所以只有三个动作：看状态 / 清孤儿 / 真连接自检。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import mcp_ctl
from .memories import _guard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


class StopBody(BaseModel):
    orphans_only: bool = True


@router.get("/mcp/status")
def mcp_status() -> JSONResponse:
    return _guard(lambda: mcp_ctl.status())


@router.post("/mcp/stop")
def mcp_stop(body: StopBody) -> JSONResponse:
    return _guard(lambda: mcp_ctl.stop(orphans_only=body.orphans_only))


@router.post("/mcp/selftest")
def mcp_selftest() -> JSONResponse:
    """真·连接自检：起一个真 stdio 客户端走 initialize + list_tools（耗时秒级）。"""
    return _guard(mcp_ctl.selftest)
