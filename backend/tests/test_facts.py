"""§5.5 四动作写入器的验收：ADD / UPDATE / DELETE(软) / NOOP + 硬幂等。

Gated on MEM_DSN。★ **不加载 bge-m3**：embedding 被打桩成"哈希词袋"，
确定性、毫秒级，而这里要验的是四个动作落库落对了，不是向量质量。
"""

import hashlib

import pytest
from conftest import requires_db

from app.core.hashing import content_hash
from app.pipeline import facts as facts_mod
from app.prompts import write_facts as write_prompt

pytestmark = requires_db

ORG = "dida"
PROJECT = "dida/gds-agg"
SOURCE_MSG = 900000001
DIM = 1024


class FakeLLM:
    def __init__(self, *payloads, raises=None):
        self.payloads = list(payloads)
        self.raises = raises
        self.prompts: list[str] = []

    def structured(self, task, *, system, messages, tool, schema, max_tokens=0):
        assert task == "write_facts"
        self.prompts.append(messages[0]["content"])
        if self.raises:
            raise self.raises
        return write_prompt.WriteResult.model_validate(self.payloads.pop(0))


class FakeEmbedder:
    """词袋哈希：同词多的两句余弦就高。确定性、无需模型。"""

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
    monkeypatch.setattr(facts_mod, "get_embedding_service", lambda: FakeEmbedder())


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    # `memories.source_msg` 有外键，所以得先造一条真实的 conversation_log 行当锚点。
    # 用固定 id 是为了让 SOURCE_MSG 能当模块常量用；顺手把序列推过去，
    # 免得后面真的摄取撞上这个 id。
    _db.execute(
        "INSERT INTO conversation_log (id, source, src_uuid, session_id, org_key, project_key,"
        "                             role, content)"
        " VALUES (%s,'pytest','pytest-facts-anchor','pytest-facts',%s,%s,'user','锚点')"
        " ON CONFLICT (id) DO NOTHING", (SOURCE_MSG, ORG, PROJECT))
    _db.execute("SELECT setval(pg_get_serial_sequence('conversation_log','id'),"
                " GREATEST((SELECT max(id) FROM conversation_log), 1))")
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM memories WHERE source_msg >= %s OR content LIKE %s",
                (SOURCE_MSG, "pytest-facts-%"))
    _db.execute("DELETE FROM conversation_log WHERE source = 'pytest'")


def payload(*items, org_key=ORG, project_key=PROJECT):
    return {"session_id": "pytest-facts", "source": "claude-code",
            "prompt_ver": "route-v1", "org_key": org_key, "project_key": project_key,
            "items": [{"source_msg": SOURCE_MSG, **item} for item in items]}


def fact(content, scope="project", subject="project", role="fact", keywords=(), **kw):
    return {"content": content, "scope": scope, "subject": subject, "role": role,
            "keywords": list(keywords), **kw}


def decision(index, action, **kw):
    return {"decisions": [{"index": index, "action": action, **kw}]}


def seed_memory(db, content, *, scope="project", subject="project", role="fact",
                keywords=(), mention_count=1):
    """插一条已存在的记忆，同时补一行 conversation_log 让 source_msg 外键成立。"""
    db.execute("INSERT INTO conversation_log (source, src_uuid, session_id, role, content)"
               " VALUES ('pytest','pytest-facts-seed','pytest-facts','user',%s)"
               " ON CONFLICT DO NOTHING", (content,))
    anchor = db.query_one("SELECT id FROM conversation_log WHERE src_uuid='pytest-facts-seed'")
    vector = _hash_vector(content)
    return db.query_one(
        "INSERT INTO memories (scope, org_key, project_key, subject, role, content,"
        "                      content_hash, keywords, embedding, mention_count, source_msg)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (scope, None if scope == "user" else ORG,
         PROJECT if scope == "project" else None, subject, role, content,
         content_hash(content), list(keywords), vector, mention_count,
         anchor["id"]))["id"]


