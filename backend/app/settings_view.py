"""设置页服务层（§11 设置页 → `GET /v1/settings`）。**只读检视器**。

★ 刻意不做「UI 写 config」：`get_settings()` 有进程级缓存（`_SETTINGS`），且 embedding 线程数、
  http 端口、dsn 这些是**启动时**读一次的——从网页写回 `~/.memxy/config.toml` 后不重启根本不生效，
  只会造成「我改了怎么没反应」的误导。config.toml 又是 app 自己的 dotfile，直接编辑更稳。
  所以这一页的职责是**让人看清当前到底在用什么值、这个值是哪一层来的**，改仍走文件 + 重启。

★ 两条安全线：
  1. **token 绝不返回**——只报每个端点「已配置/未配置」（调 `llm_token()` 判空，不取值）。
  2. **dsn 打码**——`redact()` 会把里面的 `password=...` 抹掉再返回。
"""

from __future__ import annotations

import logging
import os

from .core import db
from .core.config import (CONFIG_DIR, CONFIG_FILE, SECRETS_FILE, _DEFAULTS, _flat,
                          _read_toml, get_settings, llm_token)
from .core.redact import redact

log = logging.getLogger(__name__)

# 解析优先级（config.py）：env > secrets.toml > config.toml > 内置默认。
# 这张表把「Settings 字段」映射到「config.toml 的点号键」，好标注来源层。
# (组, 标签, Settings 属性名, config 点号键, 是否敏感)
_FIELDS = [
    ("LLM", "默认端点", "default_endpoint", "llm.default_endpoint", False),
    ("LLM", "重试次数", "llm_max_retries", "llm.max_retries", False),
    ("Embedding", "模型", "embedding_model", "embedding.model", False),
    ("Embedding", "max_seq_length", "embedding_max_seq_length", "embedding.max_seq_length", False),
    ("Embedding", "线程数", "embedding_threads", "embedding.threads", False),
    ("Embedding", "批大小", "embedding_batch_size", "embedding.batch_size", False),
    ("Embedding", "HF 离线", "hf_offline", "embedding.hf_offline", False),
    ("阈值", "atomic_fact 置信度阈值", None, "thresholds.atomic_fact", False),
    ("阈值", "栏位匹配最低分", "section_match_min_score", "thresholds.section_match_min_score", False),
    ("阈值", "栏位匹配最小间距", "section_match_min_margin", "thresholds.section_match_min_margin", False),
    ("阈值", "查重预筛", "dedup_prefilter", "thresholds.dedup_prefilter", False),
    ("汇总", "新观察触发数", "summarize_trigger_new", "summarize.trigger_new", False),
    ("汇总", "成熟观察触发数", "summarize_trigger_mature", "summarize.trigger_mature", False),
    ("召回", "k（注入条数）", "recall_k", "recall.k", False),
    ("召回", "dense 候选池", "recall_dense_pool", "recall.dense_pool", False),
    ("召回", "RRF k", "recall_rrf_k", "recall.rrf_k", False),
    ("Hook", "超时(ms)", "hook_timeout_ms", "hook.timeout_ms", False),
    ("队列", "租约(s)", "job_lease_seconds", "queue.lease_seconds", False),
    ("队列", "最大重试", "job_max_attempts", "queue.max_attempts", False),
    ("HTTP", "host", "http_host", "http.host", False),
    ("HTTP", "port", "http_port", "http.port", False),
]

# 有环境变量覆盖的键（config.py 里单独处理的），来源层要特判
_ENV_OVERRIDE = {"db.dsn": "MEM_DSN"}


def overview() -> dict:
    """生效配置 + 每项来源层 + 端点 token 有无 + 配置文件路径。全只读、全打码。"""
    settings = get_settings()
    cfg_flat = _flat(_DEFAULTS)
    config_toml = _flat(_read_toml(CONFIG_FILE))
    secrets_toml = _flat(_read_toml(SECRETS_FILE))

    items = []
    for group, label, attr, dotted, sensitive in _FIELDS:
        value = _effective(settings, attr, dotted, cfg_flat)
        items.append({
            "group": group, "label": label, "key": dotted,
            "value": value, "source": _source(dotted, config_toml, secrets_toml),
        })

    # thinking 是二维开关（endpoint×task），单独列
    thinking = []
    for task in ("default", "route", "write_facts", "summarize", "recall"):
        on = settings.thinking.enabled(task) if task != "default" else settings.thinking.default
        thinking.append({"task": task, "enabled": bool(on),
                         "source": _source(f"llm.thinking.{task}", config_toml, secrets_toml)})

    # task→endpoint 覆盖（来自 config/secrets 的 [llm] task_endpoints）
    task_endpoints = [{"task": t, "endpoint": k} for t, k in settings.task_endpoints.items()]

    return {
        "items": items,
        "thinking": thinking,
        "task_endpoints": task_endpoints,
        "dsn": redact(settings.dsn),          # ★ 打码：password=... 会被抹掉
        "dsn_source": _source("db.dsn", config_toml, secrets_toml, env_key="MEM_DSN"),
        "endpoints": _endpoint_token_presence(),
        "route_confidence_thresholds": settings.route_confidence_thresholds,
        "paths": {
            "config_dir": str(CONFIG_DIR),
            "config_file": str(CONFIG_FILE),
            "config_exists": CONFIG_FILE.is_file(),
            "secrets_file": str(SECRETS_FILE),
            "secrets_exists": SECRETS_FILE.is_file(),
            "cache_dir": str(settings.cache_dir),
        },
        "restart_note": "改 config.toml 后需重启进程才生效（get_settings 有进程级缓存，"
                        "且 embedding 线程数/端口/dsn 是启动时读一次）。",
    }


def _effective(settings, attr: str | None, dotted: str, cfg_flat: dict):
    """取生效值：有对应 Settings 字段就读它（已是解析后的终值），否则从默认表兜。"""
    if attr is not None:
        return getattr(settings, attr)
    # atomic_fact 这类没有独立字段的，从 route_confidence_thresholds 或默认表拿
    if dotted == "thresholds.atomic_fact":
        return settings.route_confidence_thresholds.get("atomic_fact")
    return cfg_flat.get(dotted)


def _source(dotted: str, config_toml: dict, secrets_toml: dict, env_key: str | None = None) -> str:
    """标注这个键的生效值来自哪一层。优先级 env > secrets > config > default。"""
    ek = env_key or _ENV_OVERRIDE.get(dotted)
    if ek and os.environ.get(ek):
        return "env"
    if dotted in secrets_toml:
        return "secrets"
    if dotted in config_toml:
        return "config"
    return "default"


def _endpoint_token_presence() -> list[dict]:
    """每个启用端点的 token 有无。★ 只调 llm_token() 判空，绝不取值、绝不返回。"""
    rows = db.query(
        "SELECT endpoint_key, model, enabled FROM llm_endpoints ORDER BY endpoint_key")
    out = []
    for r in rows:
        token = llm_token(r["endpoint_key"])
        out.append({"endpoint_key": r["endpoint_key"], "model": r["model"],
                    "enabled": r["enabled"], "token_configured": bool(token)})
    return out
