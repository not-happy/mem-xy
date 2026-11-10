"""记忆库写入 + 纠错闭环（§7.1 `/memories*`、§7.2 `memory_note`/`memory_correct`）。

Gated on MEM_DSN。★ **不加载 bge-m3**：embedding 打桩成哈希词袋。
这里要验的是"库的约束有没有在入库前被挡住""每次变更有没有留下轨迹""纠错有没有
误写否决记录"，不是向量质量。
"""

import hashlib

import pytest
from conftest import requires_db

from app import library
from app.core import redact

pytestmark = requires_db

DIM = 1024
MARK = "pytest-lib-"          # 所有测试数据都带这个前缀，cleanup 靠它
# ★ 测试专用分区（同 test_recall.py 的 live_data_firewall）：本文件跑在活库上，
#   真库 dida/gds-agg 分区的真实记忆会和种子数据竞争列表 total 与召回 top-k。
ORG = "pytest-lib-org"
OTHER_ORG = "pytest-other-org"
PROJECT = f"{ORG}/gds-agg"
OTHER_PROJECT = f"{OTHER_ORG}/gds-agg"


class FakeEmbedder:
    ready = True
    loading = False

    def encode_one(self, text):
        return _hash_vector(text)

    def encode(self, texts, *a, **k):
        return [_hash_vector(t) for t in texts]


def _hash_vector(text: str) -> list[float]:
    vector = [0.0] * DIM
    for token in text.replace("，", " ").replace("、", " ").split():
        slot = int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM
        vector[slot] += 1.0
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    from app.retrieve import keywords as kw
    from app.retrieve import recall as recall_mod

    fake = FakeEmbedder()
    monkeypatch.setattr(library, "get_embedding_service", lambda: fake)
    # ★ recall 那边也要换：只换 library 的话，"改完还能召回来"那条端到端测试会去
    #   真加载 bge-m3（冷启动 30s+），而这里要验的是向量跟着正文走了，不是向量质量。
    monkeypatch.setattr(recall_mod, "get_embedding_service", lambda: fake)
    monkeypatch.setattr(kw, "load_vocab",
                        lambda *, force=False: frozenset({"婴儿票", "人工出票", "gds-agg"}))


@pytest.fixture(autouse=True)
def live_data_firewall(monkeypatch):
    """★ 同 test_recall.py：专用分区挡掉真实 project/org 记忆，但 user 级记忆
    全局可见（§6.1 语义如此）——库里已有 6 条真实 user 级记忆，召回端到端用例
    k=3 时它们能和种子记忆竞争席位。给两通道 SQL 追加 pytest 内容前缀过滤。
    用正则不用 LIKE：psycopg 会把 SQL 文本里裸的 % 当占位符解析。"""
    from app.retrieve import recall as recall_mod

    visible = f"status = 'active' AND content ~ '^{MARK}'"
    for name in ("_DENSE_SQL", "_KEYWORDS_SQL"):
        sql = getattr(recall_mod, name)
        assert "status = 'active'" in sql, f"{name} 结构变了，防火墙需要跟着改"
        monkeypatch.setattr(recall_mod, name, sql.replace("status = 'active'", visible, 1))


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    _db.execute("INSERT INTO orgs (org_key, name) VALUES (%s,'pytest 对照组织')"
                " ON CONFLICT DO NOTHING", (OTHER_ORG,))
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    ids = [r["id"] for r in _db.query(
        "SELECT id FROM memories WHERE content LIKE %s", (MARK + "%",))]
    if ids:
        _db.execute("DELETE FROM memory_events WHERE memory_id = ANY(%s)", (ids,))
        _db.execute("DELETE FROM memory_feedback WHERE memory_id = ANY(%s)", (ids,))
        _db.execute("DELETE FROM memories WHERE id = ANY(%s)", (ids,))


def make(content="默认内容", **kw) -> dict:
    kw.setdefault("org_key", ORG)
    kw.setdefault("project_key", PROJECT)
    return library.create_memory(content=MARK + content, **kw)


def events(db, memory_id) -> list[dict]:
    return db.query("SELECT action, old_content, new_content, actor FROM memory_events"
                    " WHERE memory_id=%s ORDER BY id", (memory_id,))


def raised(fn, *a, **k):
    with pytest.raises(library.LibraryError) as exc:
        fn(*a, **k)
    return exc.value


# ─────────────────── scope 的三条 CHECK 必须在入库前挡住 ───────────────────

