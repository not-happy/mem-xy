"""B4-②：dedup_scan 周期近重复合并扫描的验收。

Gated on MEM_DSN（独立测试库，conftest 统一重建）。不加载 bge-m3：
scan 只做 SQL 近邻比对，向量直接用"哈希词袋"造（同 token → 余弦 1.0）。
"""

import json

import pytest
from conftest import requires_db

from app import dedup_scan as dedup_mod
from app.core.hashing import content_hash

pytestmark = requires_db

ORG = "dida"
PROJECT = "dida/gds-agg"
OTHER_PROJECT = "dida/flight-supplier"
SOURCE_MSG = 900000003
DIM = 1024


def _hash_vector(text: str) -> list[float]:
    import hashlib

    vector = [0.0] * DIM
    for token in text.replace("，", " ").replace("、", " ").split():
        slot = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM
        vector[slot] += 1.0
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    _db.execute(
        "INSERT INTO conversation_log (id, source, src_uuid, session_id, org_key, project_key,"
        "                             role, content)"
        " VALUES (%s,'pytest','pytest-dedup-anchor','pytest-dedup',%s,%s,'user','锚点')"
        " ON CONFLICT (id) DO NOTHING", (SOURCE_MSG, ORG, PROJECT))
    _db.execute("SELECT setval(pg_get_serial_sequence('conversation_log','id'),"
                " GREATEST((SELECT max(id) FROM conversation_log), 1))")
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM memory_events WHERE memory_id IN ("
                " SELECT id FROM memories WHERE source_msg=%s OR content LIKE %s)",
                (SOURCE_MSG, "pytest-dedup-%"))
    _db.execute("DELETE FROM memories WHERE source_msg=%s OR content LIKE %s",
                (SOURCE_MSG, "pytest-dedup-%"))
    _db.execute("DELETE FROM pending_items WHERE source_msg=%s AND stage='dedup'",
                (SOURCE_MSG,))
    _db.execute("DELETE FROM jobs WHERE kind='dedup_scan'")
    _db.execute("DELETE FROM conversation_log WHERE source='pytest'")


def seed_memory(db, content, *, project_key=PROJECT) -> int:
    return db.query_one(
        "INSERT INTO memories (scope, org_key, project_key, subject, role, content,"
        "                      content_hash, keywords, embedding, mention_count, source_msg)"
        " VALUES ('project',%s,%s,'project','fact',%s,%s,%s,%s,1,%s) RETURNING id",
        (ORG, project_key, content, content_hash(content), [],
         _hash_vector(content), SOURCE_MSG))["id"]


def cards(db):
    return db.query("SELECT * FROM pending_items WHERE stage='dedup' AND source_msg=%s"
                    " ORDER BY id", (SOURCE_MSG,))


def run_scan(db) -> dict:
    with db.transaction() as tx:
        return dedup_mod.scan(tx)


# 近重复合素材：共享 5 个 token，第 6 个只多一点——
# 哈希词袋下 cos = 5/(√5·√6) ≈ 0.913 ≥ 0.90（默认阈值）。
TWIN_A = "pytest-dedup-gds-agg 的手机号 要去掉 86- 前缀"
TWIN_B = "pytest-dedup-gds-agg 的手机号 要去掉 86- 前缀 呀"
# 跨分区用例的第二份素材：同 token 集换序（cos=1.0、字符串不同 → 哈希不同，
# 绕开 UNIQUE(source_msg, content_hash)）。
TWIN_A2 = "pytest-dedup-gds-agg 要去掉 86- 前缀 的手机号"
TWIN_B2 = "pytest-dedup-gds-agg 前缀 的手机号 要去掉 86-"


def test_scan_emits_one_card_per_near_duplicate_pair(db):
    id_a = seed_memory(db, TWIN_A)
    id_b = seed_memory(db, TWIN_B)
    seed_memory(db, "pytest-dedup-完全无关 的 内容")   # 孤儿：不该配对

    stats = run_scan(db)

    assert stats["pairs"] == 1 and stats["cards"] == 1
    card = cards(db)[0]
    assert card["dedup_hash"] == f"merge:{min(id_a, id_b)}:{max(id_a, id_b)}"
    assert card["status"] == "pending" and card["reason"] == "near_duplicate"
    suggestion = card["suggestion"]
    assert suggestion["kind"] == "merge_pair"
    assert suggestion["memory_ids"] == [min(id_a, id_b), max(id_a, id_b)]
    assert suggestion["content_a"] in (TWIN_A, TWIN_B)
    assert suggestion["similarity"] == pytest.approx(0.9129, abs=0.01)
    assert json.dumps(suggestion, ensure_ascii=False)   # 可 JSON 化（落库时已验证形状）


def test_rescan_does_not_duplicate_cards(db):
    """部分唯一索引兜底：同一对重扫不重卡（幂等）。"""
    seed_memory(db, TWIN_A)
    seed_memory(db, TWIN_B)

    first = run_scan(db)
    second = run_scan(db)

    assert (first["cards"], second["cards"]) == (1, 0)
    assert second["pairs"] == 0
    assert len(cards(db)) == 1


def test_a_rejected_pair_is_never_resurfaced(db):
    """人否过的对不再骚扰（NOT EXISTS 过滤含 rejected）；failed 的卡允许重试。"""
    id_a = seed_memory(db, TWIN_A)
    id_b = seed_memory(db, TWIN_B)
    run_scan(db)
    db.execute("UPDATE pending_items SET status='rejected' WHERE stage='dedup'"
               " AND source_msg=%s", (SOURCE_MSG,))

    assert run_scan(db) == {"pairs": 0, "cards": 0}

    db.execute("UPDATE pending_items SET status='failed' WHERE stage='dedup'"
               " AND source_msg=%s", (SOURCE_MSG,))
    assert run_scan(db)["cards"] == 1   # failed → 系统失败要能重来


def test_pairs_never_cross_partition(db):
    """同 (scope, org, project) 分区内才比对——跨工程的同文 Twin 不出卡。"""
    seed_memory(db, TWIN_A)
    seed_memory(db, TWIN_B)
    seed_memory(db, TWIN_A2, project_key=OTHER_PROJECT)   # 跨分区孪生
    seed_memory(db, TWIN_B2, project_key=OTHER_PROJECT)

    stats = run_scan(db)

    # 两个分区内各自一对 = 2 张卡；跨分区的 4 对全部不可见
    assert stats["pairs"] == 2 and stats["cards"] == 2


def test_deleted_and_vectorless_rows_are_ignored(db):
    id_a = seed_memory(db, TWIN_A)
    id_b = seed_memory(db, TWIN_B)
    db.execute("UPDATE memories SET status='deleted' WHERE id=%s", (id_b,))
    id_c = seed_memory(db, TWIN_A2)
    db.execute("UPDATE memories SET embedding=NULL WHERE id=%s", (id_c,))

    assert run_scan(db) == {"pairs": 0, "cards": 0}


def test_maybe_enqueue_daily_is_idempotent_per_day(db):
    """24h 节流 + UTC 日期 dedup_key：同一天重复调用不重复排 job。"""
    with db.transaction() as tx:
        dedup_mod.maybe_enqueue_daily(tx)
    with db.transaction() as tx:
        dedup_mod.maybe_enqueue_daily(tx)

    rows = db.query("SELECT * FROM jobs WHERE kind='dedup_scan'")
    assert len(rows) == 1
    assert rows[0]["dedup_key"].startswith("dedup_scan:")
