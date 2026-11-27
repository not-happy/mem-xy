"""§5.2/§5.4 路由与阈值的验收：用假 LLM 跑真库，验分流去向而不是验模型。

Gated on MEM_DSN. 写的是真库，所以 session_id 一律带 `pytest-route-` 前缀。
★ 栏位匹配被 monkeypatch 掉——它要加载 bge-m3（冷启动 33s），而这里测的是阈值分流。
"""

import json

import pytest
from conftest import requires_db

from app.pipeline import router, sections
from app.prompts import route as route_prompt

pytestmark = requires_db

SESSION = "pytest-route-0001"
SOURCE = "claude-code"
ORG = "dida"
PROJECT = "dida/gds-agg"


class FakeLLM:
    """按调用顺序吐预设结果；`raises` 用来模拟 §2.2 那条"不许静默丢"的路径。"""

    def __init__(self, *payloads, raises=None):
        self.payloads = list(payloads)
        self.raises = raises
        self.calls: list[str] = []

    def structured(self, task, *, system, messages, tool, schema, max_tokens=0):
        assert task == "route"
        self.calls.append(messages[0]["content"])
        if self.raises:
            raise self.raises
        return route_prompt.RouteResult.model_validate(self.payloads.pop(0))

    @property
    def call_count(self):
        return len(self.calls)


def item(kind, content="在 gds-agg 项目中，X 使用 Y", confidence=0.9, **kw):
    return {"kind": kind, "content": content, "confidence": confidence, **kw}


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM raw_observations WHERE source_msg IN"
                " (SELECT id FROM conversation_log WHERE session_id LIKE 'pytest-route-%')")
    _db.execute("DELETE FROM jobs WHERE kind='apply_decision' AND payload->>'pending_id' IN"
                " (SELECT id::text FROM pending_items WHERE source_msg IN"
                "  (SELECT id FROM conversation_log WHERE session_id LIKE 'pytest-route-%'))")
    _db.execute("DELETE FROM pending_items WHERE source_msg IN"
                " (SELECT id FROM conversation_log WHERE session_id LIKE 'pytest-route-%')")
    _db.execute("DELETE FROM route_decisions WHERE source_msg IN"
                " (SELECT id FROM conversation_log WHERE session_id LIKE 'pytest-route-%')")
    _db.execute("DELETE FROM jobs WHERE dedup_key LIKE %s", ("write_facts:%:pytest-route-%",))
    _db.execute("DELETE FROM extraction_cursor WHERE session_id LIKE 'pytest-route-%'")
    _db.execute("DELETE FROM route_checkpoints WHERE session_id LIKE 'pytest-route-%'")
    _db.execute("DELETE FROM conversation_log WHERE session_id LIKE 'pytest-route-%'")


@pytest.fixture
def session(db):
    """两个窗口：一个真人输入 + 一条 AI 回复，再来一个真人输入。"""
    with db.transaction() as conn:
        for turn, (role, content) in enumerate(
                [("user", "不要用 Lombok"), ("assistant", "好的，改用显式 getter"),
                 ("user", "枚举名要全大写")], start=1):
            conn.execute(
                "INSERT INTO conversation_log (source, src_uuid, session_id, org_key,"
                " project_key, cwd, git_branch, role, content, turn_no, tokens_est)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (SOURCE, f"pytest-route-{turn}", SESSION, ORG, PROJECT,
                 "C:/Users/yong.xu/IdeaProjects/dida/gds-agg", "dev",
                 role, content, turn, len(content)))
    return db.query("SELECT id, turn_no FROM conversation_log WHERE session_id=%s ORDER BY id",
                    (SESSION,))


@pytest.fixture
def matched(monkeypatch):
    """栏位匹配固定命中 code_style，避免测试依赖模型与词表。"""
    hit = sections.SectionMatch(section_key="code_style", score=0.81, margin=0.3,
                                runner_up="naming_docs", org_key=ORG, candidates=22)
    monkeypatch.setattr(sections, "match", lambda *a, **k: hit)
    return hit