def test_a_user_scope_memory_carries_no_partition(db):
    """★ scope='user' 是穿透的：带着 org/project 就等于把它锁死在一个分区里，
    换个仓库就召不回来了（§6.1）。"""
    row = make("用户级偏好", scope="user", cwd="C:/whatever")
    assert (row["org_key"], row["project_key"], row["subject"]) == (None, None, "user")


def test_user_scope_rejects_a_project_subject(db):
    exc = raised(make, "非法组合", scope="user", subject="project")
    assert (exc.status, exc.code) == (400, "bad_subject")


def test_project_scope_needs_both_keys(db):
    """★ cwd 判不出 project 时就得报错，不能悄悄退化成 org 级——那是把一条
    项目规矩泄漏到整个组织的通道。"""
    exc = raised(library.create_memory, content=MARK + "缺 project", scope="project",
                 org_key=ORG, project_key=None, cwd=None)
    assert (exc.status, exc.code) == (400, "missing_project")


def test_org_scope_needs_an_org(db):
    """cwd 判不出来时落到 `personal` 默认桶——不是报错。★ 但绝不能悄悄把 project_key
    也带上，否则一条"个人级"的规矩会被钉死在某个 repo 上。"""
    row = library.create_memory(content=MARK + "没有 cwd", scope="org", cwd=None)
    assert row["org_key"] == "personal" and row["project_key"] is None


def test_org_scope_drops_the_project_key(db):
    row = make("组织级规范", scope="org", project_key=PROJECT)
    assert row["scope"] == "org" and row["project_key"] is None and row["org_key"] == ORG


def test_a_bad_role_is_rejected_before_any_embedding_is_computed(db):
    exc = raised(make, "角色写错了", role="rule")
    assert (exc.status, exc.code) == (400, "bad_role")


def test_empty_content_is_rejected(db):
    """★ 空正文进库的话，`kw.extract` 给不出关键词、向量是全零，
    这条记忆永远召不回来，也不会有任何指标告诉你它丢了。"""
    for blank in ("", "   ", "\n\t"):
        assert raised(library.create_memory, content=blank, org_key=ORG,
                      project_key=PROJECT).code == "empty_content"


# ─────────────────── 新增：embedding、关键词、轨迹、幂等 ───────────────────

def test_a_new_memory_gets_an_embedding_and_auto_keywords(db):
    row = make("Tiket 改签要带 returnAncillary")
    stored = db.query_one("SELECT embedding, keywords, content_hash, prompt_ver"
                          " FROM memories WHERE id=%s", (row["id"],))
    assert stored["embedding"] is not None
    assert "returnancillary" in stored["keywords"]     # kw.extract 统一小写
    assert stored["prompt_ver"] == "manual"
    assert stored["content_hash"]


def test_a_manual_note_leaves_an_add_event_with_the_actor(db):
    row = make("手工记一条", actor="model")
    assert events(db, row["id"]) == [
        {"action": "ADD", "old_content": None,
         "new_content": MARK + "手工记一条", "actor": "model"}]


def test_the_same_content_in_the_same_bucket_is_a_409(db):
    """★ 手工新增没有 source_msg，而 `UNIQUE(source_msg, content_hash)` 里的 NULL 互不相等
    ⇒ 数据库层面拦不住重复，只能自己判。"""
    first = make("重复内容")
    exc = raised(make, "重复内容")
    assert (exc.status, exc.code) == (409, "duplicate")
    assert str(first["id"]) in exc.message


def test_the_same_content_in_another_bucket_is_allowed(db):
    make("跨桶同内容")
    row = library.create_memory(content=MARK + "跨桶同内容", scope="org", org_key=ORG)
    assert row["org_key"] == ORG and row["project_key"] is None


# ─────────────────── 改：content 必须重算 embedding ───────────────────

def test_editing_the_content_recomputes_the_embedding_and_bumps_the_revision(db):
    row = make("改之前的内容")
    before = db.query_one("SELECT embedding, revision_count, content_hash FROM memories"
                          " WHERE id=%s", (row["id"],))
    updated = library.update_memory(row["id"], content=MARK + "改之后的内容")
    after = db.query_one("SELECT embedding, revision_count, content_hash FROM memories"
                         " WHERE id=%s", (row["id"],))

    assert updated["content"] == MARK + "改之后的内容"
    assert after["embedding"] != before["embedding"], "改了正文却没重算向量 = 召回会对不上"
    assert after["revision_count"] == before["revision_count"] + 1
    assert after["content_hash"] != before["content_hash"]
    assert events(db, row["id"])[-1]["action"] == "UPDATE"


