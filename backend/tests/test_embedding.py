"""Embedding service guardrails (plan §2.4) and the readiness contract /healthz depends on."""

import sys
import threading
import time
import types

import numpy as np
import pytest

from app.core import embedding


class _Transformer:
    pass


class _Pooling:
    pass


class _Normalize:
    pass


class FakeModel:
    """Stands in for SentenceTransformer; `gate` blocks encode() so tests can observe
    the window where the model object exists but warmup has not finished."""

    def __init__(self, gate=None):
        self.max_seq_length = 8192  # bge-m3's real default — the thing we must cap
        self.calls: list[tuple[list[str], dict]] = []
        self._gate = gate

    def __iter__(self):
        return iter([_Transformer(), _Pooling(), _Normalize()])

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        if self._gate is not None:
            assert self._gate.wait(10), "test never released the gate"
        return np.ones((len(texts), 4), dtype="float32")


@pytest.fixture
def stub_heavy_imports(monkeypatch):
    """Keep the test off the real 12s torch import and the 2.3GB model."""

    def install(model):
        st = types.ModuleType("sentence_transformers")
        st.SentenceTransformer = lambda name: model
        torch_mod = types.ModuleType("torch")
        torch_mod.set_num_threads = lambda n: None
        monkeypatch.setitem(sys.modules, "sentence_transformers", st)
        monkeypatch.setitem(sys.modules, "torch", torch_mod)

    return install


def test_ready_is_false_until_warmup_finishes(stub_heavy_imports):
    """★ the regression: `ready` used to mean "model object exists", so /healthz flipped
    to 200 while the first encode still had ~440ms of lazy init to pay — more than the
    hook's entire 400ms budget."""
    gate = threading.Event()
    model = FakeModel(gate)
    stub_heavy_imports(model)
    service = embedding.EmbeddingService()

    loader = threading.Thread(target=service.load, daemon=True)
    loader.start()
    deadline = time.time() + 10
    while service._model is None and time.time() < deadline:
        time.sleep(0.01)

    assert service._model is not None, "load() never reached warmup"
    assert service.ready is False
    assert service.loading is True

    gate.set()
    loader.join(10)

    assert service.ready is True
    assert service.loading is False


def test_warmup_is_excluded_from_the_reported_average(stub_heavy_imports):
    """warmup is the one encode that is not steady state; /healthz reports the average."""
    model = FakeModel()
    stub_heavy_imports(model)
    service = embedding.EmbeddingService()
    service.load()

    assert len(model.calls) == 1, "load() should warm up exactly once"
    assert service.stats.encode_rows == 0
    assert service.stats.avg_ms_per_row == 0.0

    service.encode(["第一条", "第二条"])

    assert service.stats.encode_rows == 2
    assert service.stats.encode_calls == 1


def test_max_seq_length_is_capped_down_from_the_model_default(stub_heavy_imports):
    """★ guardrail ①: bge-m3 ships at 8192; a pasted 4000-char log would take 2-3s."""
    model = FakeModel()
    stub_heavy_imports(model)
    service = embedding.EmbeddingService()
    service.load()

    assert model.max_seq_length == 128
    assert service.stats.max_seq_length == 128


def test_vectors_are_normalized_so_cosine_is_a_dot_product(stub_heavy_imports):
    model = FakeModel()
    stub_heavy_imports(model)
    service = embedding.EmbeddingService()
    service.load()

    rows = service.encode(["随便一句话"])

    assert len(rows) == 1 and len(rows[0]) == 4
    assert model.calls[-1][1]["normalize_embeddings"] is True
    assert model.calls[-1][1]["show_progress_bar"] is False


def test_empty_input_short_circuits(stub_heavy_imports):
    model = FakeModel()
    stub_heavy_imports(model)
    service = embedding.EmbeddingService()

    assert service.encode([]) == []
    assert model.calls == []
    assert service.ready is False, "an empty call must not trigger a model load"


def test_concurrent_cold_start_loads_the_model_exactly_once(monkeypatch):
    """★ EXPERT-REVIEW B2：旧实现锁内只护标志位、真正的加载在锁外——
    healthz 探测线程与首个 encode 同时冷启动会双份加载模型，峰值多占 ~2.3GB。
    双检锁后：后到的线程在锁上等，等到后发现 _model 已就绪直接返回。"""
    constructed = []
    gate = threading.Event()

    def slow_ctor(name):
        constructed.append(name)
        assert gate.wait(10), "test never released the gate"
        return FakeModel()

    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = slow_ctor
    torch_mod = types.ModuleType("torch")
    torch_mod.set_num_threads = lambda n: None
    monkeypatch.setitem(sys.modules, "sentence_transformers", st)
    monkeypatch.setitem(sys.modules, "torch", torch_mod)

    service = embedding.EmbeddingService()
    threads = [threading.Thread(target=service.load, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.3)   # 拉开竞态窗口：一个线程持锁构造中，另一个必然已到锁上
    assert service.loading is True, "loading 必须无锁可读，否则这里就死锁了"
    gate.set()
    for t in threads:
        t.join(10)

    assert len(constructed) == 1, f"并发冷启动加载了 {len(constructed)} 份模型"
    assert service.ready is True