def rows(db):
    return db.query("SELECT * FROM memories WHERE source_msg >= %s OR content LIKE %s ORDER BY id",
                    (SOURCE_MSG, "pytest-facts-%"))


def events(db, memory_id):
    return db.query("SELECT * FROM memory_events WHERE memory_id=%s ORDER BY id", (memory_id,))


def test_add_inserts_a_memory_and_an_event(db):
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-新事实：gds-agg 的手机号要去掉 86- 前缀",
                     keywords=["gds-agg", "86-"])),
        llm=FakeLLM(decision(0, "ADD")))
    assert (stats.added, stats.buckets, stats.calls) == (1, 1, 1)
    row = rows(db)[0]
    assert row["scope"] == "project" and row["org_key"] == ORG
    assert row["project_key"] == PROJECT
    assert row["mention_count"] == 1 and row["revision_count"] == 0
    assert row["content_hash"] == content_hash(row["content"])
    assert row["embedding"] is not None
    assert [e["action"] for e in events(db, row["id"])] == ["ADD"]


def test_noop_bumps_the_mention_count_and_adds_no_row(db):
    """★ 语义幂等：不同来源的同义事实被合并，而不是各存一行。"""
    existing = seed_memory(db, "pytest-facts-gds-agg 禁止引入 Lombok", keywords=["Lombok"])
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-gds-agg 里不要用 Lombok，一律写显式 getter",
                     keywords=["Lombok"])),
        llm=FakeLLM(decision(0, "NOOP", memory_id=existing, mention_delta=2)))
    assert stats.noop == 1 and stats.added == 0
    row = db.query_one("SELECT * FROM memories WHERE id=%s", (existing,))
    assert row["mention_count"] == 3
    assert row["revision_count"] == 0        # NOOP 不动 revision_count（§5.5）
    assert [e["action"] for e in events(db, existing)] == ["NOOP"]
    assert len(rows(db)) == 1                # 只有 seed 那一行


def test_update_rewrites_content_and_keeps_the_old_text_in_events(db):
    existing = seed_memory(db, "pytest-facts-gds-agg 用 JUnit 测试", keywords=["JUnit"])
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-gds-agg 已经从 JUnit 迁到 pytest", keywords=["pytest"])),
        llm=FakeLLM(decision(0, "UPDATE", memory_id=existing,
                             merged_content="pytest-facts-gds-agg 的单元测试框架是 pytest"
                                            "（原先用 JUnit，已迁移）")))
    assert stats.updated == 1 and stats.added == 0
    row = db.query_one("SELECT * FROM memories WHERE id=%s", (existing,))
    assert "pytest" in row["content"] and "JUnit" in row["content"]  # 旧信息没丢
    assert row["revision_count"] == 1
    assert row["content_hash"] == content_hash(row["content"])
    event = events(db, existing)[0]
    assert event["action"] == "UPDATE"
    assert event["old_content"] == "pytest-facts-gds-agg 用 JUnit 测试"
    assert "迁移" in event["new_content"]


def test_delete_soft_deletes_the_contradiction_and_adds_the_new_fact(db):
    """★ 软删除：旧内容留在 `memory_events`，"曾经以为 X 后来发现是 Y"这条轨迹可追溯。"""
    existing = seed_memory(db, "pytest-facts-改签不需要先验价", keywords=["改签"])
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-改签必须先调 RetrievePnr 验价", keywords=["改签"])),
        llm=FakeLLM(decision(0, "DELETE", memory_id=existing)))
    assert stats.deleted == 1 and stats.added == 1
    old = db.query_one("SELECT * FROM memories WHERE id=%s", (existing,))
    assert old["status"] == "deleted"
    new = [r for r in rows(db) if r["id"] != existing][0]
    assert new["status"] == "active" and "必须" in new["content"]
    assert [e["action"] for e in events(db, existing)] == ["DELETE"]
    assert [e["action"] for e in events(db, new["id"])] == ["ADD"]


