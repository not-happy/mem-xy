"""§5.7 人工裁决状态机 + §4.3.2 两次比对的验收。

Gated on MEM_DSN。★ **不加载 bge-m3**：embedding 打桩成"哈希词袋"，
确定性、毫秒级——这里要验的是状态机走对了、比对挡对了，不是向量质量。
"""

import hashlib
import json

import pytest
from conftest import requires_db

from app.pipeline import review as review_mod

pytestmark = requires_db

ORG = "dida"
PROJECT = "dida/gds-agg"
SOURCE_MSG = 900000002
DIM = 1024
# ★ 专属版本号：override_stats / queue_stats 是全库聚合，
#   库里已经有真跑留下的行，测试必须能把自己那部分挑出来。
PROMPT_VER = "pytest-review-v1"


class FakeEmbedder:
    """词袋哈希：字面重叠越多余弦越高，完全不相干则接近 0。"""

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
    monkeypatch.setattr(review_mod, "get_embedding_service", lambda: FakeEmbedder())


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    # raw_observations.source_msg 有外键，先造一行真实锚点；固定 id 好让常量可用，
    # 顺手把序列推过去，免得后面真的摄取撞上它。
    _db.execute(
        "INSERT INTO conversation_log (id, source, src_uuid, session_id, org_key, project_key,"
        "                             role, content)"
        " VALUES (%s,'pytest','pytest-review-anchor','pytest-review',%s,%s,'user','锚点')"
        " ON CONFLICT (id) DO NOTHING", (SOURCE_MSG, ORG, PROJECT))
    _db.execute("SELECT setval(pg_get_serial_sequence('conversation_log','id'),"
                " GREATEST((SELECT max(id) FROM conversation_log), 1))")
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM pending_items WHERE source_msg=%s OR raw_content LIKE %s",
                (SOURCE_MSG, "pytest-review-%"))
    _db.execute("DELETE FROM raw_observations WHERE source_msg=%s OR content LIKE %s",
                (SOURCE_MSG, "pytest-review-%"))
    _db.execute("DELETE FROM living_doc WHERE content LIKE %s", ("pytest-review-%",))
    _db.execute("DELETE FROM route_decisions WHERE source_msg=%s OR raw_snippet LIKE %s",
                (SOURCE_MSG, "pytest-review-%"))
    _db.execute("DELETE FROM jobs WHERE dedup_key LIKE 'pending:%'"
                " OR dedup_key LIKE 'write_facts:pending:%'")
    _db.execute("DELETE FROM conversation_log WHERE source='pytest'")


def section_key(db, org=ORG) -> str:
    """从真实词表里取一个栏位——写死 key 会在词表调整（§14）时假失败。"""
    row = db.query_one("SELECT section_key FROM doc_sections WHERE org_key=%s"
                       " OR org_key IS NULL ORDER BY sort LIMIT 1", (org,))
    return row["section_key"]


def seed_pending(db, *, content="pytest-review-枚举名必须全大写", section=None,
                 model_kind="observation", stage="route", status="pending",
                 reason="observation_needs_review", decision_id=None, **extra) -> int:
    """造一张待办卡片，形状与 `router._queue_for_review` 落下来的完全一致。"""
    suggestion = {"kind": "observation", "model_kind": model_kind, "content": content,
                  "confidence": 0.93, "section_key": section,
                  "decision_id": decision_id, **extra}
    return db.query_one(
        "INSERT INTO pending_items (org_key, project_key, stage, raw_content, source_msg,"
        "                            suggestion, reason, prompt_ver, status)"
        " VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s) RETURNING id",
        (ORG, PROJECT, stage, content, SOURCE_MSG,
         json.dumps(suggestion, ensure_ascii=False), reason, PROMPT_VER, status))["id"]


def pending(db, pending_id) -> dict:
    return db.query_one("SELECT * FROM pending_items WHERE id=%s", (pending_id,))


def observations(db) -> list[dict]:
    return db.query("SELECT * FROM raw_observations WHERE source_msg=%s ORDER BY id",
                    (SOURCE_MSG,))


def run_job(db, pending_id, resolution=None) -> dict:
    return review_mod.apply_decision({"pending_id": pending_id,
                                      "resolution": resolution or {}})


# ── 批准 / 否决 ──

def test_only_one_of_two_concurrent_approvals_wins(db):
    """★ 抢占式批准：两个 UI 标签页同时点"通过"，只能排出一个 job。"""
    pid = seed_pending(db)
    assert review_mod.approve(pid) is not None
    assert review_mod.approve(pid) is None
    assert pending(db, pid)["status"] == "approved"


def test_approval_queues_the_job_that_actually_writes(db):
    pid = seed_pending(db)
    job_id = review_mod.approve(pid)
    job = db.query_one("SELECT * FROM jobs WHERE id=%s", (job_id,))
    assert job["kind"] == "apply_decision"
    assert job["payload"]["pending_id"] == pid
    assert job["dedup_key"] == f"pending:{pid}"


