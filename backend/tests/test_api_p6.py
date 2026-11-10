"""`/v1/pending/{id}/approve|reject` 与 `/v1/attribution*` 的 HTTP 层（P6）。

★ 和 test_api_docs.py 同构：**不连库、不加载模型**，服务层全打桩。
  这一层要验的还是那三件事——校验形状、翻译错误码（尤其是"抢不到 → 409"）、序列化。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import attribution
from app import metrics
from app import orgs
from app import replay
from app import sections
from app import settings_view
from app.api import memories as memories_api
from app.api.main import app
from app.library import LibraryError
from app.pipeline import review

client = TestClient(app)      # ★ 不用 with：lifespan 会开真连接池并触发 30s+ 的模型加载

NOW = datetime(2026, 10, 26, 8, 30, 15, tzinfo=timezone.utc)


def _stub(monkeypatch, module, name, result=None, error=None):
    calls: list[dict] = []

    def fake(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(module, name, fake)
    return calls


# ─────────────────── 待办裁决 ───────────────────

def test_approving_a_pending_item_returns_the_job_id(monkeypatch):
    calls = _stub(monkeypatch, review, "approve", result=9001)

    response = client.post("/v1/pending/7/approve",
                           json={"kind": "observation", "content": "禁止引入 Lombok"})

    assert response.status_code == 200
    assert response.json() == {"pending_id": 7, "job_id": 9001}
    assert calls[0]["args"][0] == 7
    assert calls[0]["args"][1]["kind"] == "observation"
    assert calls[0]["resolved_by"] == "human"


def test_approving_without_a_body_sends_an_empty_resolution(monkeypatch):
    calls = _stub(monkeypatch, review, "approve", result=1)

    response = client.post("/v1/pending/3/approve")

    assert response.status_code == 200
    assert calls[0]["args"][1] == {}


def test_approving_drops_none_fields_so_suggestion_wins(monkeypatch):
    calls = _stub(monkeypatch, review, "approve", result=1)

    client.post("/v1/pending/3/approve", json={"kind": "atomic_fact", "content": None})

    # content=None 不该进 resolution，否则会把模型建议里的 content 覆盖成空
    assert calls[0]["args"][1] == {"kind": "atomic_fact"}


def test_approving_an_already_handled_item_is_a_409(monkeypatch):
    _stub(monkeypatch, review, "approve", result=None)

    response = client.post("/v1/pending/7/approve", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_pending"


def test_rejecting_a_pending_item_reaches_the_service(monkeypatch):
    calls = _stub(monkeypatch, review, "reject", result=True)

    response = client.post("/v1/pending/7/reject", json={"note": "分类判错"})

    assert response.status_code == 200
    assert response.json() == {"pending_id": 7, "status": "rejected"}
    assert calls[0]["args"][0] == 7
    assert calls[0]["note"] == "分类判错"


def test_rejecting_without_a_body_is_fine(monkeypatch):
    calls = _stub(monkeypatch, review, "reject", result=True)

    response = client.post("/v1/pending/7/reject")

    assert response.status_code == 200
    assert calls[0]["note"] is None


def test_rejecting_an_already_handled_item_is_a_409(monkeypatch):
    _stub(monkeypatch, review, "reject", result=False)

    response = client.post("/v1/pending/7/reject", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_pending"


def test_a_service_error_becomes_a_redacted_500(monkeypatch):
    _stub(monkeypatch, review, "approve",
          error=RuntimeError("boom Authorization: Bearer tok_live_9f3a"))

    response = client.post("/v1/pending/7/approve", json={})

    assert response.status_code == 500
    assert "tok_live_9f3a" not in response.text


# ─────────────────── 归属管理 ───────────────────

def test_attribution_overview_passes_filters_and_serializes(monkeypatch):
    calls = _stub(monkeypatch, attribution, "overview", result={
        "rules": [{"id": 1, "pattern_type": "cwd_prefix", "pattern": "C:/x/",
                   "org_key": "dida", "project_key": None, "priority": 50, "enabled": True}],
        "workspaces": [{"cwd": "C:/x/y", "org_key": "dida", "project_key": "dida/y",
                        "match_rule_id": 1, "resolved_by": "rule", "enabled": True,
                        "resolved_at": NOW}],
        "by_source": {"rule": 1}, "orgs": [], "default_count": 0})

    response = client.get("/v1/attribution", params={"resolved_by": "default", "org_key": "dida"})

    assert response.status_code == 200
    body = response.json()
    assert body["workspaces"][0]["resolved_at"] == "2026-10-26T08:30:15+00:00"
    assert calls[0]["resolved_by"] == "default" and calls[0]["org_key"] == "dida"


def test_putting_a_workspace_reaches_the_service(monkeypatch):
    calls = _stub(monkeypatch, attribution, "set_workspace",
                  result={"cwd": "C:/x/y", "org_key": "dida", "project_key": "dida/y",
                          "resolved_by": "human", "memories_moved": 3})

    response = client.put("/v1/attribution", json={
        "cwd": "C:/x/y", "org_key": "dida", "project_key": "dida/y", "retroactive": True})

    assert response.status_code == 200
    assert response.json()["memories_moved"] == 3
    assert calls[0]["args"][0] == "C:/x/y"
    assert calls[0]["retroactive"] is True


def test_putting_a_workspace_defaults_to_retroactive(monkeypatch):
    calls = _stub(monkeypatch, attribution, "set_workspace", result={})

    client.put("/v1/attribution", json={"cwd": "C:/x/y", "org_key": "dida"})

    assert calls[0]["retroactive"] is True


def test_putting_a_workspace_without_cwd_is_a_422(monkeypatch):
    _stub(monkeypatch, attribution, "set_workspace", result={})

    response = client.put("/v1/attribution", json={"org_key": "dida"})

    assert response.status_code == 422


def test_a_library_error_keeps_its_own_status(monkeypatch):
    _stub(monkeypatch, attribution, "set_workspace",
          error=LibraryError(400, "bad_org", "org_key 不能为空"))

    response = client.put("/v1/attribution", json={"cwd": "C:/x", "org_key": "  "})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_org"


def test_creating_a_rule_omits_id(monkeypatch):
    calls = _stub(monkeypatch, attribution, "upsert_rule", result={"id": 9})

    response = client.put("/v1/attribution/rules", json={
        "pattern_type": "cwd_prefix", "pattern": "C:/new/", "org_key": "personal",
        "priority": 80})

    assert response.status_code == 200
    assert calls[0]["rule_id"] is None
    assert calls[0]["pattern_type"] == "cwd_prefix"


def test_updating_a_rule_passes_its_id(monkeypatch):
    calls = _stub(monkeypatch, attribution, "upsert_rule", result={"id": 2})

    client.put("/v1/attribution/rules", json={
        "id": 2, "pattern_type": "cwd_prefix", "pattern": "C:/x/", "org_key": "dida"})

    assert calls[0]["rule_id"] == 2


def test_deleting_a_rule_reaches_the_service(monkeypatch):
    calls = _stub(monkeypatch, attribution, "delete_rule", result={"deleted": 4})

    response = client.delete("/v1/attribution/rules/4")

    assert response.status_code == 200
    assert calls[0]["args"][0] == 4


def test_deleting_a_missing_rule_is_a_404(monkeypatch):
    _stub(monkeypatch, attribution, "delete_rule",
          error=LibraryError(404, "not_found", "归属规则 99 不存在"))

    response = client.delete("/v1/attribution/rules/99")

    assert response.status_code == 404


# ─────────────────── 指标 ───────────────────

def test_metrics_passes_org_and_windows_and_serializes(monkeypatch):
    calls = _stub(monkeypatch, metrics, "overview", result={
        "org_key": "dida",
        "suspend_rate": {"pending": 9, "total_routed": 16, "rate": 0.5625},
        "override_rate": {"by_kind": {}, "thresholds": {"atomic_fact": 0.85}},
        "rule_miss": {"method": "manual", "words": ["必须"], "candidate_count": 1, "sample": []},
        "latency": {"window_days": 30, "samples": 0, "p50_ms": None, "p95_ms": None},
        "corrections": {"by_kind": {}, "corrected": 0},
        "empty_sections": {"enabled_total": 22, "empty_count": 22, "empty": []},
        "section_pending_rate": {"section_pending": 1, "total_routed": 16, "rate": 0.0625},
        "review_throughput": {"window_days": 14, "backlog": 0, "new": 1, "resolved": 1,
                              "daily_new": 0.07, "daily_resolved": 0.07,
                              "net_backlog_growth": 0, "sustained_growth": False},
        "recall_eval": {"cases": 0, "status": "待测", "hit_at_5": None, "mrr_at_10": None}})

    response = client.get("/v1/metrics",
                          params={"org_key": "dida", "latency_window_days": 7,
                                  "throughput_window_days": 3, "sample_limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["suspend_rate"]["rate"] == 0.5625
    assert body["recall_eval"]["status"] == "待测"
    assert calls[0]["org_key"] == "dida"
    assert calls[0]["latency_window_days"] == 7
    assert calls[0]["throughput_window_days"] == 3
    assert calls[0]["sample_limit"] == 10


def test_metrics_defaults_apply_when_no_params(monkeypatch):
    calls = _stub(monkeypatch, metrics, "overview", result={"org_key": None})

    response = client.get("/v1/metrics")

    assert response.status_code == 200
    assert calls[0]["org_key"] is None
    assert calls[0]["latency_window_days"] == 30
    assert calls[0]["throughput_window_days"] == 14
    assert calls[0]["sample_limit"] == 30


def test_metrics_rejects_an_out_of_range_window(monkeypatch):
    _stub(monkeypatch, metrics, "overview", result={})

    # latency_window_days 上限 365，超了该被 Query 校验拦成 422，不进服务层
    response = client.get("/v1/metrics", params={"latency_window_days": 9999})

    assert response.status_code == 422


def test_metrics_service_error_becomes_a_redacted_500(monkeypatch):
    _stub(monkeypatch, metrics, "overview",
          error=RuntimeError("boom Authorization: Bearer tok_live_7c2d"))

    response = client.get("/v1/metrics")

    assert response.status_code == 500
    assert "tok_live_7c2d" not in response.text


# ─────────────────── 组织管理 ───────────────────

def test_orgs_overview_serializes(monkeypatch):
    _stub(monkeypatch, orgs, "overview", result={
        "orgs": [{"org_key": "dida", "name": "dida 机票", "is_personal": False,
                  "retention_days": None, "enabled": True, "created_at": NOW,
                  "memory_count": 3, "pending_count": 6}],
        "endpoints": [{"endpoint_key": "dida-gateway", "base_url": "http://x",
                       "model": "qwen", "api_protocol": "anthropic",
                       "max_input_tokens": 32000, "enabled": True, "note": None}],
        "default_endpoint": "dida-gateway", "task_endpoints": {}})

    response = client.get("/v1/orgs")

    assert response.status_code == 200
    body = response.json()
    assert body["orgs"][0]["created_at"] == "2026-10-26T08:30:15+00:00"
    assert body["orgs"][0]["memory_count"] == 3
    assert body["default_endpoint"] == "dida-gateway"


def test_creating_an_org_reaches_the_service(monkeypatch):
    calls = _stub(monkeypatch, orgs, "create_org", result={"org_key": "acme"})

    response = client.post("/v1/orgs", json={"org_key": "acme", "name": "Acme",
                                             "retention_days": 90})

    assert response.status_code == 200
    assert calls[0]["org_key"] == "acme"
    assert calls[0]["retention_days"] == 90
    assert calls[0]["is_personal"] is False


def test_creating_an_org_without_org_key_is_a_422(monkeypatch):
    _stub(monkeypatch, orgs, "create_org", result={})

    response = client.post("/v1/orgs", json={"name": "Acme"})

    assert response.status_code == 422


def test_patching_an_org_passes_only_given_fields(monkeypatch):
    calls = _stub(monkeypatch, orgs, "update_org", result={"org_key": "dida"})

    client.patch("/v1/orgs/dida", json={"enabled": False})

    assert calls[0]["args"][0] == "dida"
    assert calls[0]["enabled"] is False
    assert calls[0]["name"] is None       # 没给的字段传 None，服务层据此跳过


def test_deleting_an_in_use_org_keeps_its_409(monkeypatch):
    _stub(monkeypatch, orgs, "delete_org",
          error=LibraryError(409, "org_in_use", "org 'dida' 仍被引用"))

    response = client.delete("/v1/orgs/dida")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "org_in_use"


def test_updating_a_missing_org_is_a_404(monkeypatch):
    _stub(monkeypatch, orgs, "update_org",
          error=LibraryError(404, "not_found", "组织 nope 不存在"))

    response = client.patch("/v1/orgs/nope", json={"enabled": True})

    assert response.status_code == 404


def test_putting_an_endpoint_never_accepts_a_token_field(monkeypatch):
    calls = _stub(monkeypatch, orgs, "upsert_endpoint", result={"endpoint_key": "e"})

    response = client.put("/v1/endpoints", json={
        "endpoint_key": "e", "base_url": "http://x", "model": "m",
        "api_protocol": "openai", "max_input_tokens": 8000,
        "token": "tok_live_SHOULD_BE_IGNORED"})   # ★ 多余字段被 pydantic 丢掉，不进服务层

    assert response.status_code == 200
    assert calls[0]["api_protocol"] == "openai"
    assert "token" not in calls[0]
    assert "tok_live_SHOULD_BE_IGNORED" not in response.text


def test_putting_an_endpoint_with_a_bad_protocol_is_a_422_or_400(monkeypatch):
    # 服务层把非法协议翻成 400 bad_protocol
    _stub(monkeypatch, orgs, "upsert_endpoint",
          error=LibraryError(400, "bad_protocol", "api_protocol 只能是 ..."))

    response = client.put("/v1/endpoints", json={
        "endpoint_key": "e", "base_url": "http://x", "model": "m", "api_protocol": "grpc"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_protocol"


def test_deleting_a_default_endpoint_keeps_its_409(monkeypatch):
    _stub(monkeypatch, orgs, "delete_endpoint",
          error=LibraryError(409, "endpoint_in_use", "是默认端点，不能删"))

    response = client.delete("/v1/endpoints/dida-gateway")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "endpoint_in_use"


# ─────────────────── 设置（只读检视器） ───────────────────

_SETTINGS_BLOB = {
    "items": [{"group": "LLM", "label": "默认端点", "key": "llm.default_endpoint",
               "value": "dida-gateway", "source": "default"}],
    "thinking": [{"task": "route", "enabled": True, "source": "default"}],
    "task_endpoints": [],
    # ★ 服务层已 redact：这里放的是打码后的值，HTTP 层原样序列化，不该再出现明文口令
    "dsn": "host=127.0.0.1 dbname=memxy user=postgres password=***REDACTED***",
    "dsn_source": "secrets",
    "endpoints": [{"endpoint_key": "dida-gateway", "model": "m", "enabled": True,
                   "token_configured": True}],
    "route_confidence_thresholds": {"atomic_fact": 0.85},
    "paths": {"config_dir": "/x", "config_file": "/x/config.toml", "config_exists": False,
              "secrets_file": "/x/secrets.toml", "secrets_exists": True, "cache_dir": "/x/cache"},
    "restart_note": "改后重启",
}


def test_settings_overview_serializes_and_leaks_no_token(monkeypatch):
    _stub(monkeypatch, settings_view, "overview", result=_SETTINGS_BLOB)

    response = client.get("/v1/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["dsn"] == _SETTINGS_BLOB["dsn"]
    assert body["endpoints"][0]["token_configured"] is True
    # ★ 只报有无，token 值绝不出现；dsn 里的口令是打码态
    blob = response.text
    assert "tok_" not in blob and "sk-" not in blob
    assert "***REDACTED***" in blob


def test_settings_service_error_becomes_a_redacted_500(monkeypatch):
    _stub(monkeypatch, settings_view, "overview",
          error=RuntimeError("boom Authorization: Bearer tok_live_7c2d"))

    response = client.get("/v1/settings")

    assert response.status_code == 500
    assert "tok_live_7c2d" not in response.text


# ─────────────────── 词表（doc_sections） ───────────────────

_SECTIONS_BLOB = {
    "sections": [{"section_key": "local_env", "label": "本机环境与工具",
                  "aliases": ["JDK", "Maven"], "applies_to": "org", "org_key": "personal",
                  "sort": 10, "enabled": True, "has_embedding": True, "stale": False}],
    "total": 1, "enabled_count": 1, "missing_embedding": 0, "stale_count": 0,
    "applies_options": ["org", "project", "both"],
}


def _ready(monkeypatch, *, ready: bool = True):
    """词表写路径要算 embedding，API 层 503 拦截（`_model_ready` 在 memories.py）。
    测试里把那个模块的 `get_embedding_service` 换成假就绪/假 loading。"""
    monkeypatch.setattr(memories_api, "get_embedding_service",
                        lambda: SimpleNamespace(ready=ready, loading=not ready))


def test_sections_overview_serializes(monkeypatch):
    _stub(monkeypatch, sections, "overview", result=_SECTIONS_BLOB)

    response = client.get("/v1/sections")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["sections"][0]["section_key"] == "local_env"
    assert body["sections"][0]["aliases"] == ["JDK", "Maven"]


def test_creating_a_section_reaches_the_service(monkeypatch):
    _ready(monkeypatch)
    calls = _stub(monkeypatch, sections, "create_section",
                  result={"section_key": "s", "has_embedding": True})

    response = client.post("/v1/sections", json={
        "section_key": "s", "label": "L", "aliases": ["a", "b"], "applies_to": "org", "sort": 5})

    assert response.status_code == 200
    assert calls[0]["section_key"] == "s"
    assert calls[0]["aliases"] == ["a", "b"]
    assert calls[0]["sort"] == 5


def test_creating_a_section_without_a_ready_model_is_a_503(monkeypatch):
    # 模型没热：新增要当场算 embedding，应被 503 拦下，**不进服务层**
    _ready(monkeypatch, ready=False)
    calls = _stub(monkeypatch, sections, "create_section", result={})

    response = client.post("/v1/sections", json={"section_key": "s", "label": "L"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_loading"
    assert not calls


def test_patching_aliases_reaches_the_service(monkeypatch):
    _ready(monkeypatch)
    calls = _stub(monkeypatch, sections, "update_section",
                  result={"section_key": "local_env", "aliases": ["x"]})

    response = client.patch("/v1/sections/local_env", json={"aliases": ["x", "y"]})

    assert response.status_code == 200
    assert calls[0]["args"][0] == "local_env"
    assert calls[0]["aliases"] == ["x", "y"]
    assert "label" not in calls[0]      # 没给的字段不该传进服务层


def test_patching_only_sort_skips_the_model_gate(monkeypatch):
    # 只改 sort/enabled 不碰模型：即便模型没热也该放行（拦截只看 label/aliases）
    _ready(monkeypatch, ready=False)
    calls = _stub(monkeypatch, sections, "update_section", result={"section_key": "s"})

    response = client.patch("/v1/sections/s", json={"sort": 7, "enabled": False})

    assert response.status_code == 200
    assert calls[0]["sort"] == 7
    assert calls[0]["enabled"] is False


def test_deleting_a_referenced_section_keeps_its_409(monkeypatch):
    _stub(monkeypatch, sections, "delete_section",
          error=LibraryError(409, "section_in_use", "仍被引用"))

    response = client.delete("/v1/sections/business_boundary")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "section_in_use"


def test_updating_a_missing_section_is_a_404(monkeypatch):
    _ready(monkeypatch)
    _stub(monkeypatch, sections, "update_section",
          error=LibraryError(404, "not_found", "没找到"))

    response = client.patch("/v1/sections/nope", json={"label": "x"})

    assert response.status_code == 404


def test_recompute_reaches_the_service(monkeypatch):
    _ready(monkeypatch)
    calls = _stub(monkeypatch, sections, "recompute", result={"recomputed": 3})

    response = client.post("/v1/sections/recompute")

    assert response.status_code == 200
    assert response.json() == {"recomputed": 3}
    assert len(calls) == 1


# ─────────────────── 重跑（replay_runs） ───────────────────

_RUNS_BLOB = {"runs": [{"id": 1, "prompt_ver": "route-v2", "scope_filter": None,
                        "status": "awaiting_review", "created_at": NOW, "finished_at": None,
                        "action_counts": {"total": 2, "applied": 0, "pending": 2,
                                          "by_action": {"ADD": 1, "UPDATE": 1, "DELETE": 0, "NOOP": 0}}}]}


def test_replay_list_runs_serializes(monkeypatch):
    _stub(monkeypatch, replay, "list_runs", result=_RUNS_BLOB)

    response = client.get("/v1/replay/runs")

    assert response.status_code == 200
    body = response.json()
    assert body["runs"][0]["id"] == 1
    assert body["runs"][0]["action_counts"]["pending"] == 2
    # datetime 要序列化成字符串，不能原样进 JSONResponse
    assert body["runs"][0]["created_at"] == NOW.isoformat(timespec="seconds")


def test_replay_run_detail_serializes(monkeypatch):
    detail = {"id": 1, "status": "awaiting_review",
              "actions": [{"id": 5, "action": "UPDATE", "target_memory_id": 9,
                           "proposed_content": "新", "proposed_meta": {"role": "fact"},
                           "applied": False, "target_content": "旧", "target_status": "active",
                           "target_exists": True}]}
    _stub(monkeypatch, replay, "run_detail", result=detail)

    response = client.get("/v1/replay/runs/1")

    assert response.status_code == 200
    a = response.json()["actions"][0]
    assert a["target_content"] == "旧" and a["proposed_content"] == "新"


def test_replay_apply_reaches_the_service(monkeypatch):
    _ready(monkeypatch)
    calls = _stub(monkeypatch, replay, "apply_run",
                  result={"run_id": 1, "applied": 2, "run_status": "applied"})

    response = client.post("/v1/replay/runs/1/apply", json={})

    assert response.status_code == 200
    assert response.json() == {"run_id": 1, "applied": 2, "run_status": "applied"}
    assert calls[0]["args"][0] == 1
    assert calls[0]["action_ids"] is None      # 空 body = apply 全部未 applied


def test_replay_apply_passes_action_ids_subset(monkeypatch):
    _ready(monkeypatch)
    calls = _stub(monkeypatch, replay, "apply_run", result={"run_id": 1, "applied": 1})

    response = client.post("/v1/replay/runs/1/apply", json={"action_ids": [5, 6]})

    assert response.status_code == 200
    assert calls[0]["action_ids"] == [5, 6]


def test_replay_apply_without_a_ready_model_is_a_503(monkeypatch):
    # apply 里 ADD/UPDATE 要算 embedding：模型没热应被 503 拦下，**不进服务层**
    _ready(monkeypatch, ready=False)
    calls = _stub(monkeypatch, replay, "apply_run", result={})

    response = client.post("/v1/replay/runs/1/apply", json={})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_loading"
    assert not calls


def test_replay_apply_on_a_missing_run_is_a_404(monkeypatch):
    _ready(monkeypatch)
    _stub(monkeypatch, replay, "apply_run", error=LibraryError(404, "not_found", "没找到"))

    response = client.post("/v1/replay/runs/9999/apply", json={})

    assert response.status_code == 404


def test_replay_discard_reaches_the_service(monkeypatch):
    calls = _stub(monkeypatch, replay, "discard_run", result={"run_id": 2, "status": "discarded"})

    response = client.post("/v1/replay/runs/2/discard")

    assert response.status_code == 200
    assert response.json() == {"run_id": 2, "status": "discarded"}
    assert calls[0]["args"][0] == 2


def test_replay_discard_an_applied_run_keeps_its_409(monkeypatch):
    _stub(monkeypatch, replay, "discard_run",
          error=LibraryError(409, "run_applied", "已落地，不能丢弃"))

    response = client.post("/v1/replay/runs/1/discard")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_applied"
