"""记忆库与待办端点（§7.1）。★ 这一层只做三件事：校验形状、翻译错误码、序列化。
业务规则全在 `app/library.py` 和 `app/pipeline/review.py` 里——MCP server 是同一批
规则的另一个适配器，逻辑写在这儿就会长出两份。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import library
from ..core.embedding import get_embedding_service
from ..core.redact import redact
from ..pipeline import review
from .read import _error

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


def _jsonable(value):
    """psycopg 回来的 datetime 不能直接进 JSONResponse。"""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


def _guard(callable_, *args, **kwargs) -> JSONResponse:
    """把 `LibraryError` 翻成它自带的 4xx，其余异常一律 500 + redact（§2.1）。"""
    try:
        return JSONResponse(_jsonable(callable_(*args, **kwargs)))
    except library.LibraryError as exc:
        return _error(exc.status, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        log.exception("memory endpoint failed")
        return _error(500, "internal_error", str(exc))


def _model_ready() -> JSONResponse | None:
    """写路径要算 embedding，所以和 `/recall` 一样：模型没热就回 503，不排长队等。"""
    service = get_embedding_service()
    if service.ready:
        return None
    return _error(503, "model_loading", f"embedding 未就绪（loading={service.loading}）")


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    scope: str = "project"
    subject: str | None = None
    role: str = "fact"
    keywords: list[str] | None = None
    why: str | None = Field(default=None, max_length=2000)
    cwd: str | None = None
    org_key: str | None = None
    project_key: str | None = None


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=8000)
    role: str | None = None
    scope: str | None = None
    subject: str | None = None
    keywords: list[str] | None = None
    why: str | None = Field(default=None, max_length=2000)
    cwd: str | None = None


class MemoryCorrect(BaseModel):
    correction: str | None = Field(default=None, max_length=8000)
    note: str | None = Field(default=None, max_length=2000)
    actor: str = "model"


class FeedbackCreate(BaseModel):
    kind: str
    note: str | None = Field(default=None, max_length=2000)
    recall_log_id: int | None = None


@router.get("/memories")
def list_memories(
        org_key: str | None = None, project_key: str | None = None,
        scope: str | None = None, subject: str | None = None, role: str | None = None,
        status: str | None = Query(default="active"), q: str | None = None,
        cwd: str | None = None, all_scopes: bool = False,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0)) -> JSONResponse:
    """★ 默认按 §6.1 的可见性裁剪（user 穿透 + 本 org + 本 project）。
    `all_scopes=true` 是给"浏览全库"用的——那条路只读不注入，所以不需要裁剪。"""
    if org_key is None and not all_scopes:
        org_key, project_key = _attribute(cwd, project_key)
    return _guard(library.list_memories,
                  org_key=org_key, project_key=project_key, scope=scope, subject=subject,
                  role=role, status=status, q=q, include_all_scopes=all_scopes,
                  limit=limit, offset=offset)


def _attribute(cwd: str | None, project_key: str | None) -> tuple[str | None, str | None]:
    from ..retrieve import recall as recall_mod

    attribution = recall_mod.attribute(cwd)
    return attribution.org_key, project_key or attribution.project_key


@router.post("/memories")
def create_memory(body: MemoryCreate, actor: str = "human") -> JSONResponse:
    return _model_ready() or _guard(
        library.create_memory, content=body.content, scope=body.scope, subject=body.subject,
        role=body.role, keywords=body.keywords, why=body.why, cwd=body.cwd,
        org_key=body.org_key, project_key=body.project_key, actor=actor)


@router.patch("/memories/{memory_id}")
def patch_memory(memory_id: int, body: MemoryPatch, actor: str = "human") -> JSONResponse:
    if body.content is not None or body.scope is not None:
        blocked = _model_ready()
        if blocked is not None:
            return blocked
    return _guard(library.update_memory, memory_id, content=body.content, role=body.role,
                  scope=body.scope, subject=body.subject, keywords=body.keywords,
                  why=body.why, cwd=body.cwd, actor=actor)


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: int, note: str | None = None, actor: str = "human",
                  restore: bool = False) -> JSONResponse:
    """软删。`restore=true` 走同一条路撤销（`memory_events.action='RESTORE'`）。"""
    return _guard(library.delete_memory, memory_id, note=note, actor=actor, restore=restore)


@router.post("/memories/{memory_id}/correct")
def correct_memory(memory_id: int, body: MemoryCorrect) -> JSONResponse:
    """§7.2 的 `memory_correct`。★ 单独一个端点是因为它必须**原子**：
    改内容 + 写 feedback 分两次调用的话，中间断了就只留下一半痕迹。"""
    if body.correction and body.correction.strip():
        blocked = _model_ready()
        if blocked is not None:
            return blocked
    return _guard(library.correct_memory, memory_id, correction=body.correction,
                  note=body.note, actor=body.actor)


@router.post("/memories/{memory_id}/feedback")
def add_feedback(memory_id: int, body: FeedbackCreate) -> JSONResponse:
    return _guard(library.add_feedback, memory_id, kind=body.kind, note=body.note,
                  recall_log_id=body.recall_log_id)


@router.get("/memories/{memory_id}/events")
def memory_events(memory_id: int, limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
    return _guard(library.list_events, memory_id, limit=limit)


@router.get("/pending")
def list_pending(stage: str | None = None, status: str | None = None,
                 org_key: str | None = None,
                 limit: int = Query(default=50, ge=1, le=500),
                 offset: int = Query(default=0, ge=0)) -> JSONResponse:
    try:
        payload = review.list_pending(stage=stage, status=status, org_key=org_key,
                                      limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        log.exception("list_pending failed")
        return _error(500, "internal_error", str(exc))
    # ★ `last_error` 会在 UI 里显示，而 httpx 的异常字符串常带请求头（§2.1）。
    #   落库前已经 redact 过一道，这里再过一道是因为它便宜、而泄 token 是 bug。
    for item in payload["items"]:
        if item.get("last_error"):
            item["last_error"] = redact(item["last_error"])
    return JSONResponse(_jsonable(payload))


class PendingApprove(BaseModel):
    """人在「待确认」卡片上改过的最终裁决（§5.4）。全部可选：给了就覆盖模型建议，
    没给的字段由 `apply_decision` 从 `suggestion` 里补。`kind` 预选 `observation`，
    人可改回 `atomic_fact`（当零碎事实存）或 `discard`（丢弃）。"""
    kind: str | None = None
    content: str | None = Field(default=None, max_length=8000)
    scope: str | None = None
    subject: str | None = None
    role: str | None = None
    section_key: str | None = None
    keywords: list[str] | None = None
    why: str | None = Field(default=None, max_length=2000)


class PendingReject(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


@router.post("/pending/{pending_id}/approve")
def approve_pending(pending_id: int, body: PendingApprove | None = None,
                    resolved_by: str = "human") -> JSONResponse:
    """抢占式批准（§5.7）：返回排出去的 job id；抢不到（已被别处处理）→ 409。
    ★ 不同步落库——写入要算 embedding / 可能调 LLM，全在 worker 里跑（apply_decision）。"""
    resolution = body.model_dump(exclude_none=True) if body else {}
    try:
        job_id = review.approve(pending_id, resolution, resolved_by=resolved_by)
    except Exception as exc:  # noqa: BLE001
        log.exception("approve_pending failed")
        return _error(500, "internal_error", str(exc))
    if job_id is None:
        return _error(409, "not_pending",
                      f"待办 {pending_id} 已不是 pending（可能已被处理），请刷新列表")
    return JSONResponse(_jsonable({"pending_id": pending_id, "job_id": job_id}))


@router.post("/pending/{pending_id}/reject")
def reject_pending(pending_id: int, body: PendingReject | None = None,
                   resolved_by: str = "human") -> JSONResponse:
    """第一道审核否决：只意味着"这不是规矩线索"，不写任何记忆、不进否决记录（review.reject 注释）。
    抢不到 → 409。"""
    note = body.note if body else None
    try:
        ok = review.reject(pending_id, note=note, resolved_by=resolved_by)
    except Exception as exc:  # noqa: BLE001
        log.exception("reject_pending failed")
        return _error(500, "internal_error", str(exc))
    if not ok:
        return _error(409, "not_pending",
                      f"待办 {pending_id} 已不是 pending（可能已被处理），请刷新列表")
    return JSONResponse(_jsonable({"pending_id": pending_id, "status": "rejected"}))