def test_editing_only_the_role_does_not_bump_the_revision(db):
    row = make("只改角色")
    updated = library.update_memory(row["id"], role="pointer")
    assert updated["role"] == "pointer"
    assert updated["revision_count"] == row["revision_count"]


def test_changing_scope_moves_the_memory_to_another_partition(db):
    """换桶 = 换检索分区。org/project 不跟着重算就会留下 §6.1 要防的串台。"""
    row = make("要升到组织级", scope="project")
    updated = library.update_memory(row["id"], scope="org")
    assert (updated["scope"], updated["project_key"]) == ("org", None)


def test_a_newly_edited_memory_is_recallable_by_its_new_content(db):
    """端到端确认向量真的跟着走了：用新正文里的词能召回到它。"""
    from app.retrieve import recall as recall_mod

    row = make("旧说法：婴儿票不支持")
    library.update_memory(row["id"], content=MARK + "新说法：婴儿票要人工出票")
    result = recall_mod.recall(query="婴儿票 人工出票", org_key=ORG, project_key=PROJECT,
                               session_id="pytest-lib-recall", k=3, write_log=False)
    assert row["id"] in [h.memory_id for h in result.items]


# ─────────────────── 删 / 恢复 ───────────────────

def test_delete_is_soft_and_leaves_a_trail(db):
    row = make("要删掉")
    deleted = library.delete_memory(row["id"], note="这条是错的")
    assert deleted["status"] == "deleted"
    assert db.query_one("SELECT count(*) n FROM memories WHERE id=%s", (row["id"],))["n"] == 1
    assert events(db, row["id"])[-1]["action"] == "DELETE"


def test_a_deleted_memory_is_not_listed_by_default(db):
    row = make("删掉的别出现在列表里")
    library.delete_memory(row["id"])
    ids = [m["id"] for m in library.list_memories(org_key=ORG, project_key=PROJECT)["items"]]
    assert row["id"] not in ids
    assert row["id"] in [m["id"] for m in
                         library.list_memories(org_key=ORG, project_key=PROJECT,
                                               status="deleted")["items"]]


def test_restore_undoes_a_soft_delete(db):
    row = make("删错了要恢复")
    library.delete_memory(row["id"])
    restored = library.delete_memory(row["id"], restore=True)
    assert restored["status"] == "active"
    assert events(db, row["id"])[-1]["action"] == "RESTORE"


def test_deleting_twice_is_a_409_not_a_silent_noop(db):
    row = make("删两次")
    library.delete_memory(row["id"])
    assert raised(library.delete_memory, row["id"]).code == "already_deleted"


def test_touching_a_missing_memory_is_a_404(db):
    assert raised(library.get_memory, 999_999_999).status == 404
    assert raised(library.update_memory, 999_999_999, content=MARK + "x").status == 404
    assert raised(library.add_feedback, 999_999_999, kind="useful").status == 404


# ─────────────────── 纠错闭环 ───────────────────

def test_correcting_with_a_replacement_updates_in_place(db):
    row = make("原来说错了")
    out = library.correct_memory(row["id"], correction=MARK + "其实是这样", note="人工纠正")

    assert out["action"] == "updated" and out["status"] == "active"
    assert db.query_one("SELECT content FROM memories WHERE id=%s",
                        (row["id"],))["content"] == MARK + "其实是这样"
    assert db.query("SELECT kind FROM memory_feedback WHERE memory_id=%s", (row["id"],)) \
        == [{"kind": "corrected"}]


def test_correcting_without_a_replacement_soft_deletes(db):
    row = make("整条都是错的")
    out = library.correct_memory(row["id"], correction=None, note="不存在这回事")

    assert out["action"] == "deleted" and out["status"] == "deleted"
    assert db.query("SELECT kind, note FROM memory_feedback WHERE memory_id=%s", (row["id"],)) \
        == [{"kind": "corrected", "note": "不存在这回事"}]


def test_a_blank_correction_counts_as_no_replacement(db):
    row = make("空白纠正")
    assert library.correct_memory(row["id"], correction="   ")["action"] == "deleted"


def test_correction_records_the_channel_in_the_actor(db):
    """UI 改的是 'human'，MCP 里模型改的是 'model'。两者都是人的决定，
    但审计上要能分出是哪条路进来的。"""
    a = make("从界面改")
    b = make("从 MCP 改")
    library.correct_memory(a["id"], correction=MARK + "x", actor="human")
    library.correct_memory(b["id"], correction=MARK + "y", actor="model")
    assert events(db, a["id"])[-1]["actor"] == "human"
    assert events(db, b["id"])[-1]["actor"] == "model"