def test_replaying_the_same_payload_adds_nothing(db):
    """★ §5.5 硬幂等：job 重试 / 崩溃重放不产生第二行，靠的是
    `UNIQUE(source_msg, content_hash)`，不是靠 LLM 判 NOOP。"""
    item = fact("pytest-facts-重放测试：枚举名必须全大写", keywords=["枚举"])
    first = facts_mod.write_facts(payload(item), llm=FakeLLM(decision(0, "ADD")))
    second = facts_mod.write_facts(payload(item), llm=FakeLLM(decision(0, "ADD")))
    assert first.added == 1
    assert second.added == 0 and second.dup_blocked == 1
    assert len(rows(db)) == 1


def test_an_unjudged_fact_is_added_rather_than_dropped(db):
    """LLM 漏判了某一条 → 默认 ADD。多一条重复下次还能合并，漏一条就永远没了。"""
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-第一条", keywords=["a"]),
                fact("pytest-facts-第二条", keywords=["b"])),
        llm=FakeLLM(decision(0, "ADD")))
    assert stats.unjudged == 1 and stats.added == 2
    assert len(rows(db)) == 2


def test_a_parse_failure_falls_back_to_add(db):
    from app.core.llm import LLMParseError

    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-schema 挂了也要落库", keywords=["x"])),
        llm=FakeLLM(raises=LLMParseError("boom", raw={})))
    assert stats.parse_failed == 1 and stats.added == 1


# ─────────────────────── A4：候选集校验（W2 幻觉引用） ───────────────────────

def test_a_hallucinated_memory_id_falls_back_to_add(db, caplog):
    """★ A4（W2）：LLM 编造候选集之外的 memory_id 去执行 UPDATE/DELETE，
    要么打错人要么静默丢弃素材。按 unjudged 兜底走 ADD，素材宁多不丢。"""
    import logging

    llm = FakeLLM(decision(0, "UPDATE", memory_id=999999999, merged_content="pytest-facts-被幻觉指到"))
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-新增事实：验价服务超时 3 秒熔断", keywords=["验价"])),
        llm=llm)
    assert stats.invalid_refs == 1 and stats.added == 1
    assert stats.updated == 0
    assert not any(r["content"] == "pytest-facts-被幻觉指到" for r in rows(db))
    assert any("999999999" in r.message for r in caplog.records
               if r.levelno == logging.WARNING)


def test_an_update_pointing_at_a_missing_row_is_reported_not_silent(db, caplog):
    """★ A4 另一半：id 在候选集里（LLM 见过）但执行时行已没了（并发删除）——
    rowcount==0 必须留痕迹，不能当成功过。构造法：把候选记忆的 status 改成
    deleted，dense 查询看不见它，但 keywords 通道还能捞回来进候选。"""
    import logging

    memory_id = seed_memory(db, "pytest-facts-bookingFlowService 缺婴儿票校验",
                            keywords=["bookingFlow"])
    db.execute("UPDATE memories SET status='deleted' WHERE id=%s", (memory_id,))
    llm = FakeLLM(decision(0, "UPDATE", memory_id=memory_id, merged_content="pytest-facts-更新到已删行"))
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-bookingFlow 新增改签分支", keywords=["bookingFlow"])),
        llm=llm)
    assert stats.invalid_refs == 1
    assert stats.updated == 0
    assert any(str(memory_id) in r.message for r in caplog.records
               if r.levelno == logging.WARNING)


# ─────────────────────── A7：write_facts 逐桶续租回调 ───────────────────────

def test_the_on_bucket_callback_fires_for_every_chunk(db):
    """★ A7（W3）：write_facts 是最慢的 job（LLM 判重逐桶串行），route 式
    窗口级续租盖不住它——租约过期会被第二个 worker 双执行。回调每 chunk
    触发一次，worker 借此 renew_lease。"""
    seen = []

    def on_bucket(idx, total):
        seen.append((idx, total))

    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-续租回调测试一", keywords=["a"]),
                fact("pytest-facts-续租回调测试二", keywords=["b"])),
        llm=FakeLLM(decision(0, "ADD"), decision(0, "ADD")),
        on_bucket=on_bucket)
    # 两条 fact 同 scope/subject/role/org/project → 同一个桶，总量 <MAX_FACTS_PER_CALL
    # → 只有一个 chunk，回调触发一次 (0, 1)。要触发多 chunk 需要 >20 条，过重。
    assert len(seen) == 1
    assert seen == [(0, 1)]
    assert stats.added == 2


