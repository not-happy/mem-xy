"""成文规范 / 提案 / 观察池端点（§7.1，P5c）。

★ 和 `memories.py` 同构：这一层只做三件事——校验形状、翻译错误码、序列化。
  业务规则全在 `app/living_doc.py`、`app/pipeline/proposals.py`、`app/pipeline/summarize.py`。

★ **没有一个端点需要 503 拦截**：
  - 成文规范的读写是纯 SQL（`living_doc` 没有 embedding 列，见 living_doc.py 顶部）；
  - 提案的 accept/reject 只翻状态 + 排 job，embedding 在 worker 里算（#86），不堵 HTTP；
  - 手动汇总也是排 job（合成要调 LLM，几十秒），同步跑会把请求堵死。
  所以这里连 `get_embedding_service` 都不用 import。

★ accept/reject 返回 `None` = 提案已不是 pending（被人抢先处理了），翻成 **409**：
  前端拿到 409 就该刷新列表，而不是把"没排到 job"当成成功。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import living_doc
from ..core.redact import redact
from ..library import LibraryError
from ..pipeline import proposals, summarize
from .memories import _guard, _jsonable
from .read import _error

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


def _attribute(cwd: str | None, org_key: str | None,
               project_key: str | None) -> tuple[str | None, str | None]:
    """和 memories.py 一致：显式给了 org_key 就照用，否则从 cwd 判归属。"""
    if org_key is not None:
        return org_key, project_key
    from ..retrieve import recall as recall_mod

    attribution = recall_mod.attribute(cwd)
    return attribution.org_key, project_key or attribution.project_key


# ─────────────────────────── 成文规范 ───────────────────────────

class DocEdit(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    note: str | None = Field(default=None, max_length=2000)
    changed_by: str = "human"


class AuthorityPatch(BaseModel):
    authority: str


@router.get("/living-doc")
def list_living_doc(
        cwd: str | None = None, org_key: str | None = None,
        project_key: str | None = None, section_key: str | None = None,
        authority: str | None = None, include_projects: bool = True,
        limit: int = Query(default=200, ge=1, le=500),
        offset: int = Query(default=0, ge=0)) -> JSONResponse:
    org, project = _attribute(cwd, org_key, project_key)
    return _guard(living_doc.tree, org_key=org, project_key=project, section_key=section_key,
                  authority=authority, include_projects=include_projects,
                  limit=limit, offset=offset)


@router.get("/living-doc/{doc_id}")
def get_living_doc(doc_id: int) -> JSONResponse:
    return _guard(living_doc.get, doc_id)


@router.put("/living-doc/{doc_id}")
def edit_living_doc(doc_id: int, body: DocEdit) -> JSONResponse:
    return _guard(living_doc.edit, doc_id, body.content, note=body.note,
                  changed_by=body.changed_by)


@router.patch("/living-doc/{doc_id}/authority")
def patch_living_doc_authority(doc_id: int, body: AuthorityPatch) -> JSONResponse:
    return _guard(living_doc.set_authority, doc_id, body.authority)


@router.get("/living-doc/{doc_id}/history")
def living_doc_history(doc_id: int,
                       limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
    return _guard(living_doc.history, doc_id, limit)


# ─────────────────────────── 提案裁决 ───────────────────────────

class AcceptBody(BaseModel):
    # 空 = 原文采纳；非空 = 人改过再采纳（changed_by 会记成 human）。
    content: str | None = Field(default=None, max_length=20000)


def _adjudicate(job_id: int | None, proposal_id: int, verb: str) -> JSONResponse:
    """accept/reject 都返回 job id 或 None。None = 提案已不是 pending → 409。"""
    if job_id is None:
        return _error(409, "not_pending",
                      f"提案 {proposal_id} 已不是待处理状态（可能已被{verb}），请刷新列表")
    return JSONResponse(_jsonable({"proposal_id": proposal_id, "job_id": job_id}))


@router.get("/proposals")
def list_proposals(
        cwd: str | None = None, org_key: str | None = None,
        project_key: str | None = None, section_key: str | None = None,
        status: str | None = "pending",
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0)) -> JSONResponse:
    org, project = _attribute(cwd, org_key, project_key)
    payload = proposals.list_proposals(status=status, org_key=org, project_key=project,
                                       section_key=section_key, limit=limit, offset=offset)
    # last_error 会在 UI 里显示，而它可能带 httpx 请求头（§2.1）；落库前已 redact 过一道，
    # 这里再过一道是因为它便宜、而泄 token 是 bug。
    for item in payload["items"]:
        if item.get("last_error"):
            item["last_error"] = redact(item["last_error"])
    return JSONResponse(_jsonable(payload))


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int) -> JSONResponse:
    try:
        row = proposals.get_proposal(proposal_id)
    except LibraryError as exc:
        return _error(exc.status, exc.code, exc.message)
    if row.get("last_error"):
        row["last_error"] = redact(row["last_error"])
    return JSONResponse(_jsonable(row))


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(proposal_id: int, body: AcceptBody | None = None) -> JSONResponse:
    content = body.content if body else None
    try:
        job_id = proposals.accept(proposal_id, content)
    except Exception as exc:  # noqa: BLE001
        log.exception("accept_proposal failed")
        return _error(500, "internal_error", str(exc))
    return _adjudicate(job_id, proposal_id, "采纳或否决")


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int) -> JSONResponse:
    try:
        job_id = proposals.reject(proposal_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("reject_proposal failed")
        return _error(500, "internal_error", str(exc))
    return _adjudicate(job_id, proposal_id, "采纳或否决")


# ─────────────────────────── 观察池 ───────────────────────────

class SummarizeBody(BaseModel):
    cwd: str | None = None
    org_key: str | None = None
    project_key: str | None = None
    section_key: str | None = None
    force: bool = False


@router.get("/observations")
def list_observations(
        cwd: str | None = None, org_key: str | None = None,
        project_key: str | None = None, section_key: str | None = None,
        polarity: str | None = None, consumed: bool | None = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0)) -> JSONResponse:
    org, project = _attribute(cwd, org_key, project_key)
    return _guard(summarize.list_observations, org_key=org, project_key=project,
                  section_key=section_key, polarity=polarity, consumed=consumed,
                  limit=limit, offset=offset)


@router.post("/observations/summarize")
def trigger_summarize(body: SummarizeBody | None = None) -> JSONResponse:
    """手动排一个汇总 job。★ 不在 HTTP 里同步跑——合成要调 LLM，几十秒。"""
    body = body or SummarizeBody()
    org, project = _attribute(body.cwd, body.org_key, body.project_key)
    try:
        job_id = summarize.enqueue_summarize(org_key=org, project_key=project,
                                             section_key=body.section_key, force=body.force)
    except Exception as exc:  # noqa: BLE001
        log.exception("trigger_summarize failed")
        return _error(500, "internal_error", str(exc))
    return JSONResponse(_jsonable({"job_id": job_id}))


@router.delete("/observations/{observation_id}/negative")
def revoke_negative(observation_id: int) -> JSONResponse:
    """把一条否决从否决记录里撤出来（当初否决错了）。只删 negative，删错 polarity 回 400。"""
    return _guard(summarize.revoke_negative, observation_id)