def test_correcting_never_writes_the_veto_pool(db):
    """★ 这条钉的是 #77：§7.2 原文要求 `memory_correct` 写 negative observation，
    那是接错了线——否决记录只拦 observation 入池（§4.3.2 比对②），拦不住一条事实被重新
    写进 memories，反而会误伤同栏位以后所有正经的规矩提案（#67 那个婴儿票事故）。"""
    before = db.query_one("SELECT count(*) n FROM raw_observations"
                          " WHERE polarity='negative'")["n"]
    row = make("纠错不许碰否决记录")
    library.correct_memory(row["id"], correction=MARK + "改成对的")
    library.correct_memory(make("另一条也纠错")["id"], correction=None)
    after = db.query_one("SELECT count(*) n FROM raw_observations"
                         " WHERE polarity='negative'")["n"]
    assert after == before


def test_a_bad_feedback_kind_is_rejected(db):
    row = make("反馈类型写错")
    assert raised(library.add_feedback, row["id"], kind="thumbs_up").code == "bad_kind"


# ─────────────────── 列表与可见性 ───────────────────

def test_the_default_listing_is_clipped_to_what_you_may_see(db):
    """★ §6.1：同一个 repo 名挂在两个 org 下，列表页也不能互相看见。"""
    mine = make("本组织的项目记忆")
    theirs = library.create_memory(content=MARK + "别组织的项目记忆", scope="project",
                                   org_key=OTHER_ORG, project_key=OTHER_PROJECT)
    user_level = make("用户级记忆", scope="user")

    ids = [m["id"] for m in library.list_memories(org_key=ORG, project_key=PROJECT)["items"]]
    assert mine["id"] in ids and user_level["id"] in ids
    assert theirs["id"] not in ids


def test_all_scopes_is_the_browse_everything_escape_hatch(db):
    theirs = library.create_memory(content=MARK + "全库浏览能看到", scope="project",
                                   org_key=OTHER_ORG, project_key=OTHER_PROJECT)
    ids = [m["id"] for m in library.list_memories(include_all_scopes=True)["items"]]
    assert theirs["id"] in ids


def test_the_listing_never_returns_embeddings(db):
    make("别把向量塞进列表")
    row = library.list_memories(org_key=ORG, project_key=PROJECT)["items"][0]
    assert "embedding" not in row


def test_the_q_filter_matches_content_and_keywords(db):
    make("关键词粗筛 returnAncillary")
    hits = library.list_memories(org_key=ORG, project_key=PROJECT, q="returnancillary")
    assert hits["total"] == 1
    assert library.list_memories(org_key=ORG, project_key=PROJECT, q="压根没有")["total"] == 0


def test_pagination_reports_the_total_separately_from_the_page(db):
    for i in range(5):
        make(f"分页 {i}")
    # ★ scope='project' 是隔离的一部分：list_memories 默认可见性含 user 级穿透，
    #   真库的 user 级记忆会混进 total。专用分区挡得住 org/project 记忆，挡不住穿透。
    page = library.list_memories(org_key=ORG, project_key=PROJECT, scope="project",
                                 limit=2, offset=0)
    assert page["total"] == 5 and len(page["items"]) == 2
    assert library.list_memories(org_key=ORG, project_key=PROJECT, scope="project",
                                 limit=2, offset=4)["total"] == 5


def test_the_events_endpoint_404s_instead_of_returning_an_empty_list(db):
    """空列表会被读成"这条记忆没有历史"，而真相是"根本没这条记忆"。"""
    assert raised(library.list_events, 999_999_999).status == 404


# ─────────────────── 待办列表 ───────────────────

def test_pending_defaults_to_the_two_states_that_need_a_human(db):
    """`failed` 是"批过了但落库炸了"，比 pending 更该被看见，而分组计数看不出这一点。"""
    from app.pipeline import review

    listing = review.list_pending(limit=200)
    assert {i["status"] for i in listing["items"]} <= {"pending", "failed"}
    assert "by_status" in listing and listing["total"] >= len(listing["items"])


def test_pending_redacts_last_error_before_it_reaches_the_ui(db):
    """§2.1：`last_error` 会在 UI 里显示，而 httpx 的异常字符串常带请求头。"""
    leaky = "HTTPError: 401 for http://gw/x with header Authorization: Bearer abc123def456"
    assert "abc123def456" not in (redact.redact(leaky) or "")
