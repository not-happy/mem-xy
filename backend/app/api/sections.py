"""词表端点（§7.1 的 `/v1/sections`，P6）。

★ 和 orgs.py / metrics.py 同构：这一层只翻译错误码、序列化、按需要 503 拦截。
  业务在 `app/sections.py`。

★ 503 拦截只加在**会算 embedding 的写路径**上（新增、改 label/aliases、手动重算）；
  纯读、只改 sort/enabled、删除都不碰模型，模型没热也照常工作。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import sections
from .memories import _guard, _model_ready

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


class SectionCreate(BaseModel):
    section_key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    aliases: list[str] | None = None
    applies_to: str = "org"
    org_key: str | None = None
    sort: int = 100
    enabled: bool = True


class SectionPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] | None = None
    applies_to: str | None = None
    org_key: str | None = None
    sort: int | None = None
    enabled: bool | None = None


@router.get("/sections")
def get_sections() -> JSONResponse:
    """全部栏位（含 aliases / 是否已算 embedding / 是否 stale）。纯读，不碰模型。"""
    return _guard(sections.overview)


@router.post("/sections")
def create_section(body: SectionCreate) -> JSONResponse:
    """新增栏位。★ 要当场算 embedding，所以模型没热就 503。"""
    blocked = _model_ready()
    if blocked:
        return blocked
    return _guard(sections.create_section, **body.model_dump())


@router.patch("/sections/{section_key}")
def patch_section(section_key: str, body: SectionPatch) -> JSONResponse:
    """改栏位。★ 只有动了 label/aliases（要重算 embedding）才 503 拦截；改 sort/enabled 不拦。"""
    data = body.model_dump(exclude_none=True)
    if "label" in data or "aliases" in data:
        blocked = _model_ready()
        if blocked:
            return blocked
    return _guard(sections.update_section, section_key, **data)


@router.delete("/sections/{section_key}")
def delete_section(section_key: str) -> JSONResponse:
    """删栏位。被引用就 409（服务层守卫）。纯 SQL，不碰模型。"""
    return _guard(sections.delete_section, section_key)


@router.post("/sections/recompute")
def recompute_sections() -> JSONResponse:
    """手动重算全表 match_embedding（绕过这一页直接改库后用）。要模型，503 拦截。"""
    blocked = _model_ready()
    if blocked:
        return blocked
    return _guard(sections.recompute)
