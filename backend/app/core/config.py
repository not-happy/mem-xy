"""Configuration: ~/.memxy/{config,secrets}.toml + MEM_* environment variables.

Resolution order per key: environment > secrets.toml > config.toml > built-in default.
Secrets (DB password, LLM tokens) never live in config.toml or in the repo.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("MEM_HOME", Path.home() / ".memxy"))
CONFIG_FILE = CONFIG_DIR / "config.toml"
SECRETS_FILE = CONFIG_DIR / "secrets.toml"
CACHE_DIR = CONFIG_DIR / "cache"

DEFAULT_DSN = "host=127.0.0.1 port=5432 dbname=memxy user=postgres"
DEFAULT_ENDPOINT = "dida-gateway"


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


@dataclass(frozen=True)
class ThinkingConfig:
    """`thinking` is a two-dimensional (endpoint, task) switch — see plan §9.1."""

    per_task: dict[str, bool] = field(default_factory=dict)
    default: bool = True

    def enabled(self, task: str) -> bool:
        return self.per_task.get(task, self.default)


@dataclass(frozen=True)
class Settings:
    dsn: str
    default_endpoint: str
    task_endpoints: dict[str, str]
    thinking: ThinkingConfig
    embedding_model: str
    embedding_max_seq_length: int
    embedding_threads: int
    embedding_batch_size: int
    hf_offline: bool
    route_confidence_thresholds: dict[str, float]
    section_match_min_score: float
    section_match_min_margin: float
    dedup_prefilter: float
    summarize_trigger_new: int
    summarize_trigger_mature: int
    recall_k: int
    recall_dense_pool: int
    recall_rrf_k: int
    hook_timeout_ms: int
    job_lease_seconds: int
    job_max_attempts: int
    http_host: str
    http_port: int
    worker_autostart: int
    backup_daily_at: str
    llm_max_retries: int
    cache_dir: Path

    def endpoint_for(self, task: str) -> str:
        """The single place where a task is mapped to an endpoint (plan §9.1)."""
        return self.task_endpoints.get(task, self.default_endpoint)


_DEFAULTS: dict = {
    "db": {"dsn": DEFAULT_DSN},
    "llm": {
        "default_endpoint": DEFAULT_ENDPOINT,
        "task_endpoints": {},
        "max_retries": 2,
    },
    "llm.thinking": {"default": True, "route": True, "write_facts": True, "summarize": True},
    "embedding": {
        "model": "BAAI/bge-m3",
        # ★ hard guardrail, not an optimization: bge-m3 defaults to 8192 and a pasted
        # 4000-char log would take 2-3s on CPU, freezing the conversation start (plan §2.4 ①).
        "max_seq_length": 128,
        "threads": 8,  # measured optimum; 16 is *slower* (plan §2.4 ④)
        "batch_size": 32,  # marginal gain flattens above 32 (plan §2.4 ④)
        "hf_offline": True,  # weights are cached; avoids a 12s network timeout per start
    },
    "thresholds": {
        # `observation` deliberately absent: rules get no confidence gate (plan §5.4).
        # `discard` deliberately absent too: discards never become review cards
        # (router._decide) — they stay auditable via route_decisions + prompt_ver replay.
        "atomic_fact": 0.85,
        "section_match_min_score": 0.35,
        "section_match_min_margin": 0.02,
        # ★ 这个数**只能当预筛**，不能当"判重复"的最终依据。
        #   2026-10-23 实测（bge-m3，中文短句）：同一条规矩的两种说法 0.767–0.888，
        #   "补充了细节的超集"0.802–0.875，不相干的 0.331–0.459。
        #   ⇒ 相关/不相干之间有很宽的空档（0.46→0.77），但"重复"与"超集"完全重叠，
        #     余弦分不开。所以 0.72 取在空档中间，只用来"要不要拉出来看一眼"。
        "dedup_prefilter": 0.72,
    },
    "summarize": {"trigger_new": 3, "trigger_mature": 20},
    "recall": {"k": 5, "dense_pool": 50, "rrf_k": 60},
    "hook": {"timeout_ms": 400},
    "queue": {"lease_seconds": 300, "max_attempts": 5},
    "http": {"host": "127.0.0.1", "port": 8787},
    # 后端起来自动补齐到几个 worker；0 = 关（只允许 UI/命令行手动启停）。
    "workers": {"autostart": 2},
    # 每日备份触发时刻（backend 内置调度，绕开 SASE 对计划任务链拦 pg_dump，
    # 见 app/backup_daily.py 模块注释）；"off" 关闭。
    "backup": {"daily_at": "12:30"},
}


def _dig(table: dict, dotted: str) -> dict:
    node = table
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _flat(table: dict) -> dict:
    """Accept both nested [llm.thinking] and inline dotted keys."""
    out: dict = {}
    for key, value in table.items():
        if isinstance(value, dict):
            for sub_key, sub_value in _flat(value).items():
                out[f"{key}.{sub_key}"] = sub_value
        else:
            out[key] = value
    return out


def _merged(*layers: dict) -> dict:
    out: dict = {}
    for layer in layers:
        for key, value in _flat(layer).items():
            if value is not None:
                out[key] = value
    return out


def load_settings() -> Settings:
    config = _read_toml(CONFIG_FILE)
    secrets = _read_toml(SECRETS_FILE)
    cfg = _merged(_flat(_DEFAULTS), config, secrets)

    dsn = os.environ.get("MEM_DSN") or cfg.get("db.dsn", DEFAULT_DSN)

    thinking_prefix = "llm.thinking."
    thinking_flat = {
        k[len(thinking_prefix) :]: bool(v)
        for k, v in cfg.items()
        if k.startswith(thinking_prefix) and k != "llm.thinking.default"
    }
    thinking = ThinkingConfig(
        per_task=thinking_flat,
        default=bool(cfg.get("llm.thinking.default", True)),
    )

    task_endpoints = dict(config.get("llm", {}).get("task_endpoints", {}) or {})
    task_endpoints.update(secrets.get("llm", {}).get("task_endpoints", {}) or {})

    return Settings(
        dsn=dsn,
        default_endpoint=cfg.get("llm.default_endpoint", DEFAULT_ENDPOINT),
        task_endpoints=task_endpoints,
        thinking=thinking,
        embedding_model=cfg.get("embedding.model", "BAAI/bge-m3"),
        embedding_max_seq_length=int(cfg.get("embedding.max_seq_length", 128)),
        embedding_threads=int(cfg.get("embedding.threads", 8)),
        embedding_batch_size=int(cfg.get("embedding.batch_size", 32)),
        hf_offline=bool(cfg.get("embedding.hf_offline", True)),
        route_confidence_thresholds={
            "atomic_fact": float(cfg.get("thresholds.atomic_fact", 0.85)),
            # ★ 没有 `discard`：discard 一律不进待办（见 router._decide 的三条理由），
            #   留个没人读的阈值只会让人以为它在起作用。
        },
        section_match_min_score=float(cfg.get("thresholds.section_match_min_score", 0.35)),
        section_match_min_margin=float(cfg.get("thresholds.section_match_min_margin", 0.02)),
        dedup_prefilter=float(cfg.get("thresholds.dedup_prefilter", 0.72)),
        summarize_trigger_new=int(cfg.get("summarize.trigger_new", 3)),
        summarize_trigger_mature=int(cfg.get("summarize.trigger_mature", 20)),
        recall_k=int(cfg.get("recall.k", 5)),
        recall_dense_pool=int(cfg.get("recall.dense_pool", 50)),
        recall_rrf_k=int(cfg.get("recall.rrf_k", 60)),
        hook_timeout_ms=int(cfg.get("hook.timeout_ms", 400)),
        job_lease_seconds=int(cfg.get("queue.lease_seconds", 300)),
        job_max_attempts=int(cfg.get("queue.max_attempts", 5)),
        http_host=cfg.get("http.host", "127.0.0.1"),
        http_port=int(cfg.get("http.port", 8787)),
        worker_autostart=int(cfg.get("workers.autostart", 2)),
        # 每日备份触发时刻 HH:MM；"off" 关闭。env 覆盖便于验证（见 backup_daily 模块）。
        backup_daily_at=os.environ.get("MEM_BACKUP_DAILY_AT") or cfg.get("backup.daily_at", "12:30"),
        llm_max_retries=int(cfg.get("llm.max_retries", 2)),
        cache_dir=CACHE_DIR,
    )


def llm_token(endpoint_key: str, secrets: dict | None = None) -> str | None:
    """Token lookup: MEM_LLM_TOKEN__<ENDPOINT_KEY> > secrets.toml [llm_tokens].

    Never falls back to config.toml and never returns a value that gets logged.
    """
    env_key = f"MEM_LLM_TOKEN__{endpoint_key.upper().replace('-', '_')}"
    token = os.environ.get(env_key)
    if token:
        return token
    table = secrets if secrets is not None else _read_toml(SECRETS_FILE)
    return table.get("llm_tokens", {}).get(endpoint_key)


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS
