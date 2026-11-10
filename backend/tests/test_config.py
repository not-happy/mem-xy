import importlib
import textwrap

import pytest


def _reload_config(monkeypatch, tmp_path, *, config="", secrets="", env=None):
    monkeypatch.setenv("MEM_HOME", str(tmp_path))
    for key in ("MEM_DSN", "MEM_LLM_TOKEN__DIDA_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    if config:
        (tmp_path / "config.toml").write_text(textwrap.dedent(config), encoding="utf-8")
    if secrets:
        (tmp_path / "secrets.toml").write_text(textwrap.dedent(secrets), encoding="utf-8")
    from app.core import config as config_module

    return importlib.reload(config_module)


def test_defaults_are_the_measured_guardrails(monkeypatch, tmp_path):
    cfg = _reload_config(monkeypatch, tmp_path)
    s = cfg.load_settings()
    # ★ plan §2.4 ① — bge-m3 defaults to 8192; 128 is a guardrail, not a tweak
    assert s.embedding_max_seq_length == 128
    # ★ plan §2.4 ④ — 8 measured optimal, 16 is slower
    assert s.embedding_threads == 8
    # ★ plan §2.4 ④ — marginal gain flattens above 32
    assert s.embedding_batch_size == 32
    assert s.hf_offline is True
    assert s.hook_timeout_ms == 400


def test_observation_has_no_confidence_gate(monkeypatch, tmp_path):
    """plan §5.4: rules go 100% to human review, so there must be no threshold key."""
    cfg = _reload_config(monkeypatch, tmp_path)
    s = cfg.load_settings()
    assert "observation" not in s.route_confidence_thresholds
    assert s.route_confidence_thresholds == {"atomic_fact": 0.85}


def test_discard_has_no_threshold_either(monkeypatch, tmp_path):
    """discard 不进待办（router._decide），留个没人读的阈值只会让人以为它在起作用。"""
    cfg = _reload_config(monkeypatch, tmp_path,
                         config="[thresholds]\ndiscard = 0.99\n")
    assert "discard" not in cfg.load_settings().route_confidence_thresholds


def test_endpoint_for_task_falls_back_to_default(monkeypatch, tmp_path):
    cfg = _reload_config(
        monkeypatch, tmp_path,
        config="""
        [llm]
        default_endpoint = "dida-gateway"
        [llm.task_endpoints]
        summarize = "stronger-model"
        """,
    )
    s = cfg.load_settings()
    assert s.endpoint_for("summarize") == "stronger-model"
    assert s.endpoint_for("route") == "dida-gateway"
    assert s.endpoint_for("write_facts") == "dida-gateway"


def test_thinking_is_per_task_not_global(monkeypatch, tmp_path):
    cfg = _reload_config(
        monkeypatch, tmp_path,
        config="""
        [llm.thinking]
        route = true
        write_facts = true
        summarize = false
        """,
    )
    s = cfg.load_settings()
    assert s.thinking.enabled("route") is True
    assert s.thinking.enabled("summarize") is False
    assert s.thinking.enabled("unlisted-task") is True  # default


def test_env_overrides_files(monkeypatch, tmp_path):
    cfg = _reload_config(
        monkeypatch, tmp_path,
        config='[db]\ndsn = "host=from-config dbname=x"\n',
        env={"MEM_DSN": "host=from-env dbname=y"},
    )
    assert cfg.load_settings().dsn == "host=from-env dbname=y"


def test_token_comes_from_env_or_secrets_never_config(monkeypatch, tmp_path):
    cfg = _reload_config(
        monkeypatch, tmp_path,
        config='[llm_tokens]\ndida-gateway = "should-not-be-here"\n',
        secrets='[llm_tokens]\ndida-gateway = "from-secrets"\n',
    )
    # config.toml is not consulted for tokens at all
    assert cfg.llm_token("dida-gateway") == "from-secrets"

    cfg = _reload_config(
        monkeypatch, tmp_path,
        secrets='[llm_tokens]\ndida-gateway = "from-secrets"\n',
        env={"MEM_LLM_TOKEN__DIDA_GATEWAY": "from-env"},
    )
    assert cfg.llm_token("dida-gateway") == "from-env"


def test_missing_token_returns_none(monkeypatch, tmp_path):
    cfg = _reload_config(monkeypatch, tmp_path)
    assert cfg.llm_token("nope") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
