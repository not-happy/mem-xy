"""Pytest bootstrap: sys.path, isolated test database, DB-gated helpers.

★ 测试一律跑在独立测试库 memxy_test（decisions-log #97 根治 / #103）：
旧方案 pytest 与常驻 worker 共用生产库——jobs 的 claim 是全局竞争（org 前缀
防火墙对它无效），test_jobs_queue 的 clean_jobs 还是 `DELETE FROM jobs` 全表删
（跑一次测试就清掉生产队列）；全量测试偶发失败复现率 ~1/10–1/20 且失败点漂移。
现在 conftest 在收集期重建 memxy_test（DROP + CREATE + alembic upgrade head），
把 MEM_DSN 指向测试库——pytest 与生产数据物理隔离，跑测试**不再需要停 worker**。

安全闸：只有 dbname == "memxy_test" 的 DSN 才会被 DROP+CREATE；`MEM_TEST_DSN`
可显式指定其他库作测试目标（此时只补迁移、绝不重建），杜绝误配删生产库。
测试库初始化失败时大声抛错，**绝不静默降级回生产库**（降级 = 回到 #97 老路）。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

import pytest

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_REAL_HOME = pathlib.Path(os.environ.get("MEM_HOME", pathlib.Path.home() / ".memxy"))
TEST_DBNAME = "memxy_test"

# ★ 在线服务（localhost:8787）连的库 = 生产库。pytest 的 MEM_DSN 已被指向测试库，
# 但端到端测试（test_mcp_server）的写入走 HTTP → 服务自己的连接，数据落在服务库；
# 清理和断言必须对准同一个库，故在覆盖 MEM_DSN 之前把生产 DSN 存下来（#103）。
SERVICE_DSN: str | None = None

# ★ Set before any `app.*` import: config.py resolves CONFIG_DIR at import time.
# Keeps tests deterministic — a developer's real ~/.memxy must not leak in.
os.environ.setdefault("MEM_HOME", tempfile.mkdtemp(prefix="mem-test-home-"))


def _dsn_from_real_secrets() -> str | None:
    """The DSN is the one setting tests *do* want from the developer's real home.

    MEM_HOME is redirected to a temp dir above so config.toml cannot leak into
    test_config.py, but that would also hide secrets.toml and silently skip every
    DB-backed test. Surfacing the DSN as MEM_DSN keeps both properties: env is
    config.py's highest-priority source, and test_config.py deletes it per test.
    """
    secrets_file = _REAL_HOME / "secrets.toml"
    if not secrets_file.is_file():
        return None
    import tomllib

    with secrets_file.open("rb") as fh:
        return tomllib.load(fh).get("db", {}).get("dsn")


def _prod_dsn() -> str | None:
    """生产（源）DSN：env MEM_DSN > 真实 ~/.memxy/secrets.toml [db].dsn。"""
    return os.environ.get("MEM_DSN") or _dsn_from_real_secrets()


def _derive_test_dsn(prod: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(prod)
    parts["dbname"] = TEST_DBNAME
    return make_conninfo(**parts)


def _alembic_upgrade(dsn: str) -> None:
    # 子进程跑迁移：alembic env.py 会 fileConfig(alembic.ini) 重配 logging，
    # 进程内 command API 会干扰 pytest 的 caplog；子进程零副作用。
    env = {**os.environ, "MEM_DSN": dsn}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head 失败（测试库迁移）:\n{proc.stdout}\n{proc.stderr}")


def _rebuild_test_db(test_dsn: str) -> None:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(test_dsn)
    if parts.get("dbname") != TEST_DBNAME:
        # MEM_TEST_DSN 显式指向别的库：只补迁移，绝不 DROP（防误删安全闸）
        _alembic_upgrade(test_dsn)
        return

    import psycopg

    maint = make_conninfo(**{**parts, "dbname": "postgres"})
    with psycopg.connect(maint, autocommit=True, connect_timeout=5) as conn:
        # WITH (FORCE)：踢掉残留连接（上次 pytest 崩溃遗留的池），PG 13+
        conn.execute(f'DROP DATABASE IF EXISTS {TEST_DBNAME} WITH (FORCE)')
        conn.execute(f"CREATE DATABASE {TEST_DBNAME}")
    _alembic_upgrade(test_dsn)


def _seed_test_db() -> None:
    """orgs/endpoints/match_rules/doc_sections 种子不在迁移里（bootstrap 命令写入），
    测试库重建后必须补种，否则外键（如 raw_observations.section_key）全炸。
    with_embeddings=False：跳过 bge-m3 加载（15–33s），测试不依赖真向量。"""
    from app.core import seed

    seed.seed_all(with_embeddings=False)


def _setup_test_db() -> bool:
    """建/重建测试库并把 MEM_DSN 指过去；生产不可达时返回 False（全 skip）。"""
    global SERVICE_DSN

    prod = _prod_dsn()
    if not prod:
        return False
    try:
        import psycopg

        with psycopg.connect(prod, connect_timeout=3):
            pass
    except Exception:
        return False

    SERVICE_DSN = prod
    test_dsn = os.environ.get("MEM_TEST_DSN") or _derive_test_dsn(prod)
    try:
        _rebuild_test_db(test_dsn)
        os.environ["MEM_DSN"] = test_dsn   # 之后所有 app.*（含 seed）都指向测试库
        _seed_test_db()
    except Exception as exc:
        raise RuntimeError(
            "测试库初始化失败（绝不降级回生产库跑测试，见 decisions-log #103）") from exc
    return True


DB_AVAILABLE = _setup_test_db()

requires_db = pytest.mark.skipif(
    not DB_AVAILABLE,
    reason="no reachable database: set MEM_DSN or put [db].dsn in ~/.memxy/secrets.toml",
)
