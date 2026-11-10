"""重跑端点（§7.1 的 `/v1/replay*`，P6）。

★ 和 orgs.py / sections.py 同构：这一层只翻译错误码、序列化、按需要 503 拦截。
  业务在 `app/replay.py`。

★ 503 拦截只加在 **apply**（ADD/UPDATE 要算 embedding）；列 run、看 diff、丢弃都不碰模型。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import replay
from .memories import _guard, _model_ready

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


class ApplyBody(BaseModel):
    action_ids: list[int] | None = None   # 缺省 = apply 这个 run 里全部未 applied 的


@router.get("/replay/runs")
def list_runs() -> JSONResponse:
    """全部重跑 run + 动作计数。纯读，不碰模型。"""
    return _guard(replay.list_runs)


@router.get("/replay/runs/{run_id}")
def get_run(run_id: int) -> JSONResponse:
    """一个 run 的全部动作（带目标记忆当前内容，供 old→new diff）。纯读。"""
    return _guard(replay.run_detail, run_id)


@router.post("/replay/runs/{run_id}/apply")
def apply_run(run_id: int, body: ApplyBody | None = None) -> JSONResponse:
    """逐条 apply。★ ADD/UPDATE 要算 embedding，模型没热就 503。"""
    blocked = _model_ready()
    if blocked:
        return blocked
    action_ids = body.action_ids if body else None
    return _guard(replay.apply_run, run_id, action_ids=action_ids)


@router.post("/replay/runs/{run_id}/discard")
def discard_run(run_id: int) -> JSONResponse:
    """丢弃一个 run。纯 SQL，不碰模型。"""
    return _guard(replay.discard_run, run_id)
