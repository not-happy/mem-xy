"""MCP 探视模块的纯逻辑单测 + `/v1/mcp*` 的 HTTP 形状测试。

★ 不依赖真 MCP 进程/真客户端：进程表用假 CIM 行（dict），注册扫描指向临时文件，
  PowerShell 扫描的失败路径用 monkeypatch。要钉住的行为：
  - 匹配按"文件名 + 本项目路径"双条件——别的项目就算也有 mem_mcp.py 也不算；
  - 孤儿 = 父进程不在扫描结果里（客户端关了、adapter 还挂着）；
  - 任何底层失败（CIM/DB/注册文件读不了）都**降级为字段缺失/None**，探视不许 500；
  - stop 默认只杀孤儿——杀活进程会让客户端会话的工具当场报错。

  /v1/mcp/status 的 TestClient 不用 with（lifespan 会开真连接池并触发 30s+ 的
  模型加载，test_api_docs.py 同款约定）。本机若真有 mem_mcp.py 在跑，status 会
  返回真数据——断言只盯形状，不盯具体值。
"""

from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient

from app import mcp_ctl
from app.api.main import app

client = TestClient(app)      # ★ 不用 with：lifespan 会开真连接池（30s+ 模型加载）


def _row(pid, parent, cmdline, name="python.exe", created="2026-11-16T14:00:00"):
    """一行假的 Win32_Process（_scan_processes 输出的形状）。"""
    return {"ProcessId": pid, "ParentProcessId": parent, "Name": name,
            "CommandLine": cmdline, "CreationDate": created}


# ── 进程匹配与孤儿判定 ──────────────────────────────────────────────

def test_mcp_processes_match_and_orphan(tmp_path, monkeypatch):
    script = tmp_path / "mem_mcp.py"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(mcp_ctl, "MCP_SCRIPT", script)
    tail = str(script).replace("\\", "/").lower()
    fake = [
        _row(50, 1, "claude.exe --vscode-extension-host", name="claude.exe"),
        _row(100, 50, f"python {tail}"),                      # 活的：父进程在列表里
        _row(200, 99, f"python {tail}"),                      # 孤儿：父进程 99 已退出
        _row(300, 2, "python C:/other-project/mem_mcp.py"),   # 别的项目的同名脚本
        _row(400, 2, "python -m app.worker"),                 # 无关进程
    ]

    procs = mcp_ctl._mcp_processes(fake)

    by_pid = {p["pid"]: p for p in procs}
    assert set(by_pid) == {100, 200}
    assert by_pid[100]["orphan"] is False
    assert by_pid[100]["parent_name"] == "claude.exe"
    assert by_pid[200]["orphan"] is True


def test_mcp_processes_relative_invocation_with_project_dir(tmp_path, monkeypatch):
    """相对路径启动（cmdline 不含绝对脚本路径）但目录名是本项目 → 认。"""
    script = tmp_path / "mem_mcp.py"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(mcp_ctl, "MCP_SCRIPT", script)
    fake = [_row(7, 1, "python C:/Users/me/code/mem-xy/integrations/mcp-server/mem_mcp.py")]
    assert [p["pid"] for p in mcp_ctl._mcp_processes(fake)] == [7]


# ── PowerShell 扫描：失败降级 + 单行结果兼容 ──────────────────────────

def test_scan_processes_failure_returns_empty(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=15)

    monkeypatch.setattr(mcp_ctl.subprocess, "run", boom)
    assert mcp_ctl._scan_processes() == []


def test_scan_processes_single_row_becomes_list(monkeypatch):
    """ConvertTo-Json 对单元素给对象不给数组——必须兼容，否则状态页面漏进程。"""
    class _Proc:
        returncode = 0
        stdout = ('{"ProcessId":1,"ParentProcessId":2,"Name":"python.exe",'
                  '"CommandLine":"python mem_mcp.py","CreationDate":"2026-11-16T14:00:00"}')

    monkeypatch.setattr(mcp_ctl.subprocess, "run", lambda *a, **k: _Proc())
    rows = mcp_ctl._scan_processes()
    assert len(rows) == 1 and rows[0]["ProcessId"] == 1


# ── 注册扫描：只回布尔，不回内容 ────────────────────────────────────

