"""§5.7 提案裁决 + §4.3.2 否决写回的验收。

Gated on MEM_DSN。★ **不加载 bge-m3**：embedding 打桩成"哈希词袋"，确定性、毫秒级。
这里要验的是：采纳把 living_doc 落成 confirmed + 写 history、否决写进否决记录、
抢占式翻状态只赢一次、job 重放不叠第二遍——不是向量质量。
"""

import hashlib

import pytest
from conftest import requires_db

from app.core.hashing import content_hash
from app.pipeline import proposals as prop_mod

pytestmark = requires_db

ORG = "dida"
PROJECT = "dida/gds-agg"
SECTION = "dependency"
MARK = "pytest-prop-"
DIM = 1024


class FakeEmbedder:
    def encode(self, texts, *a, **k):
        return [_hash_vector(t) for t in texts]

    def encode_one(self, text):
        return _hash_vector(text)


def _hash_vector(text: str) -> list[float]:
    vector = [0.0] * DIM
    for token in text.replace("，", " ").replace("、", " ").split():
        slot = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM
        vector[slot] += 1.0
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    monkeypatch.setattr(prop_mod, "get_embedding_service", lambda: FakeEmbedder())


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM living_doc_history WHERE living_doc_id IN"
                " (SELECT id FROM living_doc WHERE project_key=%s OR content LIKE %s)",
                (PROJECT, MARK + "%"))
    _db.execute("DELETE FROM living_doc WHERE project_key=%s OR content LIKE %s",
                (PROJECT, MARK + "%"))
    _db.execute("DELETE FROM doc_proposals WHERE project_key=%s OR new_content LIKE %s"
                " OR old_content LIKE %s", (PROJECT, MARK + "%", MARK + "%"))
    _db.execute("DELETE FROM raw_observations WHERE project_key=%s OR content LIKE %s",
                (PROJECT, MARK + "%"))
    _db.execute("DELETE FROM jobs WHERE dedup_key LIKE 'proposal:%'")


