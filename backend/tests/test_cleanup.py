"""§14-⑤ A9 保留期清理的验收：流水表滚窗口、原文表按 org 保留期、引用行不删。

Gated on MEM_DSN。cleanup 是唯一的清理入口（app/cleanup.py），这里验的是
"该删的删了、该留的留了"，尤其是被 memories / pending_items 引用的
conversation_log 行不能动（审计可追溯优先于清库干净）。
"""

import datetime as _dt

import pytest
from conftest import requires_db

from app.core import db as _db
from app.core.config import get_settings
from app import cleanup as cleanup_mod

pytestmark = requires_db

ORG = "pytest-cleanup-org"
UUID_PREFIX = "pytest-cleanup-"


@pytest.fixture
def db():
    _db.init_pool()
    _cleanup(_db)
    _db.execute("INSERT INTO orgs (org_key, name, enabled, retention_days)"
                " VALUES (%s,%s,true,NULL) ON CONFLICT (org_key) DO NOTHING",
                (ORG, "清理测试 org"))
    yield _db
    _cleanup(_db)


def _cleanup(db):
    # 顺序无所谓——逐表按标记删，孤儿行也顺带清掉
    db.execute("DELETE FROM recall_log WHERE query_text LIKE %s", (f"{UUID_PREFIX}%",))
    db.execute("DELETE FROM llm_usage WHERE task LIKE %s", (f"{UUID_PREFIX}%",))
    db.execute("DELETE FROM route_decisions WHERE raw_snippet LIKE %s", (f"{UUID_PREFIX}%",))
    db.execute("DELETE FROM pending_items WHERE source_msg IN"
               " (SELECT id FROM conversation_log WHERE source='pytest-cleanup')")
    db.execute("DELETE FROM memories WHERE source_msg IN"
               " (SELECT id FROM conversation_log WHERE source='pytest-cleanup')")
    db.execute("DELETE FROM conversation_log WHERE source='pytest-cleanup'")
    db.execute("DELETE FROM orgs WHERE org_key=%s", (ORG,))


def _msg(db, *, days_old, org=ORG, uuid=None):
    """插一条 conversation_log，msg_ts 拨到 days_old 天前，返回 id。"""
    uuid = uuid or f"{UUID_PREFIX}{days_old}-{_dt.datetime.now().timestamp()}"
    return _db.query_one(
        "INSERT INTO conversation_log (source, src_uuid, session_id, org_key, role,"
        "                             content, msg_ts)"
        " VALUES ('pytest-cleanup',%s,'pytest-cleanup',%s,'user','清理测试消息',"
        "         now() - make_interval(days => %s)) RETURNING id",
        (uuid, org, days_old))["id"]


# ─────────────────────── conversation_log：按 org 保留期 ───────────────────────

def test_old_unreferenced_messages_are_deleted_and_recent_kept(db):
    old = _msg(db, days_old=400)
    recent = _msg(db, days_old=10)

    with _db.transaction() as tx:
        deleted = cleanup_mod.cleanup(tx)
    assert isinstance(deleted, dict)

    ids = {r["id"] for r in _db.query(
        "SELECT id FROM conversation_log WHERE source='pytest-cleanup'")}
    assert old not in ids
    assert recent in ids


def test_the_org_retention_window_wins_over_the_fallback(db):
    _db.execute("UPDATE orgs SET retention_days=30 WHERE org_key=%s", (ORG,))
    mid = _msg(db, days_old=100)          # 过 30 天窗，但没过 180 天兜底窗

    with _db.transaction() as tx:
        cleanup_mod.cleanup(tx)

    ids = {r["id"] for r in _db.query(
        "SELECT id FROM conversation_log WHERE source='pytest-cleanup'")}
    assert mid not in ids                 # org 配置优先于兜底值


def test_retention_days_zero_means_never(db):
    _db.execute("UPDATE orgs SET retention_days=0 WHERE org_key=%s", (ORG,))
    ancient = _msg(db, days_old=4000)

    with _db.transaction() as tx:
        cleanup_mod.cleanup(tx)

    ids = {r["id"] for r in _db.query(
        "SELECT id FROM conversation_log WHERE source='pytest-cleanup'")}
    assert ancient in ids


def test_a_referenced_message_survives_even_past_its_window(db):
    """★ 硬约束：被 memories / raw_observations / pending_items / route_decisions
    的 source_msg 指着的 conversation_log 行不删（FK + 审计可追溯）。"""
    msg_id = _msg(db, days_old=400)
    _db.execute(
        "INSERT INTO pending_items (source_msg, stage, raw_content, org_key, project_key,"
        "                           dedup_hash)"
        " VALUES (%s,'route','pytest-cleanup-引用它的待办',%s,'dida/x',"
        "         'pytest-cleanup-dedup')", (msg_id, ORG))

    with _db.transaction() as tx:
        cleanup_mod.cleanup(tx)

    row = _db.query_one("SELECT id FROM conversation_log WHERE id=%s", (msg_id,))
    assert row is not None


