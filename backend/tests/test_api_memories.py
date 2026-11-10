"""`/v1/memories*` 与 `/v1/pending` 的 HTTP 层。

★ **不连库、不加载模型**：`library` / `review` 全部打桩。这一层只有三件事——校验形状、
翻译错误码、序列化——要验的正是这三件，而它们出错的症状是"用户看到 500 而不是 409"
"列表页拿到一堆不能 JSON 化的 datetime""last_error 把 token 吐到界面上"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import library
from app.api import memories as api
from app.api.main import app

client = TestClient(app)      # ★ 不用 with：lifespan 会开真连接池并触发 30s+ 的模型加载

NOW = datetime(2026, 10, 23, 8, 30, 15, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def model_ready(monkeypatch):
    """默认就绪；要测降级的那几条自己把它翻成 loading。"""
    _set_model(monkeypatch, ready=True)


def _set_model(monkeypatch, *, ready: bool, loading: bool | None = None):
    monkeypatch.setattr(api, "get_embedding_service",
                        lambda: SimpleNamespace(ready=ready, loading=loading if loading is not None else not ready))


def _stub(monkeypatch, name, result=None, error=None):
    calls: list[dict] = []

    def fake(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(library, name, fake)
    return calls


# ─────────────────── 模型没热：写路径一律 503，不排队等 ───────────────────

def test_creating_while_the_model_loads_returns_503_not_a_queue(monkeypatch):
    """★ 503 而不是 200 空/挂起：调用方（MCP、UI）靠这个状态码决定放弃，
    而挂起会让人以为"网络慢"，一直等着一个 30s 的冷启动。"""
    _set_model(monkeypatch, ready=False)
    calls = _stub(monkeypatch, "create_memory", result={"id": 1})

    response = client.post("/v1/memories", json={"content": "x"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_loading"
    assert calls == []


def test_an_edit_that_touches_content_is_gated_but_a_role_change_is_not(monkeypatch):
    _set_model(monkeypatch, ready=False)
    _stub(monkeypatch, "update_memory", result={"id": 7})

    assert client.patch("/v1/memories/7", json={"content": "新正文"}).status_code == 503
    assert client.patch("/v1/memories/7", json={"role": "pointer"}).status_code == 200


def test_a_correction_without_replacement_is_not_gated(monkeypatch):
    """没给替代文本 = 整条软删，不算向量，所以不该被模型状态挡住。"""
    _set_model(monkeypatch, ready=False)
    _stub(monkeypatch, "correct_memory", result={"action": "deleted"})

    response = client.post("/v1/memories/7/correct", json={"note": "不存在这回事"})

    assert response.status_code == 200


def test_reads_are_never_gated_on_the_model(monkeypatch):
    """列表/事件/待办全是纯 SQL；拿模型状态挡它们会把"能浏览"也一起废掉。"""
    _set_model(monkeypatch, ready=False)
    _stub(monkeypatch, "list_memories", result={"total": 0, "limit": 50, "offset": 0, "items": []})
    _stub(monkeypatch, "list_events", result=[])

    assert client.get("/v1/memories").status_code == 200
    assert client.get("/v1/memories/7/events").status_code == 200


# ─────────────────── 错误码翻译 ───────────────────

def test_a_library_error_keeps_its_own_status_and_code(monkeypatch):
    _stub(monkeypatch, "create_memory",
          error=library.LibraryError(409, "duplicate", "同桶里已经有一条 #3"))

    response = client.post("/v1/memories", json={"content": "x"})

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "duplicate" and "#3" in body["message"]


def test_an_unexpected_error_becomes_500_and_gets_redacted(monkeypatch):
    """★ httpx / psycopg 的异常字符串常带请求头和连接串，而这个响应体会进 UI。"""
    _stub(monkeypatch, "create_memory",
          error=RuntimeError("401 from gateway: Authorization: Bearer tok_live_9f3a"))

    response = client.post("/v1/memories", json={"content": "x"})

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "internal_error"
    assert "tok_live_9f3a" not in body["message"]
    assert "***REDACTED***" in body["message"]


def test_a_bad_scope_is_a_400_from_the_service_layer(monkeypatch):
    _stub(monkeypatch, "create_memory",
          error=library.LibraryError(400, "bad_scope", "scope 只能是 ('user','org','project')"))

    assert client.post("/v1/memories", json={"content": "x", "scope": "global"}).status_code == 400


def test_a_missing_memory_is_a_404_not_an_empty_body(monkeypatch):
    _stub(monkeypatch, "list_events", error=library.not_found("记忆", 999))

    response = client.get("/v1/memories/999/events")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_an_oversized_content_is_rejected_by_the_schema_before_any_sql(monkeypatch):
    calls = _stub(monkeypatch, "create_memory", result={"id": 1})

    response = client.post("/v1/memories", json={"content": "x" * 8001})

    assert response.status_code == 422 and calls == []


# ─────────────────── 序列化 ───────────────────

def test_datetimes_are_serialized_instead_of_blowing_up_the_response(monkeypatch):
    _stub(monkeypatch, "list_memories",
          result={"total": 1, "limit": 50, "offset": 0,
                  "items": [{"id": 1, "content": "x", "created_at": NOW}]})

    body = client.get("/v1/memories").json()

    assert body["items"][0]["created_at"] == "2026-10-23T08:30:15+00:00"


def test_the_actor_defaults_to_human_and_can_be_overridden(monkeypatch):
    """UI 上是人在点，MCP 那边传 `actor=model`。审计要分得出这两条路。"""
    calls = _stub(monkeypatch, "create_memory", result={"id": 1})

    client.post("/v1/memories", json={"content": "x"})
    client.post("/v1/memories?actor=model", json={"content": "x"})

    assert [c["actor"] for c in calls] == ["human", "model"]


# ─────────────────── 可见性与归属 ───────────────────

def test_listing_without_an_org_derives_the_bucket_from_cwd(monkeypatch):
    """调用方（MCP tool）只知道 cwd，不该要求它自己算 org/project。"""
    monkeypatch.setattr(api, "_attribute", lambda cwd, project_key: ("dida", "dida/gds-agg"))
    calls = _stub(monkeypatch, "list_memories", result={"total": 0, "limit": 50, "offset": 0, "items": []})

    client.get("/v1/memories", params={"cwd": "C:/work/gds-agg"})

    assert calls[0]["org_key"] == "dida" and calls[0]["project_key"] == "dida/gds-agg"


def test_all_scopes_skips_the_visibility_clip(monkeypatch):
    """★ 浏览全库那条路只读不注入，所以不必裁剪；但也因此绝不能顺手把 cwd 判成过滤条件。"""
    monkeypatch.setattr(api, "_attribute", lambda *a: (_ for _ in ()).throw(AssertionError("不该判归属")))
    calls = _stub(monkeypatch, "list_memories", result={"total": 0, "limit": 50, "offset": 0, "items": []})

    client.get("/v1/memories", params={"all_scopes": "true"})

    assert calls[0]["include_all_scopes"] is True and calls[0]["org_key"] is None


def test_delete_passes_the_restore_flag_through(monkeypatch):
    calls = _stub(monkeypatch, "delete_memory", result={"id": 7, "status": "active"})

    client.delete("/v1/memories/7", params={"restore": "true", "note": "删错了"})

    assert calls == [{"args": (7,), "note": "删错了", "actor": "human", "restore": True}]


# ─────────────────── 待办列表 ───────────────────

def test_pending_redacts_last_error_on_the_way_out(monkeypatch):
    from app.pipeline import review

    monkeypatch.setattr(review, "list_pending", lambda **kw: {
        "total": 1, "limit": 50, "offset": 0, "by_status": {"pending/route": 1},
        "items": [{"id": 5, "status": "failed", "stage": "route",
                   "last_error": "HTTPError: Authorization: Bearer tok_live_9f3a"}]})

    body = client.get("/v1/pending").json()

    assert "tok_live_9f3a" not in body["items"][0]["last_error"]


def test_pending_defaults_and_filters_reach_the_service(monkeypatch):
    from app.pipeline import review

    calls: list[dict] = []
    monkeypatch.setattr(review, "list_pending",
                        lambda **kw: calls.append(kw) or {"total": 0, "limit": 50, "offset": 0,
                                                          "by_status": {}, "items": []})

    client.get("/v1/pending", params={"stage": "route", "status": "failed", "limit": 5})

    assert calls == [{"stage": "route", "status": "failed", "org_key": None,
                      "limit": 5, "offset": 0}]


def test_a_pending_failure_is_a_500_with_a_redacted_message(monkeypatch):
    from app.pipeline import review

    monkeypatch.setattr(review, "list_pending",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("password=hunter2")))

    response = client.get("/v1/pending")

    assert response.status_code == 500
    assert "hunter2" not in response.json()["error"]["message"]
