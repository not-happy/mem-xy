"""会话存档端点（`GET /v1/conversations`、`GET /v1/conversations/{id}/messages`）。

★ 和 metrics.py 同构的薄适配器：只校验形状、翻译错误码、序列化。
  原始对话是 ingest 落的留存数据（§3），这里只读不写。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import conversations
from .memories import _guard

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


@router.get("/conversations")
def list_conversations(
        org_key: str | None = None,
        limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
    return _guard(conversations.sessions, org_key=org_key, limit=limit)


@router.get("/conversations/{session_id}/messages")
def conversation_messages(
        session_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500)) -> JSONResponse:
    return _guard(conversations.messages, session_id, offset=offset, limit=limit)
