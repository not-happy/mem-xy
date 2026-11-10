"""/healthz must answer on both branches — the 503 path worked while the 200 path crashed,
because FastAPI's injected `Response.status_code` is None until the framework sets it.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Not used as a context manager on purpose: TestClient only runs the lifespan on
    __enter__, and the real one opens a DB pool and starts a ~30s model load."""
    from app.api.main import app

    return TestClient(app)


def _stub_service(monkeypatch, *, ready: bool, loading: bool = False):
    class Stats:
        max_seq_length = 128 if ready else 0
        modules = "Transformer,Pooling,Normalize" if ready else ""
        sparse_available = False
        load_seconds = 29.6 if ready else 0.0
        avg_ms_per_row = 22.0 if ready else 0.0

    class Service:
        stats = Stats()

        @property
        def ready(self):
            return ready

        @property
        def loading(self):
            return loading

    monkeypatch.setattr("app.api.main.get_embedding_service", lambda: Service())


def test_returns_503_while_the_model_is_loading(client, monkeypatch):
    _stub_service(monkeypatch, ready=False, loading=True)
    monkeypatch.setattr("app.api.main.db.server_version", lambda: (180006, "18.6", "0.8.7"))

    resp = client.get("/healthz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "model_loading"
    assert body["embedding"]["loaded"] is False
    assert body["db"]["iterative_scan_ok"] is True


def test_returns_200_once_the_model_is_ready(client, monkeypatch):
    """★ the regression: this branch used to raise TypeError on a None status_code."""
    _stub_service(monkeypatch, ready=True)
    monkeypatch.setattr("app.api.main.db.server_version", lambda: (180006, "18.6", "0.8.7"))

    resp = client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["embedding"]["max_seq_length"] == 128


def test_db_failure_is_503_and_the_error_is_redacted(client, monkeypatch):
    _stub_service(monkeypatch, ready=True)

    def boom():
        raise RuntimeError("connection failed: password=hunter2 for user postgres")

    monkeypatch.setattr("app.api.main.db.server_version", boom)

    resp = client.get("/healthz")

    assert resp.status_code == 503
    assert "hunter2" not in resp.text


@pytest.mark.parametrize(
    "server_version,pgvector,expected",
    [
        (180006, "0.8.7", True),
        (180006, "0.8.0", True),
        (180006, "0.7.4", False),
        # ★ string compare would get this one wrong: "0.10.0" >= "0.8" is False
        (180006, "0.10.0", True),
        (180006, "1.0.0", True),
        (180006, None, False),
        (110022, "0.8.7", False),
    ],
)
def test_iterative_scan_ok_compares_versions_numerically(server_version, pgvector, expected):
    from app.api.main import _iterative_scan_ok

    assert _iterative_scan_ok(server_version, pgvector) is expected