def pending(db):
    """★ 必须按 pytest 会话过滤：库里已经有真跑留下的待办卡片，
    不加条件的话测试就成了"要求全库为空"，第一次真跑之后就会全红。"""
    return db.query("SELECT * FROM pending_items WHERE source_msg IN"
                    " (SELECT id FROM conversation_log WHERE session_id LIKE 'pytest-route-%')"
                    " ORDER BY id")


def decisions(db):
    return db.query("SELECT * FROM route_decisions WHERE source_msg IN"
                    " (SELECT id FROM conversation_log WHERE session_id LIKE 'pytest-route-%')"
                    " ORDER BY id")


def test_observation_goes_to_human_review_even_at_confidence_one(db, session, matched):
    """★ #51：规矩类不设阈值。confidence 在这一类上实测毫无区分度（判错也报 0.93–1.0），
    0.95 阈值一条也拦不住，所以全审是唯一能捞回来的办法。"""
    llm = FakeLLM({"items": [item("observation", "在 gds-agg 项目中禁止引入 Lombok",
                                  confidence=1.0)]},
                  {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.pending == 1 and stats.facts == 0
    card = pending(db)[0]
    assert card["stage"] == "route"
    assert card["reason"] == "observation_needs_review"
    assert card["status"] == "pending"
    assert card["suggestion"]["section_key"] == "code_style"


def test_the_review_card_preselects_observation(db, session, matched):
    """★ §5.4：待办卡片**预选 observation**（偏向规矩侧），一眼就能改回 atomic_fact。
    偏向是安全的——误判成 observation 只是多审一次，误判成 atomic_fact 则永远发现不了。"""
    llm = FakeLLM({"items": [item("atomic_fact", "在 gds-agg 里坚持用 pytest",
                                  confidence=0.40, scope="project", subject="project",
                                  role="preference")]},
                  {"items": []})
    with db.transaction() as conn:
        router.route_session(SESSION, conn=conn, llm=llm)
    card = pending(db)[0]
    assert card["reason"] == "below_threshold:0.85"
    assert card["suggestion"]["kind"] == "observation"
    assert card["suggestion"]["model_kind"] == "atomic_fact"


def test_confident_atomic_fact_is_queued_for_the_writer(db, session, matched):
    llm = FakeLLM({"items": [item("atomic_fact", confidence=0.93, scope="project",
                                  subject="project", role="fact",
                                  keywords=["gds-agg", "Lombok"])]},
                  {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.facts == 1 and stats.pending == 0
    job = db.query_one("SELECT * FROM jobs WHERE dedup_key LIKE %s",
                       ("write_facts:%:pytest-route-%",))
    assert job["kind"] == "write_facts"
    payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])
    assert payload["session_id"] == SESSION
    assert payload["items"][0]["role"] == "fact"
    assert payload["items"][0]["keywords"] == ["gds-agg", "Lombok"]
    assert payload["items"][0]["source_msg"] == session[0]["id"]


def test_a_confident_discard_only_leaves_an_audit_trail(db, session, matched):
    llm = FakeLLM({"items": [{"kind": "discard", "reason": "derivable", "confidence": 0.99}]},
                  {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.discarded == 1 and stats.pending == 0 and stats.facts == 0
    assert pending(db) == []
    assert decisions(db)[0]["kind"] == "discard"


def test_an_unsure_discard_still_leaves_no_review_card(db, session, matched):
    """★ 2026-10-23 真跑后翻案：原先 discard 低于 0.95 就送人工，理由是"丢掉的捡不回来"。
    真跑 3 个窗口发现前提错了——discard 没有 content，卡片只能退回整段窗口原文，
    一个窗口几条 discard 就是几张一模一样的卡；人一点"通过"，预选的 observation
    还会把**原始 transcript** 写进线索池。而它其实可恢复：route_decisions 有审计行，
    conversation_log 是不可变真相源，prompt_ver 一升就全量重判。漏判交给 §10 人工抽样。"""
    llm = FakeLLM({"items": [{"kind": "discard", "reason": "one_off", "confidence": 0.60}]},
                  {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.discarded == 1 and stats.pending == 0
    assert pending(db) == []
    assert decisions(db)[0]["kind"] == "discard", "审计行仍然要留，否则漏判率无从抽样"


def test_an_unmatchable_section_lands_in_stage_section(db, session, monkeypatch):
    monkeypatch.setattr(sections, "match", lambda *a, **k: None)
    llm = FakeLLM({"items": [item("observation")]}, {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.section_unmatched == 1
    card = pending(db)[0]
    assert card["stage"] == "section"
    assert card["reason"] == "observation_needs_review+section_unmatched"
    assert card["suggestion"]["section_key"] is None


def test_every_item_leaves_a_route_decision_row(db, session, matched):
    """`route_decisions` 是 §2.3 改判率的原始数据，少一行就少一条标注。"""
    llm = FakeLLM(
        {"items": [item("observation", confidence=0.7), item("discard", confidence=0.99,
                                                             reason="derivable")]},
        {"items": [item("atomic_fact", confidence=0.9, scope="org", subject="project",
                        role="fact")]},
    )
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.items == 3 and stats.calls == 2
    rows = decisions(db)
    assert [r["kind"] for r in rows] == ["observation", "discard", "atomic_fact"]
    assert all(r["prompt_ver"] == route_prompt.PROMPT_VER for r in rows)
    assert all(r["org_key"] == ORG for r in rows)


def test_the_model_sees_the_project_context_so_content_can_be_self_contained(db, session,
                                                                             matched):
    """★ §5.2 硬要求：content 必须自包含。前提是模型得知道自己在哪个仓库。"""
    llm = FakeLLM({"items": []}, {"items": []})
    with db.transaction() as conn:
        router.route_session(SESSION, conn=conn, llm=llm)
    assert "dida/gds-agg" in llm.calls[0]
    assert "IdeaProjects/dida/gds-agg" in llm.calls[0]
    assert "【用户】不要用 Lombok" in llm.calls[0]


def test_cursor_advances_and_a_rerun_calls_the_llm_zero_times(db, session, matched):
    """★ §13 P2 验收：同会话重跑不产生重复判定。"""
    llm = FakeLLM({"items": [item("observation")]}, {"items": []})
    with db.transaction() as conn:
        router.route_session(SESSION, conn=conn, llm=llm)
    assert llm.call_count == 2
    cursor = db.query_one("SELECT * FROM extraction_cursor WHERE session_id=%s", (SESSION,))
    assert cursor["last_log_id"] == session[-1]["id"]
    assert cursor["prompt_ver"] == route_prompt.PROMPT_VER

    again = FakeLLM()
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=again)
    assert again.call_count == 0
    assert stats.windows == 0
    assert len(decisions(db)) == 1


def test_a_prompt_version_bump_reprocesses_the_whole_session(db, session, matched, monkeypatch):
    """换 prompt 就是换判据，旧判定作废；两版并存才好算改判率（§2.3）。"""
    discard = {"items": [{"kind": "discard", "reason": "derivable", "confidence": 0.99}]}
    with db.transaction() as conn:
        router.route_session(SESSION, conn=conn, llm=FakeLLM(discard, {"items": []}))
    assert len(decisions(db)) == 1

    old_ver = route_prompt.PROMPT_VER
    new_ver = old_ver + "-test"
    monkeypatch.setattr(route_prompt, "PROMPT_VER", new_ver)
    llm = FakeLLM(discard, {"items": []})
    with db.transaction() as conn:
        router.route_session(SESSION, conn=conn, llm=llm)
    assert llm.call_count == 2          # 游标被重置了，两个窗口都重判
    rows = decisions(db)
    assert {r["prompt_ver"] for r in rows} == {old_ver, new_ver}
    assert db.query_one("SELECT prompt_ver FROM extraction_cursor WHERE session_id=%s",
                        (SESSION,))["prompt_ver"] == new_ver


def test_a_parse_failure_lands_in_pending_and_never_drops_the_material(db, session, matched):
    """★ §2.2：模型答了但输出不合 schema，必须落到 `pending_items`，不能丢。"""
    from app.core.llm import LLMParseError

    llm = FakeLLM(raises=LLMParseError("schema validation failed", raw={"content": []}))
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.parse_failed == 2 and stats.pending == 2
    rows = pending(db)
    assert all(r["reason"] == "llm_parse_failed" for r in rows)
    assert "不要用 Lombok" in rows[0]["raw_content"]
    # 素材落待办了，游标照样推进——否则重跑还是同样失败，永久卡死
    assert db.query_one("SELECT last_log_id FROM extraction_cursor WHERE session_id=%s",
                        (SESSION,))["last_log_id"] == session[-1]["id"]


def test_limit_stops_early_and_leaves_the_cursor_at_the_last_finished_window(db, session,
                                                                            matched):
    """★ A6（W1）：游标推进到**窗口最后一行**（rows[-1].id），而不是窗口锚点。
    超预算桶被切块后多个窗口共享同一个 anchor_id——若按锚点推进，
    尾桶游标永远不动，`_sessions_with_new_material` 会把该会话无限重排（W1 卡死）。"""
    llm = FakeLLM({"items": []})
    with db.transaction() as conn:
        router.route_session(SESSION, conn=conn, llm=llm, limit=1)
    assert llm.call_count == 1
    # window1 = rows 1-2（锚点 row1），处理完推进到 row2
    assert db.query_one("SELECT last_log_id FROM extraction_cursor WHERE session_id=%s",
                        (SESSION,))["last_log_id"] == session[1]["id"]


def test_a_summary_row_is_not_routed(db, session, matched):
    """★ §3.3 降权：实测 112 条压缩摘要没有一条是会话首行，它们覆盖的原始消息
    全都已入库并已路由过，再判一遍只是白烧 30% 的调用（bench_windows 实测）。"""
    db.execute("UPDATE conversation_log SET role='summary' WHERE session_id=%s AND turn_no=1",
               (SESSION,))
    llm = FakeLLM({"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.windows_skipped == 1
    assert stats.windows == 1        # 只剩第二个窗口
    assert llm.call_count == 1


def test_an_over_budget_bucket_splits_windows_and_still_advances_the_cursor(db, session,
                                                                            matched,
                                                                            monkeypatch):
    """★ A6（W1）核心场景：锚点桶超过 TOKEN_BUDGET 被切成多个窗口（共享 anchor），
    游标必须推进到**最后一个窗口的末行**；重跑不得再出现任何待判窗口
    （旧行为 done_through=anchor → 游标卡死 → 会话被无限重排）。"""
    monkeypatch.setattr(router, "TOKEN_BUDGET", 5)   # 足够小：每行都装不进同一个窗口
    llm = FakeLLM({"items": []}, {"items": []}, {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)
    assert stats.windows >= 2, "超预算桶必须真的被切开（否则测了个寂寞）"
    assert db.query_one("SELECT last_log_id FROM extraction_cursor WHERE session_id=%s",
                        (SESSION,))["last_log_id"] == session[-1]["id"]

    # 重跑：游标之后没有任何窗口，不再无限重排
    again = FakeLLM()
    with db.transaction() as conn:
        rerun = router.route_session(SESSION, conn=conn, llm=again)
    assert rerun.windows == 0 and again.call_count == 0


def test_an_llm_error_midway_resumes_from_checkpoints_without_redoing_windows(db, session,
                                                                              matched):
    """★ A5（S4）：自管路径（生产路径）逐窗 checkpoint。第一窗成功后第二窗
    LLMError 中断——重试时第一窗直接从 checkpoint 恢复，不再重付 LLM 调用；
    最终游标推进、checkpoints 清理干净、decisions/pending 不重复。"""
    from app.core.llm import LLMError

    payload_w1 = {"items": [item("observation", "在 gds-agg 项目中禁止引入 Lombok")]}

    class FailOnSecond:
        """窗1 正常返回，窗2 起传输错误——模拟"跑到一半网关挂了"。"""

        def __init__(self):
            self.calls: list[str] = []

        def structured(self, task, *, system, messages, tool, schema, max_tokens=0):
            self.calls.append(messages[0]["content"])
            if len(self.calls) == 1:
                return route_prompt.RouteResult.model_validate(payload_w1)
            raise LLMError("gateway 500")

        @property
        def call_count(self):
            return len(self.calls)

    first = FailOnSecond()
    with pytest.raises(LLMError):
        router.route_session(SESSION, llm=first)          # 窗1 成功落 checkpoint，窗2 炸
    assert first.call_count == 2

    saved = db.query("SELECT first_msg_id FROM route_checkpoints WHERE session_id=%s"
                     " ORDER BY first_msg_id", (SESSION,))
    assert [r["first_msg_id"] for r in saved] == [session[0]["id"]]

    second = FakeLLM({"items": []})                        # 只需要窗2 的载荷
    stats = router.route_session(SESSION, llm=second)
    assert second.call_count == 1, "窗1 必须从 checkpoint 恢复，不再调 LLM"
    assert stats.restored == 1
    assert db.query_one("SELECT last_log_id FROM extraction_cursor WHERE session_id=%s",
                        (SESSION,))["last_log_id"] == session[-1]["id"]
    # 成功收尾后 checkpoint 清干净
    assert db.query_one("SELECT count(*) c FROM route_checkpoints WHERE session_id=%s",
                        (SESSION,))["c"] == 0
    cards = pending(db)
    assert len(cards) == 1 and cards[0]["reason"] == "observation_needs_review"


def test_route_session_manages_its_own_transactions(db, session, matched):
    """★ EXPERT-REVIEW B1：不传 conn 时走三段式自管路径——读+LLM 在事务外，
    写入一个短事务。worker 实际跑的就是这条路；上面的用例全走 conn= 兼容路径，
    测不到自管分支（B1 那个 8 分钟 idle-in-transaction 正是它出的事）。"""
    llm = FakeLLM({"items": [item("observation")]}, {"items": []})
    stats = router.route_session(SESSION, llm=llm)          # 不传 conn
    assert llm.call_count == 2
    assert stats.pending == 1
    assert len(pending(db)) == 1                            # 写事务确实提交了
    assert db.query_one("SELECT last_log_id FROM extraction_cursor WHERE session_id=%s",
                        (SESSION,))["last_log_id"] == session[-1]["id"]


def test_rerunning_the_same_window_does_not_duplicate_the_review_card(db, session, matched):
    """★ EXPERT-REVIEW A3：租约被回收后同一素材被路由两遍，待办卡不能出两套
    （decisions-log #70 承认的缺口，dedup_hash + ON CONFLICT 补上）。"""
    payload = {"items": [item("observation", "在 gds-agg 项目中禁止引入 Lombok")]}
    router.route_session(SESSION, llm=FakeLLM(payload, {"items": []}))
    assert len(pending(db)) == 1

    # 模拟"同一 job 被第二遍执行"：游标退回，同一版 prompt 重判同一批窗口
    db.execute("DELETE FROM extraction_cursor WHERE session_id=%s", (SESSION,))
    stats = router.route_session(SESSION, llm=FakeLLM(payload, {"items": []}))
    assert stats.pending == 1          # 统计口径是"送审次数"：照加，不因撞键缩水
    assert len(pending(db)) == 1       # 但表里只有一行：第二遍被 dedup_hash 挡住


def test_parse_failure_cards_are_idempotent_across_reruns(db, session, matched):
    """parse 失败卡同样带 dedup_hash：重复消费不翻倍，人不用审两张一样的卡。"""
    from app.core.llm import LLMParseError

    def boom():
        return FakeLLM(raises=LLMParseError("schema validation failed", raw={"content": []}))

    router.route_session(SESSION, llm=boom())
    first = len(pending(db))
    assert first == 2

    db.execute("DELETE FROM extraction_cursor WHERE session_id=%s", (SESSION,))
    router.route_session(SESSION, llm=boom())
    assert len(pending(db)) == first


def test_a_prompt_version_bump_issues_a_fresh_review_card(db, session, matched, monkeypatch):
    """★ dedup_hash 含 prompt_ver 的另一面：换版全量重判（§2.3）时同一素材
    必须能出新卡，否则新 prompt 的重判结果会被旧版的幂等键全部挡掉。"""
    payload = {"items": [item("observation", "在 gds-agg 项目中禁止引入 Lombok")]}
    router.route_session(SESSION, llm=FakeLLM(payload, {"items": []}))
    assert len(pending(db)) == 1

    old_ver = route_prompt.PROMPT_VER
    new_ver = old_ver + "-test"
    monkeypatch.setattr(route_prompt, "PROMPT_VER", new_ver)
    # 不用手动退游标：_prepare 检测到 prompt_ver 变化会自动从头重判
    router.route_session(SESSION, llm=FakeLLM(payload, {"items": []}))
    rows = pending(db)
    assert len(rows) == 2
    assert {r["prompt_ver"] for r in rows} == {old_ver, new_ver}


def test_queue_all_sessions_is_idempotent_per_session(monkeypatch):
    """★ 全量回填走队列不走同步 CLI：单次 route ≈53s，259 窗口串行 ≈3.8 小时（C.9）。
    反复排同一个会话不能排出两个 job，否则同一批素材会被抽两遍、排出两套待办卡。"""
    from app.core import db as _db
    from app.queue import jobs

    monkeypatch.setattr(_db, "query", lambda *a, **k: [
        {"session_id": "pytest-route-a"}, {"session_id": "pytest-route-b"}])
    seen = []
    monkeypatch.setattr(jobs, "enqueue",
                        lambda kind, payload, **kw: seen.append((kind, payload, kw)) or 1)

    assert router.queue_all_sessions() == {"sessions": 2, "queued": 2, "already_queued": 0}
    assert [s[0] for s in seen] == ["route", "route"]
    assert [s[2]["dedup_key"] for s in seen] == [
        "route:claude-code:pytest-route-a", "route:claude-code:pytest-route-b"]

    # 第二遍：队列说"已经有活的了"（enqueue 返回 None）⇒ 一个都不再排
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: None)
    assert router.queue_all_sessions() == {"sessions": 2, "queued": 0, "already_queued": 2}


# ───────────────────────── B5-③ 重复白名单（auto_repeat） ─────────────────────────
# 白名单的近邻查询和送审用的同款词袋向量（_hash_vector）就够了：生产上是 bge-m3，
# 测试只验"近邻够了放行 / 有否决不放行 / 数量不够不放行 / 跨分区不算数"的判定逻辑。

DIM = 1024
AUTO_TEXT = "在 gds-agg 项目中，X 使用 Y"          # 6 token
AUTO_SEEDS = ["在 gds-agg 项目中，X 使用 Y 甲",   # 各加一个 token：与 query cos≈0.926
              "在 gds-agg 项目中，X 使用 Y 乙",
              "在 gds-agg 项目中，X 使用 Y 丙"]


def _hash_vector(text: str) -> list[float]:
    import hashlib

    vector = [0.0] * DIM
    for token in text.replace("，", " ").replace("、", " ").split():
        slot = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM
        vector[slot] += 1.0
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


@pytest.fixture
def fake_embed(monkeypatch):
    """_precompute_section_embeddings 在函数内 import embedding 模块，patch 模块属性即可。"""
    from app.core import embedding as emb_mod

    class FakeEmbedder:
        def encode_one(self, text):
            return _hash_vector(text)

    monkeypatch.setattr(emb_mod, "get_embedding_service", lambda: FakeEmbedder())


def seed_obs(db, anchor: int, content: str, *, polarity="positive", project=PROJECT):
    from app.core.hashing import content_hash

    return db.query_one(
        "INSERT INTO raw_observations (org_key, project_key, section_key, content,"
        "                              content_hash, polarity, embedding, source_msg,"
        "                              prompt_ver)"
        " VALUES (%s,%s,'code_style',%s,%s,%s,%s,%s,'pytest')"
        " RETURNING id", (ORG, project, content, content_hash(content), polarity,
                          _hash_vector(content), anchor))["id"]


def test_a_frequent_repeat_auto_passes_without_a_card_for_humans(db, session, matched,
                                                                 fake_embed):
    """★ B5-③：同分区同栏位的正面近邻已有 3 条 → 第 4 次出现直接落库，
    人只看见 'approved' 的已处理卡。resolution={} 保证不加任何人的改判。"""
    anchor = session[0]["id"]
    for content in AUTO_SEEDS:
        seed_obs(db, anchor, content)

    llm = FakeLLM({"items": [item("observation", AUTO_TEXT, confidence=1.0)]},
                  {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)

    assert stats.pending == 0 and stats.auto_approved == 1
    card = pending(db)[0]
    assert card["status"] == "approved"
    assert card["resolved_by"] == "auto_repeat"
    assert card["resolution"] in ({}, None) or card["resolution"] == {}
    assert card["reason"].endswith("+auto_repeat")
    job = db.query_one("SELECT * FROM jobs WHERE kind='apply_decision'"
                       " AND payload->>'pending_id'=%s", (str(card["id"]),))
    assert job is not None and job["payload"]["resolved_by"] == "auto_repeat"
    assert job["payload"]["resolution"] == {}


def test_a_human_veto_nearby_blocks_the_auto_pass(db, session, matched, fake_embed):
    """★ 近邻里有任何人工否决记录（polarity='negative'）→ 永远送人工。
    人否决过的边界比省一次点击值钱——这是白名单唯一的绝对否决条件。"""
    anchor = session[0]["id"]
    for content in AUTO_SEEDS:
        seed_obs(db, anchor, content)
    seed_obs(db, anchor, "在 gds-agg 项目中，X 使用 Y 禁", polarity="negative")

    llm = FakeLLM({"items": [item("observation", AUTO_TEXT, confidence=1.0)]},
                  {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)

    assert stats.auto_approved == 0 and stats.pending == 1
    assert pending(db)[0]["status"] == "pending"


def test_two_prior_neighbors_are_not_enough(db, session, matched, fake_embed):
    """min_seen=3：只见过两次的东西还得人看——第 4 次起才免审，宁多审不小放。"""
    anchor = session[0]["id"]
    for content in AUTO_SEEDS[:2]:
        seed_obs(db, anchor, content)

    llm = FakeLLM({"items": [item("observation", AUTO_TEXT, confidence=1.0)]},
                  {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)

    assert stats.auto_approved == 0 and stats.pending == 1


def test_neighbors_in_another_project_do_not_count(db, session, matched, fake_embed):
    """白名单查询带分区谓词（org+project IS NOT DISTINCT FROM）：
    flight-supplier 里出现过的同类内容，不能替 gds-agg 的卡片免审。"""
    anchor = session[0]["id"]
    for content in AUTO_SEEDS:
        seed_obs(db, anchor, content, project="dida/flight-supplier")

    llm = FakeLLM({"items": [item("observation", AUTO_TEXT, confidence=1.0)]},
                  {"items": []})
    with db.transaction() as conn:
        stats = router.route_session(SESSION, conn=conn, llm=llm)

    assert stats.auto_approved == 0 and stats.pending == 1