def test_an_approved_observation_lands_in_the_observation_pool(db):
    section = section_key(db)
    pid = seed_pending(db, section=section)
    review_mod.approve(pid)

    outcome = run_job(db, pid)

    assert outcome["action"] == "recorded"
    assert pending(db, pid)["status"] == "done"
    row = observations(db)[0]
    assert (row["section_key"], row["polarity"], row["org_key"]) == (section, "positive", ORG)
    assert row["embedding"] is not None          # §5.6 汇总器要靠它做聚类
    assert row["content_hash"]


def test_a_negative_polarity_from_the_model_cannot_poison_the_veto_pool(db):
    """★ 2026-10-23 真跑踩到的：`polarity='negative'` 是**人工否决记录**的专用记号
    （比对② 就查它）。路由出来的线索哪怕是"系统不支持婴儿票"这种否定句，
    也必须落成 positive——否则它会反过来把以后所有同类线索挡成"你以前否决过"。"""
    section = section_key(db)
    pid = seed_pending(db, section=section, polarity="negative",
                       content="pytest-review-系统当前不支持婴儿票")
    review_mod.approve(pid)

    assert run_job(db, pid)["action"] == "recorded"
    assert observations(db)[0]["polarity"] == "positive"


def test_replaying_a_finished_decision_changes_nothing(db):
    """★ job 重放（lease 超时被另一个 worker 抢走）不能落第二行。"""
    pid = seed_pending(db, section=section_key(db))
    review_mod.approve(pid)
    assert run_job(db, pid)["action"] == "recorded"
    assert run_job(db, pid)["action"] == "skipped"
    assert len(observations(db)) == 1


def test_reject_writes_nothing_and_leaves_no_job(db):
    """★ 第一道审核的否决**不**写 `raw_observations(negative)`——那是否决记录，
    用来记"这条成文规范被否了"（§5.7 doc_proposals）。分类判错写进去只会污染它。"""
    pid = seed_pending(db, section=section_key(db))
    assert review_mod.reject(pid, note="这不是规矩，是一次性调试") is True
    assert review_mod.reject(pid) is False        # 已被处理过
    assert pending(db, pid)["status"] == "rejected"
    assert observations(db) == []
    assert db.query("SELECT * FROM jobs WHERE dedup_key=%s", (f"pending:{pid}",)) == []


def test_a_write_failure_is_recorded_as_failed_and_still_raises(db):
    """★ `failed` 必须真的落盘：状态标记和写入共用事务的话，抛错会把它一起回滚，
    于是这条既没写成、也看不出失败过——就是 §5.7 说的"永久静默丢失"。"""
    pid = seed_pending(db, section=section_key(db))
    review_mod.approve(pid)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    original = review_mod._write_observation
    review_mod._write_observation = boom
    try:
        with pytest.raises(RuntimeError):
            run_job(db, pid)
    finally:
        review_mod._write_observation = original

    row = pending(db, pid)
    assert row["status"] == "failed" and "boom" in row["last_error"]
    # failed 是可重入状态：修好之后重跑同一个 job 就能补上
    assert run_job(db, pid)["action"] == "recorded"
    assert pending(db, pid)["status"] == "done"


def test_the_error_message_is_redacted_before_it_reaches_the_ui(db):
    from app.core.redact import register_secrets

    register_secrets(["pytest-review-secret-token"])
    pid = seed_pending(db, section=section_key(db))
    review_mod.approve(pid)

    def boom(*_a, **_k):
        raise RuntimeError("HTTPError: Authorization: Bearer pytest-review-secret-token")

    original = review_mod._write_observation
    review_mod._write_observation = boom
    try:
        with pytest.raises(RuntimeError):
            run_job(db, pid)
    finally:
        review_mod._write_observation = original
    assert "pytest-review-secret-token" not in pending(db, pid)["last_error"]


# ── §4.3.2 的两次比对 ──

def test_comparison_one_bounces_when_living_doc_already_says_it(db):
    """★ P2 验收项：手工插一条 living_doc 后，同类 observation **不再入库**。

    注意它不是被丢弃，而是退回人工——0.72 只是预筛（余弦分不开"重复"和
    "补了细节的超集"），自动丢弃是不可逆的。
    """
    section = section_key(db)
    content = "pytest-review-供应商对接必须先验价再出票"
    db.execute("INSERT INTO living_doc (org_key, project_key, section_key, content, authority,"
               "                        origin) VALUES (%s,%s,%s,%s,'confirmed','human')",
               (ORG, PROJECT, section, content))

    pid = seed_pending(db, content=content, section=section)
    review_mod.approve(pid)
    outcome = run_job(db, pid)

    assert outcome["action"] == "bounced"
    assert outcome["reason"] == "already_in_living_doc"
    assert observations(db) == []
    row = pending(db, pid)
    assert row["status"] == "pending" and row["reason"] == "already_in_living_doc"
    assert row["resolution"]["score"] >= 0.72