# ─────────────────────── 流水表：固定 90 天滚动 ───────────────────────

def test_log_tables_roll_at_ninety_days(db):
    settings = get_settings()
    older = settings.cleanup_older_days
    _db.execute(
        "INSERT INTO recall_log (session_id, query_text, hits)"
        " VALUES ('pytest-cleanup','pytest-cleanup-新召回','[]'::jsonb)")
    old_day = _dt.datetime.now() - _dt.timedelta(days=older + 30)
    _db.execute("UPDATE recall_log SET created_at=%s WHERE query_text='pytest-cleanup-新召回'",
                (old_day,))
    stale_log_id = _db.query_one(
        "SELECT id FROM recall_log WHERE query_text='pytest-cleanup-新召回'")["id"]

    _db.execute(
        "INSERT INTO llm_usage (endpoint_key, model, task, input_tokens, output_tokens,"
        "                       latency_ms)"
        " VALUES ('test','test-model','pytest-cleanup-usage',1,1,1)")
    _db.execute("UPDATE llm_usage SET created_at=%s WHERE task='pytest-cleanup-usage'",
                (old_day,))
    _db.execute(
        "INSERT INTO route_decisions (source_msg, raw_snippet, prompt_ver, kind, confidence,"
        "                             model_output)"
        " VALUES (1,'pytest-cleanup-旧判定','v','atomic_fact',0.9,'{}'::jsonb)")
    _db.execute("UPDATE route_decisions SET created_at=%s"
                " WHERE raw_snippet='pytest-cleanup-旧判定'", (old_day,))

    with _db.transaction() as tx:
        deleted = cleanup_mod.cleanup(tx)

    assert deleted["recall_log"] >= 1
    assert deleted["llm_usage"] >= 1
    assert deleted["route_decisions"] >= 1
    assert _db.query_one("SELECT 1 FROM recall_log WHERE id=%s", (stale_log_id,)) is None
    # 新行不受影响
    assert _db.query_one("SELECT 1 FROM recall_log WHERE query_text='pytest-cleanup-新召回'") is None


def test_jobs_are_cleaned_only_when_finished_and_past_the_window(db):
    _db.execute("DELETE FROM jobs WHERE kind='cleanup-test'")
    _db.execute(
        "INSERT INTO jobs (kind, payload, status, finished_at)"
        " VALUES ('cleanup-test','{}','done', now() - interval '200 days')")
    done_old = _db.query_one("SELECT id FROM jobs WHERE kind='cleanup-test'")["id"]
    _db.execute(
        "INSERT INTO jobs (kind, payload, status, finished_at)"
        " VALUES ('cleanup-test','{}','done', now())")
    done_new = _db.query_one(
        "SELECT id FROM jobs WHERE kind='cleanup-test' AND finished_at IS NOT NULL"
        " ORDER BY id DESC LIMIT 1")["id"]
    _db.execute(
        "INSERT INTO jobs (kind, payload, status)"
        " VALUES ('cleanup-test','{}','pending')")   # 没完结的活 job 不清

    with _db.transaction() as tx:
        deleted = cleanup_mod.cleanup(tx)

    assert deleted["jobs"] >= 1
    assert _db.query_one("SELECT 1 FROM jobs WHERE id=%s", (done_old,)) is None
    assert _db.query_one("SELECT 1 FROM jobs WHERE id=%s", (done_new,)) is not None
    assert _db.query_one("SELECT 1 FROM jobs WHERE kind='cleanup-test'"
                         " AND status='pending'") is not None


# ─────────────────────── maybe_enqueue_daily：24h 内不重复 ───────────────────────

def test_enqueue_is_skipped_within_twenty_four_hours(db):
    _db.execute("DELETE FROM jobs WHERE kind='cleanup'")
    _db.execute(
        "INSERT INTO jobs (kind, payload, status, finished_at)"
        " VALUES ('cleanup','{}','done', now() - interval '2 hours')")
    with _db.transaction() as tx:
        assert cleanup_mod.maybe_enqueue_daily(tx) is None
    assert _db.query_one("SELECT 1 FROM jobs WHERE kind='cleanup'"
                         " AND payload::text='{}' AND status='pending'") is None


def test_enqueue_runs_after_twenty_four_hours_and_dedups_by_utc_day(db):
    _db.execute("DELETE FROM jobs WHERE kind='cleanup'")
    _db.execute(
        "INSERT INTO jobs (kind, payload, status, finished_at)"
        " VALUES ('cleanup','{}','done', now() - interval '25 hours')")
    with _db.transaction() as tx:
        job_id = cleanup_mod.maybe_enqueue_daily(tx)
    assert job_id is not None
    # 同一天重复调用：dedup_key 挡住，不再排第二个
    with _db.transaction() as tx:
        assert cleanup_mod.maybe_enqueue_daily(tx) is None
    rows = _db.query("SELECT id FROM jobs WHERE kind='cleanup' AND status='pending'")
    assert len(rows) == 1
