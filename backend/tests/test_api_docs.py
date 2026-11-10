"""`/v1/living-doc*`、`/v1/proposals*`、`/v1/observations*` 的 HTTP 层（P5c）。

★ **不连库、不加载模型**：`living_doc` / `proposals` / `summarize` 全部打桩。
  这一层只有三件事——校验形状、翻译错误码、序列化——要验的正是这三件，症状是
  "accept 返回 None 却给了 200""last_error 把 token 吐到界面上""datetime 不能 JSON 化"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import living_doc
from app.api import docs as api
from app.api.main import app
from app.library import LibraryError
from app.pipeline import proposals, summarize

client = TestClient(app)      # ★ 不用 with：lifespan 会开真连接池并触发 30s+ 的模型加载

NOW = datetime(2026, 10, 23, 8, 30, 15, tzinfo=timezone.utc)


def _stub(monkeypatch, module, name, result=None, error=None):
    calls: list[dict] = []

    def fake(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(module, name, fake)
    return calls


# ─────────────────── 成文规范：纯 SQL，不需要 503 拦截 ───────────────────

def test_living_doc_list_serializes_datetime_and_passes_filters(monkeypatch):
    calls = _stub(monkeypatch, living_doc, "tree", result={
        "total": 1, "limit": 200, "offset": 0,
        "items": [{"id": 5, "section_key": "dependency", "authority": "confirmed",
                   "version": 3, "updated_at": NOW, "label": "依赖管理"}]})

    response = client.get("/v1/living-doc",
                          params={"org_key": "dida", "authority": "confirmed"})

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["updated_at"] == "2026-10-23T08:30:15+00:00"
    assert calls[0]["org_key"] == "dida" and calls[0]["authority"] == "confirmed"


def test_living_doc_list_attributes_from_cwd_when_org_key_absent(monkeypatch):
    monkeypatch.setattr(api, "_attribute", lambda cwd, org, proj: ("dida", proj))
    calls = _stub(monkeypatch, living_doc, "tree",
                  result={"total": 0, "limit": 200, "offset": 0, "items": []})

    client.get("/v1/living-doc", params={"cwd": "/repo/x"})

    assert calls[0]["org_key"] == "dida"


def test_editing_a_doc_reaches_the_service_with_content_and_note(monkeypatch):
    calls = _stub(monkeypatch, living_doc, "edit", result={"id": 5, "version": 4})

    response = client.put("/v1/living-doc/5",
                          json={"content": "新正文", "note": "改错了"})

    assert response.status_code == 200
    assert calls[0]["args"] == (5, "新正文")
    assert calls[0]["note"] == "改错了" and calls[0]["changed_by"] == "human"


def test_editing_with_empty_content_is_a_422_from_pydantic(monkeypatch):
    _stub(monkeypatch, living_doc, "edit", result={"id": 5})

    # min_length=1 → FastAPI 在进 handler 前就挡下，回 422，不碰服务层。
    assert client.put("/v1/living-doc/5", json={"content": ""}).status_code == 422


def test_authority_patch_reaches_the_service(monkeypatch):
    calls = _stub(monkeypatch, living_doc, "set_authority", result={"id": 5, "authority": "pinned"})

    response = client.patch("/v1/living-doc/5/authority", json={"authority": "pinned"})

    assert response.status_code == 200
    assert calls[0]["args"] == (5, "pinned")


def test_a_library_error_becomes_its_own_4xx(monkeypatch):
    """not_found / bad_authority 这类业务错，_guard 要翻成它自带的状态码，不是 500。"""
    _stub(monkeypatch, living_doc, "get",
          error=LibraryError(404, "not_found", "成文规范 99 不存在"))

    response = client.get("/v1/living-doc/99")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_an_unexpected_error_becomes_500_and_gets_redacted(monkeypatch):
    _stub(monkeypatch, living_doc, "get",
          error=RuntimeError("boom: Authorization: Bearer tok_live_9f3a"))

    response = client.get("/v1/living-doc/5")

    assert response.status_code == 500
    assert "tok_live_9f3a" not in response.text


def test_history_is_returned_as_a_list(monkeypatch):
    _stub(monkeypatch, living_doc, "history", result=[
        {"version": 2, "new_authority": "confirmed", "changed_by": "human", "created_at": NOW}])

    body = client.get("/v1/living-doc/5/history").json()

    assert body[0]["created_at"] == "2026-10-23T08:30:15+00:00"


# ─────────────────── 提案裁决：accept/reject 返回 job id 或 None ───────────────────

def test_accept_returns_the_job_id(monkeypatch):
    calls = _stub(monkeypatch, proposals, "accept", result=42)

    response = client.post("/v1/proposals/7/accept", json={"content": "手改后采纳"})

    assert response.status_code == 200
    assert response.json() == {"proposal_id": 7, "job_id": 42}
    assert calls[0]["args"] == (7, "手改后采纳")


def test_accept_without_a_body_still_works(monkeypatch):
    calls = _stub(monkeypatch, proposals, "accept", result=42)

    assert client.post("/v1/proposals/7/accept").status_code == 200
    assert calls[0]["args"] == (7, None)


def test_accepting_an_already_processed_proposal_is_a_409(monkeypatch):
    """★ accept 返回 None = 提案已不是 pending（被人抢先）。给 200 会让前端以为排到 job 了。"""
    _stub(monkeypatch, proposals, "accept", result=None)

    response = client.post("/v1/proposals/7/accept")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_pending"


def test_reject_returns_the_job_id(monkeypatch):
    calls = _stub(monkeypatch, proposals, "reject", result=43)

    response = client.post("/v1/proposals/7/reject")

    assert response.status_code == 200
    assert response.json() == {"proposal_id": 7, "job_id": 43}
    assert calls[0]["args"] == (7,)


def test_rejecting_an_already_processed_proposal_is_a_409(monkeypatch):
    _stub(monkeypatch, proposals, "reject", result=None)

    assert client.post("/v1/proposals/7/reject").status_code == 409


def test_proposals_list_redacts_last_error_on_the_way_out(monkeypatch):
    _stub(monkeypatch, proposals, "list_proposals", result={
        "total": 1, "limit": 50, "offset": 0,
        "items": [{"id": 7, "status": "pending", "job_status": "failed",
                   "last_error": "HTTPError: Authorization: Bearer tok_live_9f3a"}]})

    body = client.get("/v1/proposals").json()

    assert "tok_live_9f3a" not in body["items"][0]["last_error"]


def test_single_proposal_redacts_last_error(monkeypatch):
    _stub(monkeypatch, proposals, "get_proposal", result={
        "id": 7, "status": "rejected",
        "last_error": "HTTPError: Authorization: Bearer tok_live_9f3a"})

    body = client.get("/v1/proposals/7").json()

    assert "tok_live_9f3a" not in body["last_error"]


def test_proposals_list_passes_status_and_partition(monkeypatch):
    calls = _stub(monkeypatch, proposals, "list_proposals",
                  result={"total": 0, "limit": 50, "offset": 0, "items": []})

    client.get("/v1/proposals", params={"status": "accepted", "org_key": "dida",
                                        "section_key": "dependency"})

    assert calls[0]["status"] == "accepted" and calls[0]["org_key"] == "dida"
    assert calls[0]["section_key"] == "dependency"


# ─────────────────── 观察池：列表 / 手动汇总 / 撤销否决 ───────────────────

def test_observations_list_passes_filters(monkeypatch):
    calls = _stub(monkeypatch, summarize, "list_observations",
                  result={"total": 0, "limit": 50, "offset": 0, "items": []})

    client.get("/v1/observations", params={"org_key": "dida", "polarity": "negative",
                                           "consumed": "false"})

    assert calls[0]["polarity"] == "negative" and calls[0]["consumed"] is False


def test_trigger_summarize_enqueues_a_job_and_returns_its_id(monkeypatch):
    calls = _stub(monkeypatch, summarize, "enqueue_summarize", result=99)

    response = client.post("/v1/observations/summarize",
                           json={"org_key": "dida", "section_key": "dependency", "force": True})

    assert response.status_code == 200
    assert response.json() == {"job_id": 99}
    assert calls[0]["org_key"] == "dida" and calls[0]["force"] is True


def test_trigger_summarize_without_a_body_defaults_to_the_callers_partition(monkeypatch):
    """没给分区 → 走 cwd 归属判定，回落到 `personal`（而不是全库多租户扫描）。"""
    calls = _stub(monkeypatch, summarize, "enqueue_summarize", result=99)

    assert client.post("/v1/observations/summarize").status_code == 200
    assert calls[0]["org_key"] == "personal" and calls[0]["force"] is False


def test_revoking_a_negative_reaches_the_service(monkeypatch):
    calls = _stub(monkeypatch, summarize, "revoke_negative",
                  result={"action": "revoked", "observation_id": 3})

    response = client.delete("/v1/observations/3/negative")

    assert response.status_code == 200
    assert calls[0]["args"] == (3,)


def test_revoking_a_non_veto_is_a_400_not_a_500(monkeypatch):
    _stub(monkeypatch, summarize, "revoke_negative",
          error=LibraryError(400, "not_a_veto", "观察 3 是 positive，不是否决项"))

    response = client.delete("/v1/observations/3/negative")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "not_a_veto"