def test_scan_registration_registered_and_points_here(tmp_path, monkeypatch):
    script = tmp_path / "mem_mcp.py"
    script.write_text("# stub", encoding="utf-8")
    monkeypatch.setattr(mcp_ctl, "MCP_SCRIPT", script)
    registered = tmp_path / "claude.json"
    registered.write_text(
        '{"mcpServers":{"mem-xy":{"args":["%s"]}}}'
        % str(script).replace("\\", "/"),
        encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text('{"mcpServers":{"x":{"args":["C:/elsewhere/mem_mcp.py"]}}}',
                         encoding="utf-8")
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(mcp_ctl, "_REGISTER_FILES", [registered, elsewhere, missing])

    rows = mcp_ctl._scan_registration()

    assert rows[0]["registered"] and rows[0]["points_here"]
    assert rows[1]["registered"] and not rows[1]["points_here"]   # 注册了但指向别处
    assert rows[2]["registered"] is False and rows[2]["exists"] is False


# ── DB 活动：失败降级 ───────────────────────────────────────────────

def test_activity_failure_degrades_to_none(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(mcp_ctl.db, "query_one", boom)
    assert mcp_ctl._activity() == {"recalls_7d": None, "last_recall_at": None}


# ── verdict 判定 ────────────────────────────────────────────────────

def _patch_status(monkeypatch, *, procs, registration, script_exists=True):
    script = mcp_ctl.PROJECT_ROOT / "integrations" / "mcp-server" / "mem_mcp.py"
    monkeypatch.setattr(mcp_ctl, "MCP_SCRIPT", script if script_exists
                        else mcp_ctl.PROJECT_ROOT / "no" / "such.py")
    monkeypatch.setattr(mcp_ctl, "_mcp_processes", lambda processes=None: procs)
    monkeypatch.setattr(mcp_ctl, "_scan_registration", lambda: registration)
    monkeypatch.setattr(mcp_ctl, "_activity",
                        lambda: {"recalls_7d": 3, "last_recall_at": None})


def test_verdict_broken_when_script_missing(tmp_path, monkeypatch):
    _patch_status(monkeypatch, procs=[], registration=[], script_exists=False)
    assert mcp_ctl.status()["verdict"] == "broken"


def test_verdict_orphaned_when_only_orphans(monkeypatch):
    orphans = [{"pid": 9, "started_at": None, "parent_pid": None,
                "parent_name": None, "orphan": True}]
    _patch_status(monkeypatch, procs=orphans,
                  registration=[{"registered": True, "points_here": True}])
    assert mcp_ctl.status()["verdict"] == "orphaned"


def test_verdict_ready_when_registered_but_no_session(monkeypatch):
    _patch_status(monkeypatch, procs=[],
                  registration=[{"registered": True, "points_here": True}])
    st = mcp_ctl.status()
    assert st["verdict"] == "ready"
    assert st["config_snippet"].startswith("{")   # 复制按钮的接入配置随状态一起给


def test_verdict_unregistered(monkeypatch):
    _patch_status(monkeypatch, procs=[],
                  registration=[{"registered": False, "points_here": False}])
    assert mcp_ctl.status()["verdict"] == "unregistered"


# ── stop：默认只杀孤儿 ──────────────────────────────────────────────

def test_stop_only_kills_orphans_by_default(monkeypatch):
    procs = [{"pid": 1, "orphan": False}, {"pid": 2, "orphan": True},
             {"pid": 3, "orphan": True}]
    killed = []
    monkeypatch.setattr(mcp_ctl, "_mcp_processes", lambda processes=None: procs)
    monkeypatch.setattr(mcp_ctl, "_kill", lambda pid: killed.append(pid) or True)

    out = mcp_ctl.stop(orphans_only=True)
    assert killed == [2, 3]
    assert out["killed"] == [2, 3] and out["orphans_only"] is True

    killed.clear()
    mcp_ctl.stop(orphans_only=False)
    assert killed == [1, 2, 3]


# ── HTTP 层：形状 + 错误翻译 ────────────────────────────────────────

def test_api_mcp_status_shape():
    r = client.get("/v1/mcp/status")
    assert r.status_code == 200
    body = r.json()
    assert {"verdict", "script", "package", "processes", "live_count",
            "orphan_count", "registration", "activity", "config_snippet",
            "note"} <= set(body)
    assert body["verdict"] in {"running", "orphaned", "ready", "unregistered", "broken"}


def test_api_mcp_stop_translates_args(monkeypatch):
    seen = {}

    def fake_stop(orphans_only=True):
        seen["orphans_only"] = orphans_only
        return {"killed": [], "orphans_only": orphans_only, "processes": []}

    monkeypatch.setattr(mcp_ctl, "stop", fake_stop)
    r = client.post("/v1/mcp/stop", json={"orphans_only": True})
    assert r.status_code == 200 and seen["orphans_only"] is True

    r = client.post("/v1/mcp/stop", json={})          # 缺省 = 只清孤儿
    assert r.status_code == 200 and seen["orphans_only"] is True

    r = client.post("/v1/mcp/stop", json={"orphans_only": False})
    assert r.status_code == 200 and seen["orphans_only"] is False
