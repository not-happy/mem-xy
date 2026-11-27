"""MCP 接入的运行时探视与孤儿清理（`GET/POST /v1/mcp*` 的实现体）。

★ 与 `workers_ctl.py` 的本质区别：worker 是**后端代管**的独立进程（pid 文件在手，
  可以启停）；MCP server（`integrations/mcp-server/mem_mcp.py`）是 **AI 客户端
  （claude.exe 等）按会话拉起的 stdio 子进程**——服务端既不该替它"启动"（没有客户端
  连着，起了也只挂在 stdin 上空转），也不能真正"停止"（杀了活进程只是让那个会话的
  工具报错，客户端下次要用还会自己再拉）。所以这里的职责收窄为三件看得见摸得着的：

  1. `status()`      —— 谁在跑（扫 mem_mcp.py 进程 + 父进程）、能不能跑（脚本/mcp 包/
                        客户端注册）、跑得怎么样（recall_log 里 source='mcp' 的近期活动）。
  2. `stop()`        —— 结束进程。默认只清**孤儿**（父进程已退出的 mem_mcp.py——客户端
                        没了，adapter 还挂着是纯残留）；`orphans_only=False` 才杀活进程。
  3. `selftest()`    —— 真·连接自检：起一个真 stdio 客户端走一遍 initialize +
                        list_tools，回答"从零拉起到能列出 5 个工具到底通不通"。

★ 扫进程不装 psutil：`Get-CimInstance Win32_Process` 一把全量拉回来（含 CommandLine/
  ParentProcessId），后端本来就在 Windows 上跑，PowerShell 一发 1~2s，结果缓存
  `SCAN_TTL_SECONDS` 免得 UI 轮询打爆。失败一律返回空列表 + error 字段，探视不许 500。
★ 注册扫描只回答"注册了吗、指没指向本项目的脚本"——`~/.claude.json` 里有 token 和
  别的项目的隐私，绝不回传文件内容，只回传命中与否（§2.1 的红线同样的道理）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .core import db

log = logging.getLogger(__name__)

# integrations/mcp-server/mem_mcp.py 的绝对路径（backend/ 的上一级的上一级）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_SCRIPT = PROJECT_ROOT / "integrations" / "mcp-server" / "mem_mcp.py"

# 这些客户端配置文件里找 "mem-xy"/"mem_mcp" 的引用（mem_mcp.py 文档写的接入位置）。
_REGISTER_FILES = [
    Path.home() / ".claude.json",
    PROJECT_ROOT / ".mcp.json",
]

SCAN_TTL_SECONDS = 10.0
_scan_cache: tuple[float, list[dict]] = (0.0, [])
_scan_lock = threading.Lock()

# selftest 的总预算：起 python 进程 ~1.5s + 握手，远超就是挂了（见 mem_mcp.py 的 15s）。
SELFTEST_TIMEOUT_SECONDS = 20.0
EXPECTED_TOOLS = {"memory_recall", "memory_note", "memory_correct",
                  "memory_search", "memory_pending"}


# ─────────────────────────── 进程扫描 ───────────────────────────

def _scan_processes() -> list[dict]:
    """全量进程 + 命令行（PowerShell CIM）。★ 失败返回空列表，绝不让状态端点 500。

    ★ CreationDate 现场格式化：WinPS 5.1 的 ConvertTo-Json 会把 DateTime 序列化成
      "/Date(1699…)/"，那种值 isoformat 不了——直接在 PS 里转成 ISO 字符串拿回来。
    """
    ps = ("Get-CimInstance Win32_Process | "
          "Select-Object ProcessId,ParentProcessId,Name,CommandLine,"
          "@{n='CreationDate';e={$_.CreationDate.ToString('yyyy-MM-ddTHH:mm:ss')}}"
          " | ConvertTo-Json -Compress")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = json.loads(proc.stdout) if proc.returncode == 0 else []
    except Exception:  # noqa: BLE001 — CIM 挂了（被安全软件拦/超时）就当看不见进程
        log.warning("process scan failed", exc_info=True)
        return []
    if isinstance(rows, dict):      # ConvertTo-Json 单元素时给对象不给数组
        rows = [rows]
    return [r for r in rows if isinstance(r, dict)]


def _mcp_processes(processes: list[dict] | None = None) -> list[dict]:
    """命令行含 mem_mcp.py 的进程。★ 按脚本全路径后缀匹配，别的项目同名脚本不算。"""
    if processes is None:
        global _scan_cache
        with _scan_lock:
            now = time.monotonic()
            if now - _scan_cache[0] > SCAN_TTL_SECONDS:
                _scan_cache = (now, _scan_processes())
            processes = _scan_cache[1]

    script_tail = str(MCP_SCRIPT).replace("\\", "/").lower()
    by_pid = {int(r["ProcessId"]): r for r in processes if r.get("ProcessId") is not None}
    out = []
    for pid, row in by_pid.items():
        cmdline = str(row.get("CommandLine") or "").replace("\\", "/").lower()
        # 命中文件名 + 归属本项目：注册写法是绝对路径（script_tail 命中）；相对路径
        # 启动时认项目目录名 mem-xy。别的项目就算也有 mem_mcp.py 也不算。
        if "mem_mcp.py" not in cmdline:
            continue
        if script_tail not in cmdline and "mem-xy" not in cmdline:
            continue
        parent = by_pid.get(int(row.get("ParentProcessId") or 0))
        out.append({
            "pid": pid,
            "started_at": _ctime(row.get("CreationDate")),
            "parent_pid": int(row.get("ParentProcessId") or 0) or None,
            "parent_name": parent.get("Name") if parent else None,
            # 孤儿 = 父进程已退出（客户端窗口关了，adapter 还挂着）
            "orphan": parent is None,
        })
    out.sort(key=lambda x: x["pid"])
    return out


def _ctime(value) -> str | None:
    """CIM 的 CreationDate 是 datetime；序列化失败就别让整个状态挂掉。"""
    try:
        return value.isoformat(timespec="seconds") if value else None
    except Exception:  # noqa: BLE001
        return None


def _kill(pid: int) -> bool:
    proc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                          capture_output=True, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc.returncode == 0


# ─────────────────────────── 注册扫描 ───────────────────────────

def _scan_registration() -> list[dict]:
    """★ 只回"有没有注册、命令指没指向本项目"，绝不回传配置内容（那是别人的隐私）。"""
    rows = []
    for path in _REGISTER_FILES:
        entry: dict = {"path": str(path), "exists": path.exists(), "registered": False,
                       "points_here": False}
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
                registered = "mem-xy" in raw or "mem_mcp" in raw
                points_here = str(MCP_SCRIPT).replace("\\", "/") in raw.replace("\\", "/")
                entry.update(registered=registered, points_here=points_here)
            except (OSError, ValueError):
                entry["error"] = "读取失败（权限/编码）"
        rows.append(entry)
    return rows


# ─────────────────────────── 近期活动（recall_log） ───────────────────────────

def _activity() -> dict:
    """source='mcp' 的召回审计 = "模型最近有没有真在用 MCP"。查库失败就 None，不拖垮状态。"""
    try:
        row = db.query_one(
            "SELECT count(*) AS n, max(created_at) AS last_at"
            "  FROM recall_log"
            " WHERE source = 'mcp' AND created_at >= now() - interval '7 days'")
        return {"recalls_7d": int(row["n"]),
                "last_recall_at": row["last_at"].isoformat(timespec="seconds")
                if row["last_at"] else None}
    except Exception:  # noqa: BLE001 — DB 没起/表不在：探视显示"未知"而不是 500
        log.warning("mcp activity query failed", exc_info=True)
        return {"recalls_7d": None, "last_recall_at": None}


# ─────────────────────────── 汇总 ───────────────────────────

def status() -> dict:
    script_ok = MCP_SCRIPT.exists()
    try:
        pkg_version = version("mcp")
        pkg_ok = True
    except PackageNotFoundError:
        pkg_version, pkg_ok = None, False

    procs = _mcp_processes()
    registration = _scan_registration()
    activity = _activity()
    alive = [p for p in procs if not p["orphan"]]
    orphans = [p for p in procs if p["orphan"]]

    if not script_ok or not pkg_ok:
        verdict = "broken"
    elif alive:
        verdict = "running"
    elif orphans:
        verdict = "orphaned"       # 只剩父进程已退出的残留——UI 提示可清理
    elif registration and any(r["registered"] for r in registration):
        verdict = "ready"          # 注册了但当前没有客户端会话开着它——正常
    else:
        verdict = "unregistered"

    # 复制按钮用的接入配置（与 ~/.claude.json 里已注册的写法一致，路径正斜杠跨工具）。
    snippet = {"mem-xy": {"type": "stdio", "command": "python",
                          "args": [str(MCP_SCRIPT).replace("\\", "/")], "env": {}}}

    return {
        "verdict": verdict,        # running / orphaned / ready / unregistered / broken
        "script": {"path": str(MCP_SCRIPT), "exists": script_ok},
        "package": {"installed": pkg_ok, "version": pkg_version},
        "processes": procs,
        "live_count": len(alive),
        "orphan_count": len(orphans),
        "registration": registration,
        "activity": activity,
        "config_snippet": json.dumps(snippet, ensure_ascii=False, indent=2),
        "note": "stdio MCP 由 AI 客户端在会话启动时自动拉起，无需也不应在服务端手动启动；"
                "这里的「停止」只清父进程已退出的孤儿残留。",
    }


def stop(orphans_only: bool = True) -> dict:
    """结束 mem_mcp.py 进程。★ 默认只杀孤儿：杀活进程只会让客户端会话的工具报错。"""
    global _scan_cache
    procs = _mcp_processes()
    targets = [p for p in procs if p["orphan"]] if orphans_only else procs
    killed = [p["pid"] for p in targets if _kill(p["pid"])]
    with _scan_lock:                   # 刚杀过，缓存必然过期
        _scan_cache = (0.0, [])
    return {"killed": killed, "orphans_only": orphans_only, "processes": _mcp_processes()}


# ─────────────────────────── 真·连接自检 ───────────────────────────

def selftest() -> dict:
    """起一个**真** stdio 客户端：initialize → list_tools → 断开，量全程耗时。

    ★ 跟 test_mcp_server.py 同一套做法（mcp 客户端库已在依赖里），只是从页面触发。
      在独立线程里跑 asyncio——backend 的 uvicorn loop 不该被 anyio 任务组污染。
    """
    result: dict = {}

    def runner() -> None:
        try:
            result.update(asyncio.run(_selftest_async()))
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, error=f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=runner, name="mcp-selftest", daemon=True)
    thread.start()
    thread.join(SELFTEST_TIMEOUT_SECONDS + 5)
    if not result and thread.is_alive():
        result = {"ok": False, "error": f"自检 {SELFTEST_TIMEOUT_SECONDS + 5}s 没跑完（疑似挂起）"}
    result.setdefault("latency_ms", None)
    return result


async def _selftest_async() -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    started = time.perf_counter()
    # ★ env 带上完整 os.environ（test_mcp_server.py 同款做法）：只给 MEMXY_URL 的话，
    #   Windows 上子进程 python 连 SystemRoot 都没有，起都起不来。
    params = StdioServerParameters(
        command=sys.executable, args=[str(MCP_SCRIPT)],
        env={**os.environ, "MEMXY_URL": "http://127.0.0.1:8787"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
    names = sorted(t.name for t in tools)
    return {"ok": set(names) == EXPECTED_TOOLS, "tools": names,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": None if set(names) == EXPECTED_TOOLS
            else f"工具列表不符：期望 {sorted(EXPECTED_TOOLS)}，实得 {names}"}