def test_a_failing_on_bucket_callback_does_not_kill_the_write(db):
    """续租是尽力而为：回调炸了只记日志，写入主流程不能跟着陪葬。"""

    def broken(idx, total):
        raise RuntimeError("lease renew raced")

    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-回调炸了素材还得落库", keywords=["x"])),
        llm=FakeLLM(decision(0, "ADD")),
        on_bucket=broken)
    assert stats.added == 1


def test_scope_user_drops_org_key_so_it_stays_globally_visible(db):
    """★ §6.1 的可见性条件是 `scope='user' OR (scope='org' AND org_key=:org) OR ...`。
    user 级记忆要是带上了 org_key，换个 org 的会话就召不回来了。"""
    facts_mod.write_facts(
        payload(fact("pytest-facts-用户永远要求中文回答", scope="user", subject="user",
                     role="preference", keywords=["中文"]),
                org_key=ORG, project_key=PROJECT),
        llm=FakeLLM(decision(0, "ADD")))
    row = rows(db)[0]
    assert row["scope"] == "user" and row["org_key"] is None and row["project_key"] is None


def test_scope_org_keeps_org_key_but_not_project_key(db):
    facts_mod.write_facts(
        payload(fact("pytest-facts-全公司仓库都从 dev 拉分支", scope="org", subject="project",
                     role="fact", keywords=["dev"])),
        llm=FakeLLM(decision(0, "ADD")))
    row = rows(db)[0]
    assert (row["scope"], row["org_key"], row["project_key"]) == ("org", ORG, None)


def test_scope_project_without_a_project_key_is_skipped_not_crashed(db):
    """memories 的 CHECK 约束会拒掉整批；在入口就拦下来，别让一条脏数据毁掉一桶。"""
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-没有 project_key", scope="project"), project_key=None),
        llm=FakeLLM())
    assert stats.items == 0 and stats.calls == 0
    assert rows(db) == []


def test_different_scopes_land_in_different_buckets_and_get_separate_calls(db):
    """★ 分桶的意义：只有同一分区内的记忆才可能互为重复，跨桶比对是纯噪声。"""
    llm = FakeLLM(decision(0, "ADD"), decision(0, "ADD"))
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-项目级的事实", scope="project", keywords=["p"]),
                fact("pytest-facts-用户级的偏好", scope="user", subject="user",
                     role="preference", keywords=["u"])),
        llm=llm)
    assert stats.buckets == 2 and stats.calls == 2
    assert stats.added == 2


def test_the_keyword_channel_finds_what_the_vector_channel_cannot(db):
    """★ 关键词通道存在的理由：精确标识符 dense 经常糊掉（§6.2）。
    把已有记忆的 embedding 置空，dense 通道就一定看不见它，只有 `keywords && %s` 能捞回来。"""
    memory_id = seed_memory(db, "pytest-facts-bookingFlowService 缺婴儿票校验分支",
                            keywords=["bookingFlowService"])
    db.execute("UPDATE memories SET embedding=NULL WHERE id=%s", (memory_id,))
    llm = FakeLLM(decision(0, "NOOP", memory_id=memory_id))
    facts_mod.write_facts(
        payload(fact("pytest-facts-一句语义上完全不相干的话", keywords=["bookingFlowService"])),
        llm=llm)
    assert f"id={memory_id}" in llm.prompts[0]