def seed_proposal(db, *, new_content=f"{MARK}禁止引入 Lombok", old_content=None,
                  status="pending", section=SECTION, project=PROJECT, org=ORG,
                  source_ids=()) -> int:
    return db.query_one(
        "INSERT INTO doc_proposals (org_key, project_key, section_key, old_content,"
        "                            new_content, status, source_ids)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (org, project, section, old_content, new_content, status, list(source_ids)))["id"]


def seed_doc(db, content, *, authority="draft", section=SECTION, project=PROJECT, org=ORG) -> int:
    return db.query_one(
        "INSERT INTO living_doc (org_key, project_key, section_key, content, authority)"
        " VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (org, project, section, content, authority))["id"]


def proposal(db, pid) -> dict:
    return db.query_one("SELECT * FROM doc_proposals WHERE id=%s", (pid,))


def living_doc(db, section=SECTION, project=PROJECT):
    return db.query_one("SELECT * FROM living_doc WHERE org_key=%s AND section_key=%s"
                        " AND project_key IS NOT DISTINCT FROM %s", (ORG, section, project))


def history(db, living_doc_id):
    return db.query("SELECT * FROM living_doc_history WHERE living_doc_id=%s ORDER BY version",
                    (living_doc_id,))


def negatives(db):
    return db.query("SELECT * FROM raw_observations WHERE polarity='negative'"
                    " AND content LIKE %s ORDER BY id", (MARK + "%",))


def run_job(db, pid, **extra):
    return prop_mod.apply_proposal({"proposal_id": pid, **extra})


# ── 抢占式翻状态 ──────────────────────────────────────────────────────────

def test_only_one_of_two_concurrent_accepts_wins(db):
    pid = seed_proposal(db)
    assert prop_mod.accept(pid) is not None
    assert prop_mod.accept(pid) is None          # 第二次抢不到
    assert proposal(db, pid)["status"] == "accepted"


def test_accept_after_reject_is_refused(db):
    pid = seed_proposal(db)
    assert prop_mod.reject(pid) is not None
    assert prop_mod.accept(pid) is None          # 已 rejected，accept 抢不到
    assert proposal(db, pid)["status"] == "rejected"


def test_accept_queues_the_job_that_actually_writes(db):
    pid = seed_proposal(db)
    job_id = prop_mod.accept(pid)
    job = db.query_one("SELECT * FROM jobs WHERE id=%s", (job_id,))
    assert job["kind"] == "apply_proposal"
    assert job["payload"]["proposal_id"] == pid
    assert job["dedup_key"] == f"proposal:{pid}"
    assert proposal(db, pid)["job_id"] == job_id   # 回写到提案上


# ── 采纳：新 section → 落 confirmed + history ─────────────────────────────

def test_accepting_a_new_section_inserts_a_confirmed_living_doc(db):
    src = [11, 22, 33]
    pid = seed_proposal(db, source_ids=src)
    prop_mod.accept(pid)
    out = run_job(db, pid, content=f"{MARK}禁止引入 Lombok", changed_by="summarizer")

    assert out["action"] == "applied" and out["version"] == 1
    doc = living_doc(db)
    assert doc is not None
    assert doc["authority"] == "confirmed"       # ★ 采纳即生效，不停在 draft
    assert doc["origin"] == "summarized"
    assert doc["content"] == f"{MARK}禁止引入 Lombok"
    assert sorted(doc["source_ids"]) == src       # 溯源链跟着落地
    hist = history(db, doc["id"])
    assert len(hist) == 1
    assert hist[0]["changed_by"] == "summarizer"
    assert hist[0]["old_content"] is None and hist[0]["new_authority"] == "confirmed"
    assert hist[0]["proposal_id"] == pid


def test_human_edited_accept_is_attributed_to_human(db):
    """手改后采纳（§7.1）：changed_by='human'，落的是改过的文本。"""
    pid = seed_proposal(db, new_content=f"{MARK}AI 合成的原文")
    prop_mod.accept(pid, content=f"{MARK}人改过的最终版")
    out = run_job(db, pid, content=f"{MARK}人改过的最终版", changed_by="human")
    doc = living_doc(db)
    assert doc["content"] == f"{MARK}人改过的最终版"
    assert history(db, doc["id"])[0]["changed_by"] == "human"
    assert out["changed_by"] == "human"


# ── 采纳：已有 draft 行 → 升 confirmed + version+1 ────────────────────────

def test_accepting_over_an_existing_draft_bumps_version_and_keeps_old(db):
    doc_id = seed_doc(db, f"{MARK}旧草稿", authority="draft")
    pid = seed_proposal(db, old_content=f"{MARK}旧草稿",
                        new_content=f"{MARK}旧草稿；补充一条")
    prop_mod.accept(pid)
    out = run_job(db, pid, content=f"{MARK}旧草稿；补充一条", changed_by="summarizer")

    assert out["action"] == "applied" and out["version"] == 2
    doc = living_doc(db)
    assert doc["id"] == doc_id                    # 同一行被升档，不是新插一行
    assert doc["authority"] == "confirmed" and doc["version"] == 2
    hist = history(db, doc_id)
    assert len(hist) == 1
    assert hist[0]["old_content"] == f"{MARK}旧草稿"
    assert hist[0]["old_authority"] == "draft" and hist[0]["new_authority"] == "confirmed"


# ── job 重放幂等 ─────────────────────────────────────────────────────────

def test_replaying_an_accepted_job_does_not_double_bump(db):
    """★ job 可能在"写完但没来得及 complete"后被其它 worker 回收重放。
    history 里已有这个 proposal_id 就必须认出来，别再 version+1、别再叠 history。"""
    pid = seed_proposal(db)
    prop_mod.accept(pid)
    run_job(db, pid, content=f"{MARK}禁止引入 Lombok", changed_by="summarizer")
    again = run_job(db, pid, content=f"{MARK}禁止引入 Lombok", changed_by="summarizer")
    assert again["action"] == "already_applied"
    doc = living_doc(db)
    assert doc["version"] == 1                     # 没有变成 2
    assert len(history(db, doc["id"])) == 1        # 没有叠第二条 history


# ── 否决：写否决记录 ───────────────────────────────────────────────────────

def test_rejecting_writes_a_negative_observation(db):
    pid = seed_proposal(db, new_content=f"{MARK}系统不支持婴儿票")
    prop_mod.reject(pid)
    out = run_job(db, pid)

    assert out["action"] == "vetoed"
    assert proposal(db, pid)["status"] == "rejected"
    negs = negatives(db)
    assert len(negs) == 1
    row = negs[0]
    assert row["content"] == f"{MARK}系统不支持婴儿票"   # 被否决的规范文本
    assert row["section_key"] == SECTION                 # 同提案的 section（比对② 按它查）
    assert row["org_key"] == ORG and row["project_key"] == PROJECT
    assert row["embedding"] is not None                  # 比对② 靠向量检索
    assert row["content_hash"] == content_hash(row["content"])
    assert row["source_msg"] is None                     # 否决不挂对话锚点


def test_reject_is_idempotent_on_replay(db):
    pid = seed_proposal(db, new_content=f"{MARK}不许用 Lombok")
    prop_mod.reject(pid)
    run_job(db, pid)
    again = run_job(db, pid)
    assert again["action"] == "already_applied"
    assert len(negatives(db)) == 1                       # 没有叠第二条否决


def test_rejecting_an_empty_proposal_is_skipped(db):
    pid = seed_proposal(db, new_content="   ")
    prop_mod.reject(pid)
    out = run_job(db, pid)
    assert out["action"] == "skipped" and out["reason"] == "empty_content"
    assert negatives(db) == []


# ── 否决记录真的能挡住同类线索（比对② 端到端）───────────────────────────────

def test_a_vetoed_rule_blocks_the_same_observation_via_review(db, monkeypatch):
    """★ 这是 §4.3.2 "否决一次，以后不再提" 的闭环钉子：否决写进 negative 池后，
    review._blocked_by 的比对② 要能把同一条素材挡回人工。用 review 的真比对函数验，
    两边共用同一个 FakeEmbedder（词袋哈希），字面相同的两句余弦=1 必然命中。"""
    from app.core import db as _db
    from app.pipeline import review as review_mod

    text = f"{MARK}禁止在 gds-agg 引入 Lombok"
    pid = seed_proposal(db, new_content=text)
    prop_mod.reject(pid)
    run_job(db, pid)

    monkeypatch.setattr(review_mod, "get_embedding_service", lambda: FakeEmbedder())
    with _db.connect() as conn:
        # doc_info=None：本用例只验比对②（negative 池）；living_doc 的比对①
        # 已由 _prepare_vectors 在事务外备好行与向量（EXPERT-REVIEW B1 同款纪律）。
        blocked = review_mod._blocked_by(
            conn, embedding=_hash_vector(text), doc_info=None,
            org_key=ORG, section_key=SECTION)
    assert blocked is not None and blocked["reason"] == "rejected_before"
    assert blocked["matched_content"] == text
