"""组织管理端点（§7.1 的 `/v1/orgs`、`/v1/endpoints`，P6）。

★ 和 attribution.py / metrics.py 同构：这一层只校验形状、翻译错误码、序列化。
  业务规则全在 `app/orgs.py`。纯 SQL，不碰模型，所以不需要 503 拦截。
  ★ 端点相关操作**绝不接受 token 字段**——token 只走 `~/.memxy/secrets.toml`（§2.1）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import orgs
from .memories import _guard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


@router.get("/orgs")
def get_orgs() -> JSONResponse:
    """orgs（带各自数据量）+ 端点（非敏感）+ task→endpoint 映射。"""
    return _guard(orgs.overview)


class OrgCreate(BaseModel):
    org_key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    is_personal: bool = False
    retention_days: int | None = Field(default=None, ge=1)
    enabled: bool = True


@router.post("/orgs")
def create_org(body: OrgCreate) -> JSONResponse:
    return _guard(orgs.create_org, org_key=body.org_key, name=body.name,
                  is_personal=body.is_personal, retention_days=body.retention_days,
                  enabled=body.enabled)


class OrgPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    retention_days: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    is_personal: bool | None = None


@router.patch("/orgs/{org_key}")
def update_org(org_key: str, body: OrgPatch) -> JSONResponse:
    return _guard(orgs.update_org, org_key, name=body.name,
                  retention_days=body.retention_days, enabled=body.enabled,
                  is_personal=body.is_personal)


@router.delete("/orgs/{org_key}")
def delete_org(org_key: str) -> JSONResponse:
    return _guard(orgs.delete_org, org_key)


class EndpointPut(BaseModel):
    endpoint_key: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_protocol: str = "anthropic"
    max_input_tokens: int | None = Field(default=None, ge=1)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=2000)
    # ★ 刻意没有 token 字段：token 不进 DB、不经 API（§2.1）。


@router.put("/endpoints")
def put_endpoint(body: EndpointPut) -> JSONResponse:
    """新增或改端点的非敏感字段。改完运行进程会 refresh_endpoints()。"""
    return _guard(orgs.upsert_endpoint, endpoint_key=body.endpoint_key, base_url=body.base_url,
                  model=body.model, api_protocol=body.api_protocol,
                  max_input_tokens=body.max_input_tokens, enabled=body.enabled, note=body.note)


@router.delete("/endpoints/{endpoint_key}")
def delete_endpoint(endpoint_key: str) -> JSONResponse:
    return _guard(orgs.delete_endpoint, endpoint_key)
