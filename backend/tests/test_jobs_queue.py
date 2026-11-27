"""P0 acceptance: a job can be claimed, retried, and its expired lease reclaimed (plan §13).

Gated on MEM_DSN pointing at a migrated database.
"""

import pytest
from conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def clean_jobs():
    from app.core import db

    db.init_pool()
    db.execute("DELETE FROM jobs")
    yield db
    db.execute("DELETE FROM jobs")


def test_enqueue_and_claim(clean_jobs):
    from app.queue import jobs

    job_id = jobs.enqueue("route", {"session_id": "s1"}, org_key="dida",
                          dedup_key="route:s1")
    assert job_id

    claimed = jobs.claim(lease_seconds=60)
    assert claimed["id"] == job_id
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["lease_until"] is not None

    # nothing left to claim
    assert jobs.claim() is None


def test_dedup_key_blocks_a_second_live_job(clean_jobs):
    from app.queue import jobs

    first = jobs.enqueue("route", {"n": 1}, dedup_key="same-key")
    second = jobs.enqueue("route", {"n": 2}, dedup_key="same-key")
    assert first is not None
    assert second is None

    jobs.complete(first)
    third = jobs.enqueue("route", {"n": 3}, dedup_key="same-key")
    assert third is not None, "a finished job must not keep blocking new ones"


def test_fail_requeues_until_the_budget_is_exhausted(clean_jobs):
    """A8：失败后进入指数退避（next_retry_at），退避期内认领不到；
    把 next_retry_at 拨回过去模拟时间流逝后可再认领；预算耗尽落 failed。"""
    from app.queue import jobs

    job_id = jobs.enqueue("route", {}, dedup_key="retry-me", max_attempts=2)

    jobs.claim()
    assert jobs.fail(job_id, "boom 1") == "pending"

    # ★ A8：第一次失败 → 60s 退避，立即 claim 拿不到
    row = clean_jobs.query_one("SELECT next_retry_at FROM jobs WHERE id=%s", (job_id,))
    assert row["next_retry_at"] is not None
    assert jobs.claim() is None

    clean_jobs.execute("UPDATE jobs SET next_retry_at = now() - interval '1s' WHERE id=%s",
                       (job_id,))
    jobs.claim()
    assert jobs.fail(job_id, "boom 2") == "failed"

    row = clean_jobs.query_one(
        "SELECT status, attempts, last_error, next_retry_at FROM jobs WHERE id=%s",
        (job_id,))
    assert row["status"] == "failed"
    assert row["attempts"] == 2
    assert row["last_error"] == "boom 2"
    assert row["next_retry_at"] is None, "终态不再需要退避时间"

    # a failed job is not reclaimed
    assert jobs.claim() is None


def test_retry_backoff_doubles_and_is_capped_at_one_hour(clean_jobs):
    """A8：60s×2^n 封顶 1h——60 → 120 → 240（jobs.max_attempts=5 时最多 4 次重试）。"""
    from datetime import timedelta

    from app.queue import jobs

    job_id = jobs.enqueue("route", {}, dedup_key="backoff", max_attempts=5)

    delays = []
    for expected in (60, 120, 240):
        clean_jobs.execute("UPDATE jobs SET next_retry_at = now() - interval '1s' WHERE id=%s",
                           (job_id,))
        jobs.claim()
        before = clean_jobs.query_one("SELECT now() n")["n"]
        assert jobs.fail(job_id, f"boom {expected}") == "pending"
        row = clean_jobs.query_one(
            "SELECT next_retry_at FROM jobs WHERE id=%s", (job_id,))
        delta = row["next_retry_at"] - before
        delays.append(delta)
        lo = timedelta(seconds=expected - 5)
        hi = timedelta(seconds=expected + 5)
        assert lo <= delta <= hi, f"attempt {expected}s: got {delta}"

    assert jobs.stats().get("pending", 0) == 1


def test_job_in_backoff_is_not_claimable(clean_jobs):
    """A8：退避期内的 pending job 必须被认领 SQL 挡住（否则退避形同虚设）。"""
    from app.queue import jobs

    job_id = jobs.enqueue("route", {}, dedup_key="cooling")
    jobs.claim()
    jobs.fail(job_id, "boom")

    assert jobs.claim() is None, "退避期内不应认领"
    clean_jobs.execute("UPDATE jobs SET next_retry_at = now() - interval '1s' WHERE id=%s",
                       (job_id,))
    reclaimed = jobs.claim()
    assert reclaimed is not None and reclaimed["id"] == job_id
    row = clean_jobs.query_one("SELECT next_retry_at FROM jobs WHERE id=%s", (job_id,))
    assert row["next_retry_at"] is None, "认领时清空退避时间"


