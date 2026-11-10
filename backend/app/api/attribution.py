"""归属管理端点（§7.1 的 `GET/PUT /v1/attribution`，P6）。

★ 和 memories.py / docs.py 同构：这一层只校验形状、翻译错误码、序列化。
  业务规则全在 `app/attribution.py`。纯 SQL，不碰模型，所以不需要 503 拦截。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import attribution
from .memories import _guard
from .read import _error

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


@router.get("/attribution")
def get_attribution(
        resolved_by: str | None = None, org_key: str | None = None,
        limit: int = Query(default=500, ge=1, le=2000)) -> JSONResponse:
    """规则 + 目录缓存 + 各来源计数。`resolved_by=default` 就是等人确认的那批。"""
    return _guard(attribution.overview, resolved_by=resolved_by, org_key=org_key, limit=limit)


class WorkspacePut(BaseModel):
    cwd: str = Field(min_length=1, max_length=1000)
    org_key: str = Field(min_length=1, max_length=200)
    project_key: str | None = Field(default=None, max_length=200)
    # ★ 默认回溯改已入库记忆的 org_key（§5.1）；关掉则只改 workspace_map 这一行。
    retroactive: bool = True


@router.put("/attribution")
def put_attribution(body: WorkspacePut) -> JSONResponse:
    """人工定一个目录的归属（resolved_by='human'），可选回溯改已入库记忆。"""
    return _guard(attribution.set_workspace, body.cwd, org_key=body.org_key,
                  project_key=body.project_key, retroactive=body.retroactive)


class RulePut(BaseModel):
    id: int | None = None
    pattern_type: str
    pattern: str = Field(min_length=1, max_length=500)
    org_key: str = Field(min_length=1, max_length=200)
    project_key: str | None = Field(default=None, max_length=200)
    priority: int = 50
    enabled: bool = True


@router.put("/attribution/rules")
def put_rule(body: RulePut) -> JSONResponse:
    """新增（无 id）或改（有 id）一条匹配规则。"""
    return _guard(attribution.upsert_rule, rule_id=body.id, pattern_type=body.pattern_type,
                  pattern=body.pattern, org_key=body.org_key, project_key=body.project_key,
                  priority=body.priority, enabled=body.enabled)


@router.delete("/attribution/rules/{rule_id}")
def delete_rule(rule_id: int) -> JSONResponse:
    return _guard(attribution.delete_rule, rule_id)
