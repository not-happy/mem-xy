"""workers_ctl.ensure 的补齐语义（后端自启靠它，不能真起进程——全部打桩）。

要验的就三件事：缺口才补、够了不动、多出来的不杀（可能是人从 UI 手动加的）。
"""

from app import workers_ctl as ctl


def _stub(monkeypatch, alive_pids: list[int], started: list[int]):
    monkeypatch.setattr(ctl, "_reap_dead", lambda: None)
    monkeypatch.setattr(ctl, "status",
                        lambda: [{"slot": i, "pid": p, "alive": True}
                                 for i, p in enumerate(alive_pids)])
    monkeypatch.setattr(ctl, "start", lambda n: started.append(n))


def test_ensure_starts_only_the_shortfall(monkeypatch):
    started: list[int] = []
    _stub(monkeypatch, [101], started)
    ctl.ensure(2)
    assert started == [1]          # 已有 1 个，只补 1 个


def test_ensure_noop_when_target_reached(monkeypatch):
    started: list[int] = []
    _stub(monkeypatch, [101, 102], started)
    ctl.ensure(2)
    assert started == []


def test_ensure_never_kills_extra_workers(monkeypatch):
    started: list[int] = []
    _stub(monkeypatch, [101, 102, 103], started)
    ctl.ensure(2)
    assert started == []           # 超出目标也不动它们


def test_ensure_respects_max_cap(monkeypatch):
    started: list[int] = []
    _stub(monkeypatch, [101, 102, 103], started)
    ctl.ensure(99)                 # 目标再大也只补到 MAX_WORKERS
    assert started == [ctl.MAX_WORKERS - 3]


def test_ensure_zero_disables_autostart(monkeypatch):
    started: list[int] = []
    _stub(monkeypatch, [], started)
    ctl.ensure(0)
    assert started == []
