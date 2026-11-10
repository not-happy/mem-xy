"""backup_daily（backend 内置每日备份调度）单元测试。

不触库、不真跑备份：subprocess 一律 monkeypatch。
背景：SASE 拦计划任务链的 pg_dump（decisions-log #100），调度逻辑本身必须可靠。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import pytest

from app import backup_daily


@pytest.fixture()
def bdir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM_BACKUP_DIR", str(tmp_path))
    return tmp_path


def _touch(directory, name: str) -> None:
    (directory / name).write_bytes(b"x")


class TestLatestBackupDate:
    def test_no_directory_returns_none(self, bdir):
        assert backup_daily.latest_backup_date() is None

    def test_empty_directory_returns_none(self, bdir):
        assert backup_daily.latest_backup_date() is None

    def test_picks_max_date_and_ignores_junk(self, bdir):
        _touch(bdir, "memxy-20261109-120000.7z.001")
        _touch(bdir, "memxy-20261110-120000.7z.001")
        _touch(bdir, "memxy-20261110-130000.dump")  # 明文 dump 也算"当日有备份"
        _touch(bdir, "notes.txt")                    # 无关文件忽略
        assert backup_daily.latest_backup_date() == "20261110"


class TestDue:
    NOW = datetime(2026, 11, 10, 12, 30, 0)

    def test_before_trigger_time_is_not_due(self):
        assert backup_daily.due("12:31", self.NOW) is False

    def test_exact_trigger_time_is_due(self):
        assert backup_daily.due("12:30", self.NOW) is True

    def test_after_trigger_time_is_due(self):
        assert backup_daily.due("09:00", self.NOW) is True

    @pytest.mark.parametrize("at", ["", "off", "OFF", "none", "  off "])
    def test_disabled_values_are_never_due(self, at):
        assert backup_daily.due(at, self.NOW) is False

    def test_malformed_value_is_not_due_and_logs_error(self, caplog):
        with caplog.at_level(logging.ERROR, logger="app.backup_daily"):
            assert backup_daily.due("每天中午", self.NOW) is False
        assert "格式非法" in caplog.text


class TestRunBackupOnce:
    def test_exit_zero_is_success(self, monkeypatch, caplog):
        calls = {}

        def fake_run(cmd, cwd):
            calls["cmd"] = cmd
            calls["cwd"] = cwd
            return type("R", (), {"returncode": 0})()

        monkeypatch.setattr(backup_daily.subprocess, "run", fake_run)
        with caplog.at_level(logging.INFO, logger="app.backup_daily"):
            assert backup_daily.run_backup_once() is True
        assert calls["cmd"][-2:] == [str(backup_daily.BACKUP_SCRIPT), "backup"]
        assert calls["cmd"][0].endswith("powershell.exe")
        assert "完成" in caplog.text

    def test_nonzero_exit_is_failure_and_logs_loudly(self, monkeypatch, caplog):
        monkeypatch.setattr(backup_daily.subprocess, "run",
                            lambda cmd, cwd: type("R", (), {"returncode": 1})())
        with caplog.at_level(logging.ERROR, logger="app.backup_daily"):
            assert backup_daily.run_backup_once() is False
        assert "失败" in caplog.text
        assert "SASE" in caplog.text  # 报错必须指路到已知根因


class TestStart:
    @pytest.mark.parametrize("at", ["", "off", "none"])
    def test_disabled_does_not_spawn_thread(self, at):
        before = threading.active_count()
        backup_daily.start(at)
        assert threading.active_count() == before

    def test_enabled_spawns_daemon_thread(self, monkeypatch):
        started = {}

        class FakeThread:
            def __init__(self, target, args, name, daemon):
                started.update(name=name, daemon=daemon)

            def start(self):
                pass

        monkeypatch.setattr(backup_daily.threading, "Thread", FakeThread)
        backup_daily.start("12:30")
        assert started["name"] == "daily-backup"
        assert started["daemon"] is True
