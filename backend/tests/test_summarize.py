"""§5.6 汇总器的验收：观察池攒够 → 合成 draft → doc_proposals + 标 consumed。

Gated on MEM_DSN。★ **不加载 bge-m3、不调 LLM**：汇总全程是纯文本 + SQL，
向量在这一步用不上；LLM 被打桩成"回一段固定正文"，要验的是触发判定、
锁档跳过、消费与提案落库对不对，不是合成质量。
"""

import pytest
from conftest import requires_db

from app.core.hashing import content_hash
from app.pipeline import summarize as summ
from app.prompts import summarize as summ_prompt

pytestmark = requires_db

ORG = "dida"
PROJECT = "dida/pytest-summarize"
SECTION = "dependency"
MARK = "pytest-summ-"


class FakeLLM:
    """回一段固定 content（或空），并记下每次收到的 user message 供断言。"""

    def __init__(self, content="合成后的规范正文", reason="合并了同义项", raises=None):
        self.content = content
        self.reason = reason
        self.raises = raises
        self.prompts: list[str] = []
        self.calls = 0

    def structured(self, task, *, system, messages, tool, schema, max_tokens=0):
        assert task == "summarize"
        assert tool["name"] == summ_prompt.TOOL["name"]
        self.calls += 1
        self.prompts.append(messages[0]["content"])
        if self.raises:
            raise self.raises
        return summ_prompt.SummarizeResult(content=self.content, reason=self.reason)


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM doc_proposals WHERE project_key = %s OR org_key = %s",
                (PROJECT, "pytest-summ-org"))
    _db.execute("DELETE FROM living_doc WHERE project_key = %s", (PROJECT,))
    _db.execute("DELETE FROM raw_observations WHERE project_key = %s OR content LIKE %s",
                (PROJECT, MARK + "%"))


def seed_obs(db, content, *, polarity="positive", consumed=False, section=SECTION,
             project=PROJECT, org=ORG, keywords=()):
    return db.query_one(
        "INSERT INTO raw_observations (org_key, project_key, section_key, content,"
        "                              content_hash, polarity, keywords, consumed)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (org, project, section, content, content_hash(content), polarity,
         list(keywords), consumed))["id"]


