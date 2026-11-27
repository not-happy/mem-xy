"""★ 双判共识制（decisions-log #116）的测试。

测试策略：不调真 LLM——monkeypatch `app.pipeline.auto_review.get_llm_client`
返回替身 client，`structured()` 按用例脚本返回预设 JudgeItem/MergeJudge。
卡片造数复用 test_review.py 的基建（直接 import 其 helper，口径一致）。
"""

from __future__ import annotations

import dataclasses

import pytest

import app.pipeline.auto_review as auto_review_mod
import app.prompts.auto_review as prompts_auto
from tests import test_review as tr
from tests.test_review import FakeEmbedder

# db fixture 定义在 test_review.py 里（不是 conftest）——跨模块复用必须显式挂载，
# 否则 "fixture 'db' not found"（pytest 只把插件模块里的 fixture 注册进全局表）。
pytest_plugins = ["tests.test_review"]

pytestmark = pytest.mark.requires_db

# --------------------------------------------------------------------------
# 基建：替身 LLM
# --------------------------------------------------------------------------


class FakeLLM:
    """structured() 按脚本逐次弹出结果；脚本空了就抛错（测失败回退路径）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def structured(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise RuntimeError("FakeLLM script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        # ★ 对齐真 LLMClient.structured 的返回契约：schema=JudgeResult 时返回的
        # 是 JudgeResult（.kind 才是 JudgeItem）。脚本里允许直接写 JudgeItem，
        # 这里自动包一层，免得每个用例都套 JudgeResult(kind=...)。
        schema = kwargs.get("schema")
        if schema is prompts_auto.JudgeResult and isinstance(
                item, prompts_auto.JudgeItem):
            return prompts_auto.JudgeResult(kind=item)
        return item


@pytest.fixture
def fake_llm(monkeypatch):
    """返回 install(script)：把 auto_review 的 LLM 出口换成替身。"""
    holder = {"client": None}

    def install(script):
        client = FakeLLM(script)
        holder["client"] = client
        monkeypatch.setattr(auto_review_mod, "get_llm_client", lambda: client)
        return client

    holder["install"] = install
    return holder


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    # apply_decision 落库路径要算向量（observation 写池 / merge 改文案）——
    # 与 test_review 同款：把 embedding 出口换成词袋替身。
    monkeypatch.setattr(tr.review_mod, "get_embedding_service", lambda: FakeEmbedder())


def _off(monkeypatch):
    monkeypatch.setattr(auto_review_mod, "get_settings",
                        lambda: dataclasses.replace(tr_get_settings(),
                                                    auto_review_enabled=False))


def tr_get_settings():
    from app.core.config import get_settings

    return get_settings()


# --------------------------------------------------------------------------
# route 卡：一致 → 自动落库
# --------------------------------------------------------------------------


def test_consensus_observation_is_auto_recorded(db, fake_llm):
    p = tr.seed_pending(db, content="pytest-review-枚举名必须全大写",
                        section=tr.section_key(db), model_kind="observation")
    fake_llm["install"]([prompts_auto.JudgeItem(kind="observation", confidence=0.9)])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "auto_approved"
    row = tr.pending(db, p)
    assert row["status"] == "approved" and row["resolved_by"] == "llm_auto"
    # 落库链路走 review.approve：apply_decision job 已按 llm_auto 排上
    job = db.query_one(
        "SELECT * FROM jobs WHERE kind='apply_decision' AND payload->>'pending_id'=%s",
        (str(p),))
    assert job is not None and job["status"] == "pending"
    assert job["payload"]["resolved_by"] == "llm_auto"
    assert job["payload"]["resolution"] == {"kind": "observation", "resolved_by": "llm_auto"}


def test_consensus_atomic_fact_auto_approves(db, fake_llm):
    p = tr.seed_pending(db, content="pytest-review-备份推 not-happy/memxy-backup",
                        section=tr.section_key(db), model_kind="atomic_fact",
                        scope="project", subject="project", role="fact")
    fake_llm["install"]([prompts_auto.JudgeItem(
        kind="atomic_fact", confidence=0.88, scope="project",
        subject="project", role="fact")])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "auto_approved"
    assert tr.pending(db, p)["resolved_by"] == "llm_auto"


def test_second_opinion_discard_stays_human(db, fake_llm):
    p = tr.seed_pending(db, content="pytest-review-第二遍说这是可推导的",
                        section=tr.section_key(db), model_kind="observation")
    fake_llm["install"]([prompts_auto.JudgeItem(
        kind="discard", confidence=0.7, why="一次性执行细节")])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "left_for_human"
    row = tr.pending(db, p)
    assert row["status"] == "pending"  # 不自动落库也不自动否决
    assert row["suggestion"]["llm_review"]["kind"] == "discard"
    assert row["suggestion"]["llm_review"]["reason"] == "second_opinion_discard"
    assert "llm_disagreed" in row["reason"]
    assert db.query_one("SELECT 1 AS x FROM jobs WHERE kind='apply_decision'"
                        " AND payload->>'pending_id'=%s", (str(p),)) is None


def test_kind_swap_is_disagreement(db, fake_llm):
    p = tr.seed_pending(db, content="pytest-review-第一遍观察第二遍事实",
                        section=tr.section_key(db), model_kind="observation")
    fake_llm["install"]([prompts_auto.JudgeItem(
        kind="atomic_fact", confidence=0.9, scope="project",
        subject="project", role="fact")])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "left_for_human"
    assert tr.pending(db, p)["suggestion"]["llm_review"]["reason"] == "kind_disagreement"


def test_atomic_fact_without_facets_stays_human(db, fake_llm):
    p = tr.seed_pending(db, content="pytest-review-缺 facet 的 atomic_fact",
                        section=tr.section_key(db), model_kind="atomic_fact")
    fake_llm["install"]([prompts_auto.JudgeItem(
        kind="atomic_fact", confidence=0.9)])  # scope/subject/role 缺
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "left_for_human"
    assert tr.pending(db, p)["suggestion"]["llm_review"]["reason"] == "missing_facets"


def test_section_unmatched_filled_by_second_pass(db, fake_llm):
    sec = tr.section_key(db)
    p = tr.seed_pending(db, content="pytest-review-第一遍没匹配上栏位",
                        section=None, model_kind="observation")
    fake_llm["install"]([prompts_auto.JudgeItem(
        kind="observation", confidence=0.9, section_key=sec)])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "auto_approved"
    assert tr.pending(db, p)["status"] == "approved"
    # 栏位补齐进了 resolution → apply job 拿得到 section_key
    job = db.query_one(
        "SELECT payload FROM jobs WHERE kind='apply_decision' AND payload->>'pending_id'=%s",
        (str(p),))
    assert job["payload"]["resolution"]["section_key"] == sec


def test_section_still_unmatched_stays_human(db, fake_llm):
    p = tr.seed_pending(db, content="pytest-review-两遍都给不出栏位",
                        section=None, model_kind="observation")
    fake_llm["install"]([prompts_auto.JudgeItem(
        kind="observation", confidence=0.9)])  # 不给 section_key
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "left_for_human"
    assert tr.pending(db, p)["suggestion"]["llm_review"]["reason"] == "section_still_unmatched"


def test_revised_content_passes_through(db, fake_llm):
    """第二遍给了不同的 content → 作为修订写进 resolution（人最终可见）。"""
    p = tr.seed_pending(db, content="pytest-review-原句不够自包含",
                        section=tr.section_key(db), model_kind="observation")
    fake_llm["install"]([prompts_auto.JudgeItem(
        kind="observation", confidence=0.9,
        content="pytest-review-修订后的自包含句子")])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "auto_approved"
    job = db.query_one(
        "SELECT payload FROM jobs WHERE kind='apply_decision' AND payload->>'pending_id'=%s",
        (str(p),))
    assert job["payload"]["resolution"]["content"] == "pytest-review-修订后的自包含句子"


# --------------------------------------------------------------------------
# merge 卡（stage='dedup'）
# --------------------------------------------------------------------------


def _seed_pair(db):
    """两条近邻记忆 + 一张 dedup 卡（suggestion 形如 dedup_scan 产的）。

    seed_pending 的常规参数 content 兼作 raw_content 与 suggestion.content——
    dedup_scan 产的卡后者正是"默认合并文案"（A 的原文），口径一致。
    """
    a = tr.seed_memory(db, "pytest-review-旧记忆：发布走 service.ps1")
    b = tr.seed_memory(db, "pytest-review-新记忆：发布走 service.ps1 脚本")
    p = tr.seed_pending(db, content="pytest-review-旧记忆：发布走 service.ps1",
                        stage="dedup", model_kind="merge_pair",
                        memory_ids=[a, b],
                        content_a="pytest-review-旧记忆：发布走 service.ps1",
                        content_b="pytest-review-新记忆：发布走 service.ps1 脚本",
                        scope="project", similarity=0.93)
    return a, b, p


def test_merge_verdict_auto_merges(db, fake_llm):
    _a, _b, p = _seed_pair(db)
    fake_llm["install"]([prompts_auto.MergeJudge(
        verdict="merge", content="pytest-review-合并后的发布说明")])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "auto_merged"
    row = tr.pending(db, p)
    assert row["status"] == "approved" and row["resolved_by"] == "llm_auto"
    job = db.query_one(
        "SELECT payload FROM jobs WHERE kind='apply_decision' AND payload->>'pending_id'=%s",
        (str(p),))
    assert job["payload"]["resolution"]["kind"] == "merge_pair"
    assert job["payload"]["resolution"]["content"] == "pytest-review-合并后的发布说明"


def test_keep_both_auto_rejects_without_negative(db, fake_llm):
    _a, _b, p = _seed_pair(db)
    fake_llm["install"]([prompts_auto.MergeJudge(
        verdict="keep_both", why="两条记录的是不同对象")])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "auto_rejected"
    row = tr.pending(db, p)
    assert row["status"] == "rejected" and row["resolved_by"] == "llm_auto"
    # 关卡不写否决记录（raw_observations.polarity='negative' 是人工否决专用记号）
    assert db.query_one("SELECT count(*) AS n FROM raw_observations")["n"] == 0


# --------------------------------------------------------------------------
# 竞态 / 失败 / 开关
# --------------------------------------------------------------------------


def test_llm_failure_leaves_card_pending_and_raises(db, fake_llm):
    p = tr.seed_pending(db, content="pytest-review-LLM 挂了的卡",
                        section=tr.section_key(db), model_kind="observation")
    fake_llm["install"]([RuntimeError("gateway down")])
    with pytest.raises(RuntimeError):
        auto_review_mod.handle({"pending_id": p})
    row = tr.pending(db, p)
    assert row["status"] == "pending"  # 卡原状，job 走 A8 退避重试
    assert row["suggestion"].get("llm_review") is None


def test_already_adjudicated_card_is_skipped(db, fake_llm):
    p = tr.seed_pending(db, content="pytest-review-人已经审过了",
                        section=tr.section_key(db), model_kind="observation")
    db.execute("UPDATE pending_items SET status='rejected', resolved_by='human'"
               " WHERE id=%s", (p,))
    fake_llm["install"]([prompts_auto.JudgeItem(kind="observation", confidence=0.9)])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "skipped"
    assert fake_llm["client"].calls == []  # 没花 LLM 调用


def test_switch_off_skips_without_llm(db, fake_llm, monkeypatch):
    p = tr.seed_pending(db, content="pytest-review-开关关了",
                        section=tr.section_key(db), model_kind="observation")
    _off(monkeypatch)
    fake_llm["install"]([])
    out = auto_review_mod.handle({"pending_id": p})
    assert out["action"] == "disabled"
    assert tr.pending(db, p)["status"] == "pending"
