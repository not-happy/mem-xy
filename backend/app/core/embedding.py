"""Resident bge-m3 embedding service (CPU-only).

Two hard guardrails from measurement (plan §2.4):
  ① `max_seq_length` MUST be capped — bge-m3 defaults to 8192 and a pasted log
     would take 2-3s, freezing the conversation start.
  ② the model MUST stay resident in this process — cold start is 15s
     (import torch 12.5s + load 1.3s), so `mem-hook` can only be a thin HTTP client.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from .config import get_settings

log = logging.getLogger(__name__)

DIM = 1024
SPARSE_DIM = 250002  # reserved; sentence_transformers 2.7.0 cannot produce sparse weights (§2.4 ⑤)


def as_list(value) -> list[float]:
    """pgvector 读回来的是 `Vector` 对象（不可迭代，只有 `to_list()`），
    而 `encode()` 给的是普通 list。两边都要能算余弦，所以统一在这里转。"""
    return value.to_list() if hasattr(value, "to_list") else [float(x) for x in value]


@dataclass
class EmbeddingStats:
    loaded: bool = False
    load_seconds: float = 0.0
    encode_calls: int = 0
    encode_rows: int = 0
    encode_seconds_total: float = 0.0
    max_seq_length: int = 0
    modules: str = ""
    sparse_available: bool = False

    @property
    def avg_ms_per_row(self) -> float:
        if not self.encode_rows:
            return 0.0
        return self.encode_seconds_total / self.encode_rows * 1000


class EmbeddingService:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self._loading = False
        self._warm = False
        self.stats = EmbeddingStats()

    @property
    def ready(self) -> bool:
        """Warm, not merely imported: the first encode costs ~440ms of lazy init, and
        the hook's whole budget is 400ms — reporting ready early would blow it once."""
        return self._model is not None and self._warm

    def load(self, force: bool = False) -> None:
        """幂等 + 并发安全：整个加载过程持锁（双检锁）。

        ★ 之前锁内只护标志位、真正的 SentenceTransformer 加载在锁外——
          并发冷启动（healthz 探测线程与首个 encode 同时进来）会双份加载模型，
          峰值多占 ~2.3GB 内存（EXPERT-REVIEW B2）。现在后到的线程在锁上等，
          等到后发现 `_model` 已就绪直接返回，只加载一份。
        """
        with self._lock:
            if self._model is not None and not force:
                return
            self._loading = True
            self._warm = False
            try:
                settings = get_settings()
                if settings.hf_offline:
                    # weights are already cached; without this every start burns a 12s timeout
                    os.environ.setdefault("HF_HUB_OFFLINE", "1")
                    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                os.environ.setdefault(
                    "HF_ENDPOINT", "https://hf-mirror.com"
                )  # huggingface.co is unreachable on this network

                started = time.perf_counter()
                from sentence_transformers import SentenceTransformer

                torch_threads = settings.embedding_threads
                if torch_threads > 0:
                    import torch

                    # measured optimum is the default 8; 16 is *slower* (plan §2.4 ④)
                    torch.set_num_threads(torch_threads)

                model = SentenceTransformer(settings.embedding_model)
                model.max_seq_length = settings.embedding_max_seq_length

                elapsed = time.perf_counter() - started
                self._model = model
                self.stats = EmbeddingStats(
                    loaded=True,
                    load_seconds=round(elapsed, 3),
                    max_seq_length=int(model.max_seq_length),
                    modules=",".join(m.__class__.__name__ for m in model),
                    sparse_available=False,
                )
                log.info(
                    "bge-m3 loaded in %.2fs (max_seq_length=%s, modules=%s)",
                    elapsed,
                    model.max_seq_length,
                    self.stats.modules,
                )
                self.warmup()
                self._warm = True
            finally:
                self._loading = False

    @property
    def loading(self) -> bool:
        """★ 无锁读：/healthz 靠它回 503 model_loading，绝不能被正在进行的
        加载（持锁 15–33s）阻塞。GIL 下 bool 属性读写原子，做状态展示足够。"""
        return self._loading

    def warmup(self) -> None:
        """Absorb the first-encode lazy-init cost (~440ms measured) before serving.

        Deliberately not recorded in stats: it is the one encode that does not
        represent steady-state latency, and /healthz reports that average.
        """
        self.encode(["预热 warmup 记忆系统 embedding"], record_stats=False)

    def encode(
        self,
        texts: list[str],
        batch_size: int | None = None,
        *,
        record_stats: bool = True,
    ) -> list[list[float]]:
        """Returns L2-normalized dense vectors, so cosine == dot product."""
        if not texts:
            return []
        if self._model is None:
            self.load()
        settings = get_settings()
        bs = batch_size or settings.embedding_batch_size
        started = time.perf_counter()
        vectors = self._model.encode(
            texts,
            batch_size=bs,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        elapsed = time.perf_counter() - started
        if record_stats:
            self.stats.encode_calls += 1
            self.stats.encode_rows += len(texts)
            self.stats.encode_seconds_total += elapsed
        return [v.tolist() for v in vectors]

    def encode_one(self, text: str) -> list[float]:
        return self.encode([text])[0]

    def cosine(self, a: list[float], b: list[float]) -> float:
        return float(sum(x * y for x, y in zip(a, b)))


_service = EmbeddingService()


def get_embedding_service() -> EmbeddingService:
    return _service
