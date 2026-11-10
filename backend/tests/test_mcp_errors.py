"""MCP 适配层的**离线**单测：错误翻译 + cwd 归属优先级。

★ 和 `test_mcp_server.py` 分工：那边起真进程打真服务，验的是 stdio 握手和端到端闭环；
这边不联网、不连库，专验两件"错了也不报错、只是悄悄归错桶/悄悄吞掉错误"的事——
`_call` 怎么把 HTTP 状态翻成模型看得懂的一句话，`_cwd` 到底听谁的。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

_SERVER = pathlib.Path(__file__).resolve().parents[2] / "integrations/mcp-server/mem_mcp.py"

requires_windows = pytest.mark.skipif(os.name != "nt",
                                      reason="盘符路径这个坑只在 Windows 上存在")


def _load():
    spec = importlib.util.spec_from_file_location("mem_mcp_under_test", _SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "_ROOTS", None)      # 模块级缓存，不重置会串到下一条
    monkeypatch.setenv("MEMXY_URL", "http://127.0.0.1:8787")
    monkeypatch.delenv("MEMXY_CWD", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return module


def _transport(monkeypatch, mod, handler):
    """把 `_call` 里那个 AsyncClient 换成 MockTransport：不发一个真包。"""
    real = httpx.AsyncClient
    monkeypatch.setattr(mod, "httpx", SimpleNamespace(
        AsyncClient=lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
        MockTransport=httpx.MockTransport))


def _json(status: int, payload: dict):
    return httpx.Response(status, json=payload)


def _error(status: int, code: str, message: str, **detail):
    return _json(status, {"error": {"code": code, "message": message, "detail": detail}})


# ─────────────────── 错误翻译：模型拿到的是"一句能行动的话" ───────────────────

def test_a_loading_model_reads_as_retryable_not_broken(monkeypatch, mod):
    """★ 不说"稍后重试"，模型就会告诉用户记忆系统坏了，然后用户去修一个没坏的东西。"""
    _transport(monkeypatch, mod, lambda request: _error(503, "model_loading", "embedding 未就绪"))

    with pytest.raises(ToolError) as exc:
        asyncio.run(mod._call("GET", "/v1/pending"))

    assert "预热" in str(exc.value) and "重试" in str(exc.value)


def test_a_business_error_keeps_its_code_and_the_server_message(monkeypatch, mod):
    _transport(monkeypatch, mod,
               lambda request: _error(409, "duplicate", "同桶里已经有一条内容相同的活跃记忆 #3"))

    with pytest.raises(ToolError) as exc:
        asyncio.run(mod._call("POST", "/v1/memories", body={"content": "x"}))

    assert "duplicate" in str(exc.value) and "#3" in str(exc.value)


def test_a_non_json_error_body_is_never_echoed_to_the_model(monkeypatch, mod):
    """反代吐的 HTML 里可能有内网主机名、版本号；服务端没 redact 过的东西一律不转述。"""
    _transport(monkeypatch, mod, lambda request: httpx.Response(
        502, text="<html>nginx/1.25 on gw-internal-07</html>"))

    with pytest.raises(ToolError) as exc:
        asyncio.run(mod._call("GET", "/v1/pending"))

    assert "gw-internal-07" not in str(exc.value)
    assert "502" in str(exc.value)


def test_a_transport_error_hides_credentials_from_the_url(monkeypatch, mod):
    """★ `MEMXY_URL` 允许写成 `http://user:pass@host`；异常字符串会带上整个 URL，
    而这句话会进模型上下文——所以转述前把 URL 换成占位符（§2.1 的同一条纪律）。"""
    monkeypatch.setenv("MEMXY_URL", "http://memxy:sup3rs3cret@127.0.0.1:8787")

    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    _transport(monkeypatch, mod, handler)

    with pytest.raises(ToolError) as exc:
        asyncio.run(mod._call("GET", "/v1/pending"))

    assert "sup3rs3cret" not in str(exc.value)
    assert "ConnectError" in str(exc.value) and "连不上" in str(exc.value)


def test_a_dead_service_is_reported_as_unreachable(monkeypatch, mod):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    _transport(monkeypatch, mod, handler)

    with pytest.raises(ToolError) as exc:
        asyncio.run(mod._call("GET", "/v1/pending"))

    assert "连不上" in str(exc.value) and "app.commands.serve" in str(exc.value)


def test_none_params_are_dropped_and_success_returns_the_payload(monkeypatch, mod):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return _json(200, {"total": 0, "items": []})

    _transport(monkeypatch, mod, handler)

    out = asyncio.run(mod._call("GET", "/v1/memories", params={"q": "婴儿票", "role": None}))

    assert out == {"total": 0, "items": []}
    assert dict(seen[0].url.params) == {"q": "婴儿票"}


# ─────────────────── cwd：谁说了算 ───────────────────

class FakeSession:
    def __init__(self, roots=None, error=None):
        self._roots = roots
        self._error = error
        self.calls = 0
        self.session_id = "abc123"

    async def list_roots(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return SimpleNamespace(roots=[SimpleNamespace(uri=uri) for uri in self._roots])


def _ctx(roots=None, error=None):
    return SimpleNamespace(session=FakeSession(roots, error))


def test_an_explicit_cwd_wins_and_roots_are_never_asked(mod):
    ctx = _ctx(roots=["file:///C:/other"])

    out = asyncio.run(mod._cwd(ctx, "C:/explicit"))

    assert out == "C:/explicit"
    assert ctx.session.calls == 0


@requires_windows
def test_roots_beat_both_environment_variables(mod, monkeypatch):
    """★ `CLAUDE_PROJECT_DIR` 指的是**配置文件所在的项目**；用户在别的仓库里开会话时，
    听它的话就会把记忆归错桶（§6.1 串台）。roots 才是这个会话真正打开的目录。"""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "C:/from/claude")
    monkeypatch.setenv("MEMXY_CWD", "C:/from/memxy")

    out = asyncio.run(mod._cwd(_ctx(roots=["file:///C:/from/roots"]), None))

    assert out == "C:\\from\\roots"


@requires_windows
def test_a_windows_drive_letter_survives_the_file_url_round_trip(mod):
    """★ `file:///C:/x` 的 path 是 `/C:/x`，直接 `Path(path)` 会得到 `C:x`——盘符相对路径，
    归属判定跟着全错。这个 bug **不抛异常**，只是把记忆悄悄归到另一个桶里。"""
    assert asyncio.run(mod._cwd(_ctx(roots=["file:///C:/from/roots"]), None)) == "C:\\from\\roots"


@requires_windows
def test_a_percent_encoded_root_is_decoded_exactly_once(mod):
    """`url2pathname` 自己会 unquote；外面再 unquote 一遍会把字面量 `%25` 也吃掉。"""
    out = asyncio.run(mod._cwd(_ctx(roots=["file:///C:/a%2520b"]), None))

    assert out == "C:\\a%20b"


def test_a_client_without_roots_falls_back_to_memxy_cwd(mod, monkeypatch):
    monkeypatch.setenv("MEMXY_CWD", "C:/from/memxy")

    out = asyncio.run(mod._cwd(_ctx(error=RuntimeError("Client does not support listing roots")), None))

    assert out == "C:/from/memxy"


def test_a_slow_or_denied_roots_call_does_not_hang_the_tool(mod, monkeypatch):
    """SDK 默认等 60s。工具调用挂住 = 模型卡住，比归错桶更难查。"""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "C:/from/claude")

    out = asyncio.run(mod._cwd(_ctx(error=TimeoutError()), None))

    assert out == "C:/from/claude"


def test_a_non_file_root_is_ignored(mod, monkeypatch):
    monkeypatch.setenv("MEMXY_CWD", "C:/from/memxy")

    out = asyncio.run(mod._cwd(_ctx(roots=["https://example.com/repo"]), None))

    assert out == "C:/from/memxy"


def test_the_roots_probe_happens_once_per_process(mod):
    """每个 tool 调用都往客户端问一次 roots 是白花钱，而一个 stdio server 只服务一个窗口。"""
    ctx = _ctx(roots=["file:///C:/somewhere"])

    asyncio.run(mod._cwd(ctx, None))
    asyncio.run(mod._cwd(ctx, None))

    assert ctx.session.calls == 1


def test_the_process_cwd_is_the_last_resort(mod):
    assert asyncio.run(mod._cwd(_ctx(error=RuntimeError("no roots")), None)) == os.getcwd()


def test_the_session_id_separates_model_initiated_recall_from_hook_injection(mod):
    """审计上要能分清哪些记忆是模型主动查的、哪些是 hook 自动注入的。"""
    assert mod._session_id(_ctx()) == "mcp-abc123"