def test_the_dense_channel_finds_synonyms_without_any_shared_keyword(db):
    memory_id = seed_memory(db, "pytest-facts-gds-agg 禁止引入 Lombok 依赖",
                            keywords=["完全不相干的词"])
    llm = FakeLLM(decision(0, "NOOP", memory_id=memory_id))
    facts_mod.write_facts(
        payload(fact("pytest-facts-gds-agg 里不要用 Lombok，一律写显式 getter",
                     keywords=["另一个词"])),
        llm=llm)
    assert f"id={memory_id}" in llm.prompts[0]


def test_an_empty_partition_tells_the_model_to_add_everything(db):
    """冷启动（§4.3.2）时已有记忆列表是空的，prompt 必须明说，否则模型会瞎编 memory_id。"""
    llm = FakeLLM(decision(0, "ADD"))
    facts_mod.write_facts(
        payload(fact("pytest-facts-冷启动第一条", keywords=["冷启动"]),
                project_key="dida/pytest-facts-empty-bucket"),
        llm=llm)
    assert "还没有任何记忆" in llm.prompts[0]


def test_a_role_mismatched_twin_is_now_a_candidate_not_a_second_row(db):
    """★ B4（S5 去重角色墙）：同一件事被 route 判成 role=preference、新事实判成 role=fact
    （#2441/#2506 的形状）——旧口径下候选查询按 role 过滤，两条互相看不见，
    落成两行永不合拢；放宽到 (scope, org, project) 后必须能对上（NOOP 命中旧行）。"""
    existing = seed_memory(db, "pytest-facts-用户永远要求中文回答",
                           scope="user", subject="user", role="preference",
                           keywords=["中文"])
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-用户永远要求中文回答", scope="user", subject="user",
                     role="fact", keywords=["中文"])),
        llm=FakeLLM(decision(0, "NOOP", memory_id=existing)))
    assert stats.noop == 1 and stats.added == 0
    assert len(rows(db)) == 1


def test_a_cross_subject_candidate_still_ranks_behind_the_same_tag_one(db):
    """★ B4：subject/role 降级为加分项——跨标签候选进得来（上面的用例），
    但同标签的候选在平分时要排前面（加分只影响边界排序，不影响进不进得来）。"""
    same_tag = seed_memory(db, "pytest-facts-gds-agg 禁止引入 Lombok",
                           subject="project", role="fact", keywords=["Lombok"])
    cross_tag = seed_memory(db, "pytest-facts-gds-agg 禁止引入 Lombok 补充说明",
                            subject="user", role="preference", keywords=["Lombok"])
    stats = facts_mod.write_facts(
        payload(fact("pytest-facts-gds-agg 禁止引入 Lombok", keywords=["Lombok"])),
        llm=FakeLLM(decision(0, "NOOP", memory_id=same_tag)))
    # 候选顺序里同标签的排前面（tag_match 加分），且两者都进了候选。
    probe = facts_mod._facts_from(payload(
        fact("pytest-facts-gds-agg 禁止引入 Lombok", keywords=["Lombok"])))[0]
    probe.embedding = facts_mod.get_embedding_service().encode_one(probe.content)
    with facts_mod.db.transaction() as tx:
        facts_mod.db.enable_iterative_scan(tx)
        probe.candidates = facts_mod._candidates(tx, probe)
    ids = [c["id"] for c in probe.candidates]
    assert same_tag in ids and cross_tag in ids
    assert ids.index(same_tag) < ids.index(cross_tag)


def test_the_iterative_scan_setting_is_accepted_by_this_pgvector(db):
    """★ 回归保护：方案 §6.3 原先写的是 `relaxed_ordering`，pgvector 0.8.7 直接报
    InvalidParameterValue。这个开关要是没生效，症状不是报错而是"召回莫名其妙变少"
    （§6.3 那个静默失效），所以钉一条测试。"""
    from app.core import db as _db

    with _db.transaction() as conn:
        _db.enable_iterative_scan(conn)
        row = conn.execute("SELECT current_setting('hnsw.iterative_scan') AS v,"
                           " current_setting('hnsw.max_scan_tuples') AS t").fetchone()
    assert row["v"] == "relaxed_order"
    assert int(row["t"]) == 20000
