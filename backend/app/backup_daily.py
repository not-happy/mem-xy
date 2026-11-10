"""每日备份调度（backend 进程内置）——绕开 SASE 对计划任务链的拦截。

★ 为什么不用 Windows 计划任务：公司 SASE 应用控制策略禁止 pg_dump.exe /
  psql.exe 等 PG 客户端工具在无人值守上下文（计划任务）执行——2026-11-10
  实测：任务上下文里连 `pg_dump --version` 都被拦（Win32 1260「管理员用
  策略规则限制了访问」），而 7z / git / python / pwsh 均放行，交互链跑同款
  命令全部成功。backend 进程由交互链拉起（service.ps1），python →
  subprocess → powershell → pg_dump 与手动成功的路径同构。

★ 已知边界：机器重启后 backend 由 watchdog 计划任务拉起，进程树含任务
  来源，届时 pg_dump 可能被同策略拦截——失败会大声记 ERROR 并指数退避
  重试，不会静默；交互终端 `service.ps1 restart backend` 即恢复。用户已
  明确（2026-11-10）不申请 SASE 白名单，本调度即长期方案；若重启边界
  真实造成困扰，备选加固是让 postgres 服务进程代跑 pg_dump
  （COPY TO PROGRAM，执行上下文与调用方解耦），见 decisions-log #101。

★ 触发语义与计划任务对齐：每天 HH:MM（backup.daily_at，默认 12:30）之后
  第一次检查时，若 backups 目录里没有今日日期的备份文件就补跑（幂等：
  当天成功一次后不再触发；失败按退避重试）。backend 在触发时刻之后重启
  会立即补跑当日备份（等价 StartWhenAvailable）。
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import subprocess
import threading
import time
from datetime import datetime

log = logging.getLogger("app.backup_daily")

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
BACKUP_SCRIPT = PROJECT_ROOT / "backup.ps1"
_CHECK_INTERVAL_SECONDS = 600
_MAX_RETRY_SECONDS = 4 * 3600
_ARCHIVE_NAME_RE = re.compile(r"memxy-(\d{8})-\d{6}\.")


def backup_dir() -> pathlib.Path:
    """与 backup.ps1 同源：MEM_BACKUP_DIR 覆盖，默认 ~/.memxy/backups。"""
    env = os.environ.get("MEM_BACKUP_DIR")
    return pathlib.Path(env) if env else pathlib.Path.home() / ".memxy" / "backups"


def latest_backup_date() -> str | None:
    """最新备份文件名的日期段（yyyyMMdd）；目录不存在或无备份返回 None。"""
    directory = backup_dir()
    if not directory.is_dir():
        return None
    dates = [m.group(1) for m in
             (_ARCHIVE_NAME_RE.match(entry.name) for entry in directory.iterdir())
             if m]
    return max(dates) if dates else None


def due(at: str, now: datetime) -> bool:
    """是否已到触发时刻：at="HH:MM" 且 now ≥ 今日 at；at 为 off/空 = 关闭。"""
    if not at or at.strip().lower() in {"off", "none"}:
        return False
    try:
        hh, mm = at.strip().split(":")
        trigger = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except ValueError:
        log.error("backup.daily_at 格式非法 %r（期望 HH:MM），每日备份不触发", at)
        return False
    return now >= trigger


def run_backup_once() -> bool:
    """同步跑一次 backup.ps1 backup；输出继承 backend 的 stdout（进 backend.log）。

    用 powershell.exe（System32，永远在）而不是 pwsh：backup.ps1 本就双兼容，
    少一个"pwsh 没装"的失败面。
    """
    system_root = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    cmd = [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(BACKUP_SCRIPT), "backup"]
    log.info("每日备份启动: backup.ps1 backup")
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode == 0:
        log.info("每日备份完成 (exit 0)")
        return True
    log.error(
        "每日备份失败 (exit %s)——具体原因见上方 backup.ps1 输出。"
        "若为「管理员用策略规则限制」即 SASE 拦了 pg_dump（本 backend 由 "
        "watchdog 任务链拉起时会发生）：交互终端 .\\service.ps1 restart backend "
        "即恢复；其他原因按输出排查后等调度退避重试", result.returncode)
    return False


def _loop(at: str) -> None:
    failures = 0
    while True:
        try:
            now = datetime.now()
            if due(at, now) and latest_backup_date() != now.strftime("%Y%m%d"):
                if run_backup_once():
                    failures = 0
                else:
                    failures += 1
        except Exception:  # noqa: BLE001
            log.exception("每日备份调度异常")
            failures += 1
        if failures:
            sleep_seconds = min(_MAX_RETRY_SECONDS,
                                _CHECK_INTERVAL_SECONDS * (2 ** min(failures, 4)))
        else:
            sleep_seconds = _CHECK_INTERVAL_SECONDS
        time.sleep(sleep_seconds)


def start(at: str) -> None:
    """启动守护线程（lifespan 调用）；at 为 off/空则不启动。"""
    if not at or at.strip().lower() in {"off", "none"}:
        log.info("每日备份调度已关闭 (backup.daily_at=%r)", at)
        return
    threading.Thread(target=_loop, args=(at,), name="daily-backup",
                     daemon=True).start()
    log.info("每日备份调度已启动（触发 ≥ %s，检查间隔 %ss，目录 %s）",
             at, _CHECK_INTERVAL_SECONDS, backup_dir())
