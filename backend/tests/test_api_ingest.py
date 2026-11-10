"""`POST /v1/ingest/scan` 的两条路径。

★ 不 gated on DB：`jobs.enqueue` 和 `scan_and_queue` 都被桩掉，这里测的是**分流**本身。
  分流写错的症状很难查——Stop hook 走了同步路径的话，用户每轮对话结束都要等 15s
  （全量解析 195MB 的实测耗时），而 hook 的预算是 400ms。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import app
from app.queue import jobs as jobs_mod

client = TestClient(app)      # ★ 不用 with：lifespan 会开真连接池并触发 33s 的模型加载


def _stub_enqueue(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(jobs_mod, "enqueue",
                        lambda kind, payload, **kw: calls.append((kind, payload, kw)) or 42)
    return calls


def _stub_scan(monkeypatch, result=None, error=None):
    from app.ingest import scan as scan_mod

    calls: list[dict] = []

    def fake(**kw):
        calls.append(kw)
        if error is not None:
            raise error
        return result if result is not None else {"files": 0, "inserted": 0}

    monkeypatch.setattr(scan_mod, "scan_and_queue", fake)
    return calls


def test_the_default_path_queues_a_job_and_returns_202_immediately(monkeypatch):
    calls = _stub_enqueue(monkeypatch)
    _stub_scan(monkeypatch)

    response = client.post("/v1/ingest/scan")

    assert response.status_code == 202
    assert response.json() == {"queued": True, "job_id": 42,
                               "note": "扫描在队列里跑，`GET /v1/jobs` 或 worker 日志看结果"}
    assert calls == [("scan", {"include_subagents": False}, {"dedup_key": "scan:claude-code"})]


def test_the_dedup_key_is_stable_so_repeated_stops_do_not_pile_up(monkeypatch):
    """一轮对话结束可能连着触发好几次 Stop；扫描幂等，但排一堆一样的 job 只是白烧 CPU。"""
    calls = _stub_enqueue(monkeypatch)
    _stub_scan(monkeypatch)

    client.post("/v1/ingest/scan")
    client.post("/v1/ingest/scan")

    assert [c[2]["dedup_key"] for c in calls] == ["scan:claude-code"] * 2


def test_a_duplicate_enqueue_reports_queued_false(monkeypatch):
    monkeypatch.setattr(jobs_mod, "enqueue", lambda *a, **k: None)
    _stub_scan(monkeypatch)

    body = client.post("/v1/ingest/scan").json()

    assert body["queued"] is False and body["job_id"] is None


def test_the_async_path_never_touches_the_transcripts(monkeypatch):
    """★ 这是 hook 那条路的命门：一旦同步扫起来，每轮对话结束都要卡 15s。"""
    _stub_enqueue(monkeypatch)
    scans = _stub_scan(monkeypatch)

    client.post("/v1/ingest/scan")

    assert scans == []


def test_the_sync_path_runs_inline_and_returns_the_stats(monkeypatch):
    _stub_enqueue(monkeypatch)
    scans = _stub_scan(monkeypatch, result={"files": 29, "inserted": 155, "route_queued": 26})

    response = client.post("/v1/ingest/scan?sync=true")

    assert response.status_code == 200
    assert response.json()["route_queued"] == 26
    assert scans == [{"include_subagents": False}]


def test_the_sync_path_passes_include_subagents_through(monkeypatch):
    _stub_enqueue(monkeypatch)
    scans = _stub_scan(monkeypatch)

    client.post("/v1/ingest/scan?sync=true&include_subagents=true")

    assert scans == [{"include_subagents": True}]


def test_a_failing_sync_scan_returns_500_and_not_a_stack_trace(monkeypatch):
    _stub_enqueue(monkeypatch)
    _stub_scan(monkeypatch, error=RuntimeError("boom"))

    response = client.post("/v1/ingest/scan?sync=true")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "scan_failed"
    assert "boom" in body["error"]["message"]
