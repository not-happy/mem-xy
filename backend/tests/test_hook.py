"""`integrations/hooks/mem-hook.py` 的契约测试。

★ 这个脚本是整套系统里唯一跑在**用户对话进程里**的代码：每轮都要起一次，一旦它报错
  或者卡住，用户看到的就是"Claude 变慢了/挂了"。所以它的验收标准不是"能注入记忆"，
  而是**无论后端什么状态都退出码 0、且不超时**。这些测试全部不碰网络、不碰 DB。

模块名带连字符（`mem-hook.py`），import 不进来，只能用 importlib 按路径加载。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import time
import urllib.error
import urllib.request

import pytest

_HOOK_PATH = (pathlib.Path(__file__).resolve().parents[2]
              / "integrations" / "hooks" / "mem-hook.py")

_spec = importlib.util.spec_from_file_location("mem_hook_under_test", _HOOK_PATH)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

DEAD_URL = "http://127.0.0.1:1"        # 端口 1，保证没人监听
CWD = "C:/Users/yong.xu/IdeaProjects/dida/gds-agg-processor-tiket"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """每个测试一个空的 `MEM_HOME`，超时压到 50ms——否则一次拒连就要等 400ms。"""
    monkeypatch.setenv("MEM_HOME", str(tmp_path))
    monkeypatch.setenv("MEMXY_TIMEOUT_MS", "50")
    monkeypatch.setenv("MEMXY_URL", DEAD_URL)
    yield tmp_path


def _event(**kw) -> dict:
    return {"session_id": "pytest-hook", "cwd": CWD, **kw}


def _capture_calls(monkeypatch, *, result=(200, {"items": []}), error=None):
    """把 `_call` 换成录音机，返回记录到的 (method, path, body) 列表。"""
    calls: list[tuple] = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(hook, "_call", fake)
    return calls


# ─────────────────────── 硬约束：绝不影响对话 ───────────────────────

def test_an_unknown_command_is_the_only_thing_that_complains(capsys):
    assert hook.main(["bogus"]) == 2
    assert "未知命令" in capsys.readouterr().err


def test_a_handler_that_raises_still_exits_zero(monkeypatch):
    monkeypatch.setattr(hook, "COMMANDS", {"recall": lambda event: 1 / 0})
    assert hook.main(["recall"]) == 0


def test_an_unreachable_service_degrades_to_silence(capsys):
    """真连一次死端口（超时已被 fixture 压到 50ms），验端到端静默。"""
    started = time.perf_counter()
    assert hook.recall(_event(prompt="Tiket 改签")) == 0
    assert capsys.readouterr().out == ""
    assert time.perf_counter() - started < 1.0, "降级路径不该比超时还慢"


def test_garbage_on_stdin_is_not_an_error(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin("{这不是 json"))
    assert hook.main(["bootstrap"]) == 0
    assert capsys.readouterr().out == ""


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# ─────────────────────── 哑熔断（离线不该每轮白交 400ms）───────────────────────

def _count_urlopen(monkeypatch, error):
    attempts = []

    def fake_urlopen(request, timeout=None):
        attempts.append(request.full_url)
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return attempts


def test_a_failed_call_trips_the_breaker_so_the_next_turn_skips_the_timeout(monkeypatch):
    """★ Windows 上连没人监听的本地端口收不到 RST，实测耗满超时（402ms）。
    没有熔断的话，服务没起的每一轮对话都要白交这 400ms。"""
    attempts = _count_urlopen(monkeypatch, OSError("refused"))

    hook.recall(_event(prompt="第一次"))
    hook.recall(_event(prompt="第二次"))
    hook.recall(_event(prompt="第三次"))

    assert len(attempts) == 1, "跳闸后不该再发请求"


def test_a_successful_call_clears_the_breaker_marker(isolated_home, monkeypatch):
    """★ 只桩 `urlopen`，不桩 `_call`——复位逻辑就住在 `_call` 里，换掉它等于没测。"""
    _count_urlopen(monkeypatch, OSError("refused"))
    hook.recall(_event(prompt="失败一次"))
    marker = isolated_home / "cache" / hook._BREAKER_FILE
    assert hook._breaker_open()

    # 冷静期过了：把时间戳改老，让 hook 愿意再试一次
    marker.write_text(str(time.time() - hook.BREAKER_COOLDOWN_SECONDS - 1), encoding="utf-8")
    assert not hook._breaker_open()

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda request, timeout=None: _FakeResponse(200, b'{"items": []}'))
    hook.recall(_event(prompt="服务回来了"))
    assert not marker.exists(), "成功一次就该把标记摘掉"


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def test_a_503_does_not_trip_the_breaker(monkeypatch):
    """模型预热要 11–33s，这期间服务是**活的**。把它当宕机跳闸，
    冷静期内就一条记忆都注入不进来了。"""
    attempts = _count_urlopen(
        monkeypatch, urllib.error.HTTPError(DEAD_URL, 503, "model_loading", {}, None))

    hook.recall(_event(prompt="预热中"))
    hook.recall(_event(prompt="还在预热"))

    assert not hook._breaker_open()
    assert len(attempts) == 2


def test_the_breaker_marker_survives_a_corrupt_file(isolated_home, monkeypatch):
    (isolated_home / "cache").mkdir(parents=True)
    (isolated_home / "cache" / hook._BREAKER_FILE).write_text("不是时间戳", encoding="utf-8")
    assert hook._breaker_open() is False


# ─────────────────────── recall：注入与去重 ───────────────────────

def test_recall_prints_the_hits_as_markdown_bullets(monkeypatch, capsys):
    _capture_calls(monkeypatch, result=(200, {"items": [
        {"memory_id": 95, "text": "[记忆 #95 · project] 改签要带 returnAncillary"},
        {"memory_id": 93, "text": "[记忆 #93 · project] passengerId 是订单级"},
    ]}))
    assert hook.recall(_event(prompt="Tiket 改签")) == 0
    out = capsys.readouterr().out
    assert "<!-- mem-xy 召回的相关记忆" in out
    assert out.count("- [记忆 #") == 2


def test_an_empty_prompt_never_reaches_the_server(monkeypatch):
    calls = _capture_calls(monkeypatch)
    assert hook.recall(_event(prompt="   ")) == 0
    assert calls == []


def test_the_second_turn_excludes_what_the_first_turn_already_injected(monkeypatch):
    """同一条记忆每轮都塞一遍，600 token 的预算（§1.2）几轮就烧光，模型还会开始忽略它。"""
    calls = _capture_calls(monkeypatch, result=(200, {"items": [
        {"memory_id": 95, "text": "a"}, {"memory_id": 93, "text": "b"}]}))

    hook.recall(_event(prompt="第一轮"))
    hook.recall(_event(prompt="第二轮"))

    assert calls[0][2]["exclude_ids"] == []
    assert calls[1][2]["exclude_ids"] == [95, 93]


def test_the_session_state_file_is_capped(isolated_home, monkeypatch):
    """★ 长会话可能召回几百次，状态文件不能无限长。"""
    hook._remember_injected("long", list(range(500)))
    hook._remember_injected("long", [9999])
    seen = hook._session_state("long")["injected"]
    assert len(seen) == 200
    assert seen[-1] == 9999, "截断的应该是最老的"


def test_a_non_200_response_injects_nothing(monkeypatch, capsys):
    _capture_calls(monkeypatch, result=(503, {}))
    assert hook.recall(_event(prompt="预热中")) == 0
    assert capsys.readouterr().out == ""


# ─────────────────────── bootstrap：本地快照缓存降级（§4.3.3）───────────────────────

def test_bootstrap_prints_the_server_markdown_and_caches_it(isolated_home, monkeypatch):
    monkeypatch.setattr(hook, "_call", lambda *a, **k: (200, {
        "markdown": "## 代码风格\n\n- 用 pytest",
        "cache_key": "bootstrap-dida-dida-gds-agg-processor-tiket",
    }))
    hook.bootstrap(_event())
    cached = isolated_home / "cache" / "bootstrap-dida-dida-gds-agg-processor-tiket.md"
    assert cached.read_text(encoding="utf-8") == "## 代码风格\n\n- 用 pytest"


def test_an_empty_library_is_legal_and_writes_no_cache(isolated_home, monkeypatch, capsys):
    """库还没建起来时 markdown 是空串——这时候不该留一个空缓存文件，
    否则离线降级会打出一条只有横幅、没有内容的假快照。"""
    monkeypatch.setattr(hook, "_call", lambda *a, **k: (200, {"markdown": "", "cache_key": "bootstrap-x"}))
    hook.bootstrap(_event())
    assert capsys.readouterr().out == ""
    assert not (isolated_home / "cache" / "bootstrap-x.md").exists()


def test_offline_bootstrap_falls_back_to_the_cached_snapshot(isolated_home, monkeypatch, capsys):
    cache = isolated_home / "cache"
    cache.mkdir(parents=True)
    (cache / "bootstrap-dida-dida-gds-agg-processor-tiket.md").write_text(
        "## 代码风格\n\n- 【钉死】按 tripType 选 journey 下标", encoding="utf-8")
    monkeypatch.setattr(hook, "_call", lambda *a, **k: (0, {}))

    hook.bootstrap(_event())
    out = capsys.readouterr().out
    assert hook.OFFLINE_BANNER in out
    assert "tripType" in out


def test_offline_bootstrap_never_injects_another_projects_snapshot(isolated_home, monkeypatch, capsys):
    """★ §6.1 要防的串台：离线时猜不中缓存就什么都不注入，
    **绝不**去遍历 cache 目录随便挑一个文件。"""
    cache = isolated_home / "cache"
    cache.mkdir(parents=True)
    (cache / "bootstrap-dida-dida-gds-agg-processor-tiket.md").write_text(
        "别的项目的规矩", encoding="utf-8")
    monkeypatch.setattr(hook, "_call", lambda *a, **k: (0, {}))

    hook.bootstrap({"session_id": "s", "cwd": "C:/Users/yong.xu/own/totally-other-repo"})
    assert capsys.readouterr().out == ""


def test_the_cache_key_guess_normalizes_windows_paths(isolated_home, monkeypatch):
    """transcript 里的 cwd 用反斜杠，服务端算 cache_key 时用的是正斜杠 + 仓库名。"""
    cache = isolated_home / "cache"
    cache.mkdir(parents=True)
    (cache / "bootstrap-dida-gds-agg.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(hook, "_call", lambda *a, **k: (0, {}))
    assert hook._guess_cache_key({"cwd": r"C:\Users\yong.xu\work\gds-agg"}) == "bootstrap-dida-gds-agg"
    assert hook._guess_cache_key({}) is None


# ─────────────────────── flush：Stop hook 不许阻塞 ───────────────────────

def test_flush_fires_the_scan_without_waiting_for_it(monkeypatch):
    calls = _capture_calls(monkeypatch, result=(202, {"queued": True}))
    assert hook.flush(_event()) == 0
    assert calls == [("POST", "/v1/ingest/scan?sync=false", {})]


def test_flush_skips_when_the_stop_hook_is_already_active(monkeypatch):
    """Claude Code 会带 `stop_hook_active` 重入 Stop，不去重就会自己给自己排一遍扫描。"""
    calls = _capture_calls(monkeypatch)
    assert hook.flush(_event(stop_hook_active=True)) == 0
    assert calls == []
