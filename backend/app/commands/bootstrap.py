"""One-shot P0 bootstrap: create the database if missing, migrate, seed, verify.

Credentials come only from config (§2.1): `MEM_DSN` or `~/.memxy/secrets.toml`.
This module must never read another project's config files.

    python -m app.commands.bootstrap
"""

from __future__ import annotations

import logging
import pathlib
import sys
import time

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

log = logging.getLogger(__name__)


def _maintenance_conninfo(dsn: str) -> str:
    """Same credentials, but pointed at the `postgres` database — you cannot
    CREATE DATABASE while connected to the database being created."""
    import psycopg.conninfo as conninfo

    parts = conninfo.conninfo_to_dict(dsn)
    parts["dbname"] = "postgres"
    return conninfo.make_conninfo(**{k: v for k, v in parts.items() if v is not None})


def ensure_database(dsn: str) -> bool:
    """Returns True if the database was created by this call."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    target = conninfo_to_dict(dsn).get("dbname", "postgres")
    with psycopg.connect(_maintenance_conninfo(dsn), autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,)).fetchone()
        if row:
            print(f"[1/4] 数据库 {target} 已存在")
            return False
        # CREATE DATABASE cannot take parameters, and `target` comes from the operator's
        # own config — quote it as an identifier rather than interpolating raw text.
        from psycopg import sql

        conn.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(sql.Identifier(target)))
        print(f"[1/4] ✓ 已创建数据库 {target}")
        return True


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    print("[2/4] 执行迁移到 head")
    command.upgrade(cfg, "head")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # quiet the model loader's own progress spam
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

    from app.core import db, embedding, seed
    from app.core.config import get_settings

    settings = get_settings()
    if ensure_database(settings.dsn):
        import psycopg

        with psycopg.connect(settings.dsn, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    migrate()

    db.init_pool(min_size=1, max_size=2)
    try:
        server_version, pg_version, pgvector_version = db.server_version()
        print(f"[3/4] PostgreSQL {pg_version} / pgvector {pgvector_version or '未安装'}")
        if pgvector_version is None:
            print("      ✗ 缺 pgvector 扩展：CREATE EXTENSION vector 需要先装 pgvector")
            return 1

        started = time.perf_counter()
        seed.seed_all(with_embeddings=False)
        print(f"[4/4] seed 完成（{time.perf_counter() - started:.1f}s）")

        # The model must be resident and warm (§2.4 ②′): load it here so the section
        # match vectors are computed in this same process.
        print(f"      预热 {settings.embedding_model}（冷启动约 33s，会漂）…")
        service = embedding.get_embedding_service()
        service.load()
        print(f"      加载 {service.stats.load_seconds}s，"
              f"max_seq_length={service.stats.max_seq_length}，模块链 {service.stats.modules}")

        count = seed.recompute_section_embeddings()
        print(f"      栏位匹配向量：新增/更新 {count} 条")

        report = seed.verify()
        for key, value in report.items():
            if key != "ok":
                print(f"      {key}: {value}")
        if report.get("ok"):
            print("\n✓ P0 就绪（§13 验收：22 个栏位 + match_embedding 全部非空）")
            return 0
        print("\n✗ 自检未通过，见上")
        return 1
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