def test_comparison_two_bounces_when_the_same_rule_was_rejected_before(db):
    section = section_key(db)
    content = "pytest-review-所有时间戳统一存 UTC"
    db.execute("INSERT INTO raw_observations (org_key, project_key, section_key, content,"
               "                              content_hash, polarity, embedding, source_msg)"
               " VALUES (%s,%s,%s,%s,%s,'negative',%s,%s)",
               (ORG, PROJECT, section, content, "pytest-review-negative",
                _hash_vector(content), SOURCE_MSG))

    pid = seed_pending(db, content=content, section=section)
    review_mod.approve(pid)
    outcome = run_job(db, pid)

    assert outcome["reason"] == "rejected_before"
    # 只有那条 negative 种子，没有新的 positive
    assert [r["polarity"] for r in observations(db)] == ["negative"]
    assert pending(db, pid)["status"] == "pending"


def test_unrelated_material_is_not_blocked_by_either_comparison(db):
    """★ 反向保护：预筛不能变成"什么都挡"。0.33–0.46 是实测的不相干区间。"""
    section = section_key(db)
    db.execute("INSERT INTO living_doc (org_key, project_key, section_key, content, authority,"
               "                        origin) VALUES (%s,%s,%s,%s,'confirmed','human')",
               (ORG, PROJECT, section, "pytest-review-完全不相干的另一件事：前端用 Vite"))
    pid = seed_pending(db, content="pytest-review-枚举名必须全大写 且用下划线分隔",
                       section=section)
    review_mod.approve(pid)
    assert run_job(db, pid)["action"] == "recorded"


def test_a_missing_section_bounces_instead_of_writing_into_the_wrong_partition(db):
    """栏位没定下来就落库，两次比对会在错误分区里做（#50 治的就是栏位飘移）。"""
    pid = seed_pending(db, stage="section", section=None,
                       reason="below_threshold:0.85+section_unmatched")
    review_mod.approve(pid)
    outcome = run_job(db, pid)
    assert outcome == {"action": "bounced", "reason": "section_unmatched"}
    assert observations(db) == []
    assert pending(db, pid)["status"] == "pending"


def test_a_card_with_no_content_bounces_rather_than_writing_an_empty_row(db):
    """这种卡片多半来自 llm_parse_failed（§2.2 素材不许丢），人还没把内容补上。"""
    pid = seed_pending(db, section=section_key(db))
    db.execute("UPDATE pending_items SET suggestion=jsonb_set(suggestion,'{content}','\"\"')"
               " WHERE id=%s", (pid,))
    review_mod.approve(pid)
    assert run_job(db, pid) == {"action": "bounced", "reason": "no_content"}
    assert observations(db) == []


# ── 人把预选的 observation 改回 atomic_fact ──

def test_a_flip_back_to_atomic_fact_is_handed_to_the_four_action_writer(db):
    """★ §5.4「一眼就能改回」：改回之后走写入器（能 UPDATE/NOOP 掉重复），
    而不是自己往 memories 里插一行——那就绕过了 §5.5 的整套幂等。"""
    pid = seed_pending(db, model_kind="atomic_fact",
                       content="pytest-review-gds-agg 的手机号要去掉 86- 前缀",
                       scope="project", subject="project", role="fact",
                       keywords=["gds-agg", "86-"])
    review_mod.approve(pid, {"kind": "atomic_fact"})

    outcome = run_job(db, pid, {"kind": "atomic_fact"})

    assert outcome["action"] == "handed_to_write_facts"
    assert observations(db) == []
    job = db.query_one("SELECT * FROM jobs WHERE dedup_key=%s",
                       (f"write_facts:pending:{pid}",))
    assert job["kind"] == "write_facts"
    assert job["payload"]["org_key"] == ORG and job["payload"]["project_key"] == PROJECT
    item = job["payload"]["items"][0]
    assert item["source_msg"] == SOURCE_MSG
    assert item["content"].startswith("pytest-review-gds-agg")
    assert item["keywords"] == ["gds-agg", "86-"]


def test_a_human_discard_writes_nothing_and_closes_the_card(db):
    pid = seed_pending(db, model_kind="atomic_fact")
    review_mod.approve(pid, {"kind": "discard"})
    assert run_job(db, pid, {"kind": "discard"})["action"] == "discarded_by_human"
    assert pending(db, pid)["status"] == "done"
    assert observations(db) == []


# ── §2.3 改判率 ──

