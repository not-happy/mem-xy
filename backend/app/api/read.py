"""读路径端点（§7.1）：SessionStart 稳定层 + 每轮召回 + 召回审计。

★ 这些端点全部在 **400ms 预算**里跑（hook 侧超时就是这个数，§2.4）。所以：
  - 不在这里调 LLM，一个 token 都不花；
  - 不在这里加载模型——没预热完就回 `503 model_loading`，hook 收到即降级走本地快照缓存，
    绝不阻塞对话（§4.3.3）；
  - `bootstrap` 连向量都不用，纯 SQL，所以它**不需要**等模型就绪。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..core import db
from ..core.config import get_settings
from ..core.embedding import get_embedding_service
from ..core.redact import redact
from ..retrieve import bootstrap as bootstrap_mod
from ..retrieve import recall as recall_mod

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


def _error(status: int, code: str, message: str, **detail) -> JSONResponse:
    """★ message/detail 返回前必须过 redact（§2.1）：psycopg 的异常字符串带连接串，
    httpx 的带请求头，而这个响应体会被 hook 打日志、被 UI 展示。"""
    return JSONResponse(
        {"error": {"code": code, "message": redact(message)[:500],
                   "detail": {k: redact(str(v))[:200] for k, v in detail.items()}}},
        status_code=status)


class RecallRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    session_id: str = "anonymous"
    cwd: str | None = None
    # ★ org_key 显式给了就不走 cwd 判定：评测 runner 要能指定分区，
    #   而它跑在 repo 外面，attribution 会把它判成 personal。
    org_key: str | None = None
    project_key: str | None = None
    prev_assistant_summary: str | None = Field(default=None, max_length=2000)
    turn_no: int | None = None
    k: int | None = Field(default=None, ge=1, le=50)
    exclude_ids: list[int] = Field(default_factory=list)
    source: str | None = None


def _attribute(cwd: str | None, org_key: str | None, project_key: str | None) -> tuple:
    if org_key is not None:
        return org_key, project_key
    attribution = recall_mod.attribute(cwd)
    return attribution.org_key, attribution.project_key


@router.get("/context/bootstrap")
def context_bootstrap(cwd: str | None = None, org_key: str | None = None,
                      project_key: str | None = None) -> JSONResponse:
    """SessionStart 稳定层。**不需要模型就绪**——纯 SQL。"""
    try:
        org, project = _attribute(cwd, org_key, project_key)
        boot = bootstrap_mod.build(org_key=org, project_key=project)
    except Exception as exc:  # noqa: BLE001 — hook 那边只认状态码，不能吐 traceback
        log.exception("bootstrap failed")
        return _error(500, "bootstrap_failed", str(exc))

    payload = boot.as_dict()
    payload["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return JSONResponse(payload)


@router.post("/recall")
def recall(body: RecallRequest) -> JSONResponse:
    return _do_recall(body, debug=False)


@router.post("/recall/debug")
def recall_debug(body: RecallRequest) -> JSONResponse:
    """召回调试页用：多返回 query 增广后的文本、抽出来的关键词、每条的两路名次。
    ★ 不写 `recall_log`——调试召回不是真注入，混进审计会让 UI 上的历史失真。"""
    return _do_recall(body, debug=True)


def _do_recall(body: RecallRequest, *, debug: bool) -> JSONResponse:
    service = get_embedding_service()
    if not service.ready:
        # ★ 503 而不是 200 空列表：hook 靠这个状态码决定去读本地快照缓存（§4.3.3）。
        #   回 200 空列表的话，hook 会把"模型还在加载"当成"没有相关记忆"，
        #   然后把这个错误的空结果写进缓存，把好的那份盖掉。
        return _error(503, "model_loading",
                      f"embedding 未就绪（loading={service.loading}）")
    try:
        result = recall_mod.recall(
            query=body.query, cwd=body.cwd, org_key=body.org_key,
            project_key=body.project_key, session_id=body.session_id,
            turn_no=body.turn_no, prev_assistant_summary=body.prev_assistant_summary,
            k=body.k, exclude_ids=body.exclude_ids, source=body.source,
            write_log=not debug)
    except Exception as exc:  # noqa: BLE001
        log.exception("recall failed")
        return _error(500, "recall_failed", str(exc))

    settings = get_settings()
    payload = {
        "items": [h.as_dict() for h in result.items],
        "latency_ms": result.latency_ms,
        "recall_log_id": result.recall_log_id,
    }
    if debug:
        payload.update(query_text=result.query_text, keywords=result.keywords,
                       org_key=result.org_key, project_key=result.project_key,
                       budget_ms=settings.hook_timeout_ms,
                       over_budget=result.latency_ms > settings.hook_timeout_ms)
    return JSONResponse(payload)


@router.get("/recall-log")
def recall_log(limit: int = Query(default=50, ge=1, le=500),
               session_id: str | None = Query(default=None)) -> JSONResponse:
    """召回审计。★ 只存 id + 分数，正文要现查 `memories`——
    把内容抄一份进日志就是第二份真相源，改了就对不上。"""
    sql = ("SELECT r.id, r.session_id, r.turn_no, r.query_text, r.org_key, r.project_key,"
           "        r.hits, r.injected_ids, r.latency_ms, r.created_at"
           "  FROM recall_log r")
    params: dict = {"limit": limit}
    if session_id:
        sql += " WHERE r.session_id = %(session)s"
        params["session"] = session_id
    sql += " ORDER BY r.id DESC LIMIT %(limit)s"
    rows = db.query(sql, params)

    # ★ 命中与注入的正文都现查 memories（日志只存 id + 分数，抄正文就是第二份真相源）。
    #   命中里没被注入的候选也要解析——UI 详情靠它看"召回到了为什么没进 prompt"。
    wanted = {mid for row in rows for mid in row["injected_ids"]}
    wanted |= {h.get("id") for row in rows for h in (row["hits"] or []) if h.get("id")}
    texts = {r["id"]: r["content"] for r in db.query(
        "SELECT id, content, status FROM memories WHERE id = ANY(%(ids)s::bigint[])",
        {"ids": list(wanted)})} if wanted else {}
    return JSONResponse([
        {**{k: (v.isoformat(timespec="seconds") if isinstance(v, datetime) else v)
              for k, v in row.items() if k not in ("injected_ids", "hits")},
         "hits": [{**h, "text": texts.get(h.get("id"), "（已删除或不存在）")}
                  for h in (row["hits"] or [])],
         "injected": [{"id": mid, "text": texts.get(mid, "（已删除或不存在）")}
                      for mid in row["injected_ids"]]}
        for row in rows
    ])
