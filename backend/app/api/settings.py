"""设置端点（§7.1 的 `GET /v1/settings`，P6）。**只读**。

★ 和 metrics.py / orgs.py 同构：这一层只翻译错误码、序列化。业务在 `app/settings_view.py`。
  纯读配置 + 一条 SQL（端点 token 有无），不碰模型，不需要 503 拦截。
  ★ 刻意没有 PUT/POST：改配置走 `~/.memxy/config.toml` + 重启，理由见 settings_view 的模块 docstring。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import settings_view
from .memories import _guard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


@router.get("/settings")
def get_settings_view() -> JSONResponse:
    """生效配置 + 每项来源层 + 端点 token 有无 + 配置文件路径。token/dsn 已打码。"""
    return _guard(settings_view.overview)