def test_adjudication_is_written_back_to_route_decisions(db):
    """★ 少写这一行，改判率就永远算不出来，改进 few-shot 也没有素材。"""
    decision_id = db.query_one(
        "INSERT INTO route_decisions (source_msg, raw_snippet, prompt_ver, kind, confidence,"
        "                              model_output, org_key)"
        " VALUES (%s,'pytest-review-原始素材',%s,'atomic_fact',0.91,'{}'::jsonb,%s)"
        " RETURNING id", (SOURCE_MSG, PROMPT_VER, ORG))["id"]
    pid = seed_pending(db, model_kind="atomic_fact", decision_id=decision_id)

    review_mod.approve(pid)          # 人没改 kind，沿用卡片预选的 observation
    row = db.query_one("SELECT * FROM route_decisions WHERE id=%s", (decision_id,))
    assert row["human_output"] is not None
    assert row["overridden"] is True, "atomic_fact → observation 是一次改判"


def test_keeping_the_model_kind_is_not_counted_as_an_override(db):
    decision_id = db.query_one(
        "INSERT INTO route_decisions (source_msg, raw_snippet, prompt_ver, kind, confidence,"
        "                              model_output, org_key)"
        " VALUES (%s,'pytest-review-原始素材',%s,'observation',0.99,'{}'::jsonb,%s)"
        " RETURNING id", (SOURCE_MSG, PROMPT_VER, ORG))["id"]
    pid = seed_pending(db, model_kind="observation", decision_id=decision_id)
    review_mod.approve(pid)
    assert db.query_one("SELECT overridden FROM route_decisions WHERE id=%s",
                        (decision_id,))["overridden"] is False


def test_a_rejection_counts_as_an_override_of_the_model_kind(db):
    decision_id = db.query_one(
        "INSERT INTO route_decisions (source_msg, raw_snippet, prompt_ver, kind, confidence,"
        "                              model_output, org_key)"
        " VALUES (%s,'pytest-review-原始素材',%s,'observation',0.97,'{}'::jsonb,%s)"
        " RETURNING id", (SOURCE_MSG, PROMPT_VER, ORG))["id"]
    pid = seed_pending(db, model_kind="observation", decision_id=decision_id)
    review_mod.reject(pid, note="只是闲聊")
    assert db.query_one("SELECT overridden FROM route_decisions WHERE id=%s",
                        (decision_id,))["overridden"] is True


def test_override_stats_are_broken_down_by_kind(db):
    """★ P2 验收项：改判率**必须按 kind 分开**。合在一起会被数量最多的那类带着走，
    而最危险的恰恰是"高置信却判错"，所以还单独给了 `avg_conf_when_wrong`。"""
    review_mod.approve(seed_pending(db, model_kind="observation", decision_id=_decision(
        db, kind="observation", confidence=0.99)))
    review_mod.reject(seed_pending(db, model_kind="observation", decision_id=_decision(
        db, kind="observation", confidence=0.97)))
    review_mod.approve(seed_pending(db, model_kind="atomic_fact", decision_id=_decision(
        db, kind="atomic_fact", confidence=0.60)))

    rows = {r["kind"]: r for r in review_mod.override_stats()
            if r["prompt_ver"] == PROMPT_VER}   # ★ 全库聚合，只挑自己这批
    assert rows["observation"]["judged"] == 2
    assert rows["observation"]["reviewed"] == 2
    assert rows["observation"]["overridden"] == 1
    assert float(rows["observation"]["avg_conf_when_wrong"]) == pytest.approx(0.97)
    assert rows["atomic_fact"]["overridden"] == 1     # 预选 observation ≠ 模型判的 atomic_fact
    assert float(rows["atomic_fact"]["avg_confidence"]) == pytest.approx(0.60)


def _decision(db, *, kind, confidence) -> int:
    return db.query_one(
        "INSERT INTO route_decisions (source_msg, raw_snippet, prompt_ver, kind, confidence,"
        "                              model_output, org_key)"
        " VALUES (%s,'pytest-review-原始素材',%s,%s,%s,'{}'::jsonb,%s) RETURNING id",
        (SOURCE_MSG, PROMPT_VER, kind, confidence, ORG))["id"]


def test_queue_stats_are_grouped_by_status_and_stage(db):
    """★ 用**增量**断言：queue_stats 按 status/stage 全库聚合、没有 prompt_ver 可过滤，
    而库里已经有真跑留下的卡片。断言绝对值等于要求全库为空。"""
    before = review_mod.queue_stats()
    seed_pending(db, stage="route")
    seed_pending(db, stage="section", section=None, content="pytest-review-另一条没栏位的",
                 reason="section_unmatched")
    review_mod.approve(seed_pending(db, content="pytest-review-已批准的"))

    after = review_mod.queue_stats()
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in set(after) | set(before)}
    assert delta["pending/route"] == 1
    assert delta["pending/section"] == 1
    assert delta["approved/route"] == 1