def test_expired_lease_is_reclaimed_by_another_worker(clean_jobs):
    from app.queue import jobs

    job_id = jobs.enqueue("route", {}, dedup_key="zombie")
    jobs.claim(lease_seconds=-1)  # already expired

    reclaimed = jobs.claim(lease_seconds=60)
    assert reclaimed["id"] == job_id
    assert reclaimed["attempts"] == 2


def test_a_stale_worker_cannot_complete_the_new_owners_job(clean_jobs):
    """★ EXPERT-REVIEW A4：租约过期被回收后，卡死的旧 worker 醒来再 complete，
    会把别人**正在跑**的 job 标成 done——`started_at` 所有权令牌必须挡住它。"""
    import time as _time

    from app.queue import jobs

    job_id = jobs.enqueue("route", {}, dedup_key="stale-complete")
    stale = jobs.claim(lease_seconds=-1)      # 立即过期
    _time.sleep(0.01)                          # 确保两次 claim 的 now() 不同
    owner = jobs.claim(lease_seconds=60)       # 被"新 worker"回收
    assert owner["id"] == job_id

    assert jobs.complete(job_id, started_at=stale["started_at"]) is False
    assert clean_jobs.query_one("SELECT status FROM jobs WHERE id=%s",
                                (job_id,))["status"] == "running"

    assert jobs.complete(job_id, started_at=owner["started_at"]) is True
    assert clean_jobs.query_one("SELECT status FROM jobs WHERE id=%s",
                                (job_id,))["status"] == "done"


def test_a_stale_worker_fail_reports_stolen_and_touches_nothing(clean_jobs):
    import time as _time

    from app.queue import jobs

    job_id = jobs.enqueue("route", {}, dedup_key="stale-fail", max_attempts=3)
    stale = jobs.claim(lease_seconds=-1)
    _time.sleep(0.01)
    jobs.claim(lease_seconds=60)

    assert jobs.fail(job_id, "boom from zombie", started_at=stale["started_at"]) == "stolen"
    row = clean_jobs.query_one("SELECT status, last_error FROM jobs WHERE id=%s", (job_id,))
    assert row["status"] == "running"          # 新 owner 的执行不受影响
    assert not row["last_error"]               # 旧 worker 的错误也没写进去


def test_a_stale_worker_cannot_renew_the_new_owners_lease(clean_jobs):
    """被回收后旧 worker 若无条件续租，会把新 worker 手里正在跑的 job 租约
    越续越长，僵尸回收机制对它彻底失效。"""
    import time as _time

    from app.queue import jobs

    job_id = jobs.enqueue("route", {}, dedup_key="stale-renew")
    stale = jobs.claim(lease_seconds=-1)
    _time.sleep(0.01)
    jobs.claim(lease_seconds=5)
    before = clean_jobs.query_one("SELECT lease_until FROM jobs WHERE id=%s",
                                  (job_id,))["lease_until"]

    jobs.renew_lease(job_id, lease_seconds=300, started_at=stale["started_at"])
    after = clean_jobs.query_one("SELECT lease_until FROM jobs WHERE id=%s",
                                 (job_id,))["lease_until"]
    assert after == before


def test_complete_clears_the_lease(clean_jobs):
    from app.queue import jobs

    job_id = jobs.enqueue("route", {}, dedup_key="done-key")
    jobs.claim()
    jobs.complete(job_id)
    row = clean_jobs.query_one("SELECT status, lease_until FROM jobs WHERE id=%s", (job_id,))
    assert row["status"] == "done"
    assert row["lease_until"] is None


def test_last_error_is_redacted(clean_jobs):
    from app.core.redact import register_secrets
    from app.queue import jobs

    register_secrets(["leaked-token-value"])
    job_id = jobs.enqueue("route", {}, dedup_key="redact-me", max_attempts=1)
    jobs.claim()
    jobs.fail(job_id, "HTTPError: Authorization: Bearer leaked-token-value")
    row = clean_jobs.query_one("SELECT last_error FROM jobs WHERE id=%s", (job_id,))
    assert "leaked-token-value" not in row["last_error"]


def test_unknown_kind_fails_rather_than_being_skipped(clean_jobs):
    from app.queue import jobs

    jobs.enqueue("nonexistent_kind", {}, dedup_key="unknown")
    jobs.run_worker({}, idle_sleep=0, should_stop=lambda: jobs.stats().get("pending", 0) == 0)
    row = clean_jobs.query_one(
        "SELECT status, last_error FROM jobs WHERE kind='nonexistent_kind'")
    assert row["status"] == "failed"
    assert "no handler" in row["last_error"]
