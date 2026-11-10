"""后台 worker 的 Web UI 启停（§13 P2 的运行 half）。

★ worker 是独立进程（`python -m app.worker`），以前只能命令行起停；现在由后端
  代管：pid 文件落在 `~/.memxy/run/`，存活判定走 Win32 API（★ Windows 上
  `os.kill(pid, 0)` 会真的杀进程，不能用），停止用 `taskkill`。
  后端重启后凭 pid 文件仍能看见/停掉存量 worker——不装看不见。
★ 上限 4：网关并发探测的结论是 2 稳（§14 item 2），留一倍余量但不再压。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

RUN_DIR = Path.home() / ".memxy" / "run"
MAX_WORKERS = 4
_BACKEND_ROOT = Path(__file__).resolve().parents[1]

# ★ worker-N.log 以 "ab" 追加、从不轮转，是单机上最可能静默吃满盘的部件
#   （EXPERT-REVIEW B5/§5.2.5）。启动时超过阈值就滚动一次，保留一代 .old。
MAX_LOG_BYTES = 20 * 1024 * 1024

_STILL_ACTIVE = 259
_KERNEL32 = ctypes.windll.kernel32 if os.name == "nt" else None


def _rotate_log(path: Path) -> None:
    """超过 `MAX_LOG_BYTES` 就把 log 滚动成 `<name>.old`（覆盖上一代）。"""
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.replace(path.with_name(path.name + ".old"))
    except OSError:
        pass                               # 轮转失败不拦启动，日志继续追加


def _alive(pid: int) -> bool:
    if _KERNEL32 is None:                      # 非 Windows 兜底
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = _KERNEL32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code))
    _KERNEL32.CloseHandle(handle)
    return bool(ok) and code.value == _STILL_ACTIVE


def _pid_files() -> list[tuple[int, Path]]:
    """[(slot, path)]，按 slot 排序；pid 文件坏了一行删掉。"""
    if not RUN_DIR.exists():
        return []
    out = []
    for p in sorted(RUN_DIR.glob("worker-*.pid")):
        try:
            out.append((int(p.stem.split("-")[1]), p))
        except ValueError:
            p.unlink(missing_ok=True)
    return out


def status() -> list[dict]:
    rows = []
    for slot, path in _pid_files():
        try:
            pid = int(path.read_text().strip())
        except (ValueError, OSError):
            path.unlink(missing_ok=True)
            continue
        rows.append({"slot": slot, "pid": pid, "alive": _alive(pid),
                     "log": str(RUN_DIR / f"worker-{slot}.log")})
    return rows


def _reap_dead() -> None:
    """清掉死进程的 pid 文件：槽位可以被 start() 复用，状态列表也不再显示尸体。"""
    for _slot, path in _pid_files():
        try:
            pid = int(path.read_text().strip())
        except (ValueError, OSError):
            path.unlink(missing_ok=True)
            continue
        if not _alive(pid):
            path.unlink(missing_ok=True)


def start(count: int = 2) -> list[dict]:
    """拉起 count 个 worker；存活+新增超上限就拒绝（返回 ValueError 文案）。"""
    _reap_dead()
    live = [w for w in status() if w["alive"]]
    if len(live) + count > MAX_WORKERS:
        raise ValueError(f"已有 {len(live)} 个 worker 在跑，上限 {MAX_WORKERS}")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    taken = {slot for slot, _ in _pid_files()}
    env = dict(os.environ, HF_HUB_OFFLINE="1")     # bge-m3 只吃本地缓存，别碰网
    for _ in range(count):
        slot = next(i for i in range(MAX_WORKERS) if i not in taken)
        taken.add(slot)
        log_path = RUN_DIR / f"worker-{slot}.log"
        _rotate_log(log_path)
        log = open(log_path, "ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.worker"], cwd=_BACKEND_ROOT, env=env,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.close()                                # 子进程自己持有一份
        (RUN_DIR / f"worker-{slot}.pid").write_text(str(proc.pid))
    return status()


def ensure(target: int) -> list[dict]:
    """把存活 worker 补齐到 `target` 个；已达标就一个不动。返回补齐后的状态。

    ★ 与 `start(count)` 的区别：start 是"再加 count 个"（UI 按钮语义，超上限报错）；
      ensure 是"至少要有 target 个"（后端启动时自启语义，幂等、不报错）——
      重启后存量 worker 可能还活着（pid 文件在），ensure 不会重复叠。
    """
    _reap_dead()
    live = sum(1 for w in status() if w["alive"])
    need = max(0, min(target, MAX_WORKERS) - live)
    if need:
        start(need)
    return status()


def stop_all() -> list[int]:
    """taskkill 全部 pid 文件里的 worker（活着的才杀），返回被杀的 pid。"""
    killed = []
    for _slot, path in _pid_files():
        try:
            pid = int(path.read_text().strip())
        except (ValueError, OSError):
            path.unlink(missing_ok=True)
            continue
        if _alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
            killed.append(pid)
        path.unlink(missing_ok=True)
    return killed