def seed_doc(db, content, *, authority="draft", section=SECTION, project=PROJECT, org=ORG):
    return db.query_one(
        "INSERT INTO living_doc (org_key, project_key, section_key, content, authority)"
        " VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (org, project, section, content, authority))["id"]


def obs_state(db, ids):
    rows = db.query("SELECT id, consumed FROM raw_observations WHERE id = ANY(%s) ORDER BY id",
                    (list(ids),))
    return {r["id"]: r["consumed"] for r in rows}


def proposals(db):
    return db.query("SELECT * FROM doc_proposals WHERE project_key = %s ORDER BY id", (PROJECT,))


# ── 触发阈值 ──────────────────────────────────────────────────────────────

def test_new_section_below_threshold_does_nothing(db):
    """新 section（无 living_doc 行）阈值 = trigger_new(3)。攒 2 条不该动。"""
    ids = [seed_obs(db, f"{MARK}线索{i}") for i in range(2)]
    llm = FakeLLM()
    out = summ.summarize_partition(ORG, PROJECT, SECTION, llm=llm)
    assert out["action"] == "below_threshold"
    assert out["threshold"] == 3 and out["count"] == 2
    assert llm.calls == 0                      # 没到阈值连 LLM 都不调
    assert not any(obs_state(db, ids).values())  # 线索原样留着


def test_new_section_reaches_threshold_proposes_and_consumes(db):
    ids = [seed_obs(db, f"{MARK}线索{i}") for i in range(3)]
    llm = FakeLLM(content="禁止引入 Lombok；一律写显式 getter")
    out = summ.summarize_partition(ORG, PROJECT, SECTION, llm=llm)
    assert out["action"] == "proposed" and out["consumed"] == 3
    assert all(obs_state(db, ids).values())    # 三条都标了 consumed
    prop = proposals(db)
    assert len(prop) == 1
    assert prop[0]["status"] == "pending"
    assert prop[0]["old_content"] is None      # 新 section，没有旧草稿
    assert prop[0]["new_content"] == "禁止引入 Lombok；一律写显式 getter"
    assert sorted(prop[0]["source_ids"]) == sorted(ids)   # ★ 溯源链（migration 0002）


def test_mature_section_needs_twenty(db):
    """已有 draft 行的成熟 section 阈值 = trigger_mature(20)，攒 5 条不动。"""
    seed_doc(db, "禁止引入 Lombok")
    ids = [seed_obs(db, f"{MARK}线索{i}") for i in range(5)]
    llm = FakeLLM()
    out = summ.summarize_partition(ORG, PROJECT, SECTION, llm=llm)
    assert out["action"] == "below_threshold" and out["threshold"] == 20
    assert llm.calls == 0 and not any(obs_state(db, ids).values())


def test_mature_section_absorbs_old_draft_into_old_content(db):
    seed_doc(db, "禁止引入 Lombok")
    [seed_obs(db, f"{MARK}线索{i}") for i in range(20)]
    llm = FakeLLM(content="禁止引入 Lombok；枚举常量必须大写下划线")
    out = summ.summarize_partition(ORG, PROJECT, SECTION, llm=llm)
    assert out["action"] == "proposed"
    prop = proposals(db)[0]
    assert prop["old_content"] == "禁止引入 Lombok"   # diff 的左边是旧草稿
    assert "当前草稿" in llm.prompts[0] and "禁止引入 Lombok" in llm.prompts[0]


# ── 锁档：confirmed / pinned 在 LLM 之前就跳过 ───────────────────────────

@pytest.mark.parametrize("authority", ["confirmed", "pinned"])
def test_locked_doc_is_never_summarized(db, authority):
    seed_doc(db, "人工钉死的规矩", authority=authority)
    ids = [seed_obs(db, f"{MARK}线索{i}") for i in range(30)]
    llm = FakeLLM()
    out = summ.summarize_partition(ORG, PROJECT, SECTION, force=True, llm=llm)
    assert out["action"] == "locked" and out["authority"] == authority
    assert llm.calls == 0                     # ★ 连 LLM 都不调
    assert not any(obs_state(db, ids).values())  # 线索继续攒着，不消费
    assert proposals(db) == []


# ── force / 手动触发 ─────────────────────────────────────────────────────

def test_force_bypasses_threshold(db):
    ids = [seed_obs(db, f"{MARK}线索{i}") for i in range(1)]   # 只有 1 条，远低于 3
    llm = FakeLLM(content="单条也合成")
    out = summ.summarize_partition(ORG, PROJECT, SECTION, force=True, llm=llm)
    assert out["action"] == "proposed" and all(obs_state(db, ids).values())


def test_no_observations_is_a_noop(db):
    out = summ.summarize_partition(ORG, PROJECT, SECTION, force=True, llm=FakeLLM())
    assert out["action"] == "no_observations"


# ── 已有一版等人审：不叠、不消费、不调 LLM ────────────────────────────────

def test_already_pending_blocks_a_second_proposal(db):
    seed_obs(db, f"{MARK}第一轮")
    seed_obs(db, f"{MARK}第一轮b")
    first = summ.summarize_partition(ORG, PROJECT, SECTION, force=True,
                                     llm=FakeLLM(content="第一版"))
    assert first["action"] == "proposed"
    # 又攒了新线索，但上一版还没被裁决
    seed_obs(db, f"{MARK}第二轮")
    llm = FakeLLM(content="第二版")
    out = summ.summarize_partition(ORG, PROJECT, SECTION, force=True, llm=llm)
    assert out["action"] == "already_pending"
    assert out["proposal_id"] == first["proposal_id"]
    assert llm.calls == 0                      # 不调 LLM
    assert len(proposals(db)) == 1             # 没有叠出第二版


# ── LLM 判"没得写" ───────────────────────────────────────────────────────

def test_empty_content_consumes_but_proposes_nothing(db):
    ids = [seed_obs(db, f"{MARK}噪声{i}") for i in range(3)]
    llm = FakeLLM(content="   ")               # 全空白 = 无可写内容
    out = summ.summarize_partition(ORG, PROJECT, SECTION, llm=llm)
    assert out["action"] == "empty"
    assert all(obs_state(db, ids).values())    # 仍消费掉，否则每轮重复触发白烧 LLM
    assert proposals(db) == []


# ── 解析失败：线索留着下轮再试 ────────────────────────────────────────────

def test_parse_failure_leaves_observations_unconsumed(db):
    from app.core.llm import LLMParseError
    ids = [seed_obs(db, f"{MARK}线索{i}") for i in range(3)]
    llm = FakeLLM(raises=LLMParseError("boom"))
    out = summ.summarize_partition(ORG, PROJECT, SECTION, llm=llm)
    assert out["action"] == "parse_failed" and out["parse_failed"] == 1
    assert not any(obs_state(db, ids).values())  # 不消费、不落提案
    assert proposals(db) == []


# ── 否决清单进 prompt，且不混进 positive 线索 ─────────────────────────────

def test_negatives_are_listed_for_exclusion_but_not_summarized(db):
    [seed_obs(db, f"{MARK}正{i}") for i in range(3)]
    seed_obs(db, f"{MARK}这条被人否决过", polarity="negative")
    llm = FakeLLM(content="只写正向的")
    summ.summarize_partition(ORG, PROJECT, SECTION, llm=llm)
    prompt = llm.prompts[0]
    assert "已否决" in prompt and f"{MARK}这条被人否决过" in prompt
    # 否决项不算进 positive 线索，也不该被消费
    neg = db.query_one("SELECT consumed FROM raw_observations WHERE content = %s",
                       (f"{MARK}这条被人否决过",))
    assert neg["consumed"] is False


# ── 单次合成的线索上限 ───────────────────────────────────────────────────

def test_only_the_first_batch_is_consumed_per_run(db):
    ids = [seed_obs(db, f"{MARK}线索{i:03d}") for i in range(summ.MAX_OBSERVATIONS_PER_CALL + 5)]
    llm = FakeLLM(content="一次吃不下这么多")
    out = summ.summarize_partition(ORG, PROJECT, SECTION, force=True, llm=llm)
    assert out["consumed"] == summ.MAX_OBSERVATIONS_PER_CALL
    state = obs_state(db, ids)
    assert sum(state.values()) == summ.MAX_OBSERVATIONS_PER_CALL   # 剩下的留给下一轮
    assert sorted(proposals(db)[0]["source_ids"]) == sorted(
        i for i, c in state.items() if c)


# ── run() 枚举候选分区 ───────────────────────────────────────────────────

def test_run_scans_and_reports(db):
    # 分区 A：够阈值 → proposed；分区 B：只有 1 条 → below_threshold
    [seed_obs(db, f"{MARK}A{i}") for i in range(3)]
    seed_obs(db, f"{MARK}B0", section="code_style")
    llm = FakeLLM(content="A 的规范")
    stats = summ.run(org_key=ORG, project_key=PROJECT, llm=llm)
    assert stats.scanned == 2 and stats.proposed == 1 and stats.skipped == 1
    actions = {p["section_key"]: p["action"] for p in stats.partitions}
    assert actions[SECTION] == "proposed"
    assert actions["code_style"] == "below_threshold"


def test_run_targeting_one_section_still_respects_threshold(db):
    """★ section_key 只圈范围，不等于 force：自动触发（review 落库后排 job）也带
    section_key，推断成 force 会让每条线索都强制调 LLM，阈值形同虚设。"""
    seed_obs(db, f"{MARK}只有一条")
    llm = FakeLLM(content="不该被合成")
    stats = summ.run(org_key=ORG, project_key=PROJECT, section_key=SECTION, llm=llm)
    assert stats.proposed == 0 and llm.calls == 0
    assert stats.partitions[0]["action"] == "below_threshold"


def test_run_with_explicit_force_bypasses_threshold(db):
    """UI 的"强制汇总"按钮显式传 force=true，单条也合成。"""
    seed_obs(db, f"{MARK}只有一条")
    llm = FakeLLM(content="手动合成")
    stats = summ.run(org_key=ORG, project_key=PROJECT, section_key=SECTION,
                     force=True, llm=llm)
    assert stats.proposed == 1
    assert stats.partitions[0]["action"] == "proposed"


def test_candidate_sections_counts_only_unconsumed_positive(db):
    seed_obs(db, f"{MARK}活1")
    seed_obs(db, f"{MARK}活2")
    seed_obs(db, f"{MARK}已消费", consumed=True)
    seed_obs(db, f"{MARK}否决", polarity="negative")
    rows = summ.candidate_sections(org_key=ORG, project_key=PROJECT, section_key=SECTION)
    assert len(rows) == 1 and rows[0]["n"] == 2
