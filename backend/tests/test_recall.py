"""§6.1 可见性 + §6.2 两通道 + §6.4 RRF/加权 + §6.5 稳定层的验收。

Gated on MEM_DSN。★ **不加载 bge-m3**：embedding 打桩成"哈希词袋"，确定性、毫秒级。
这里要验的是"该召回的召回了、不该串台的没串台、两个通道各自起作用了"，不是向量质量。
向量质量归 §10 的评测集管。
"""

import hashlib
import json
import math
import time
from datetime import datetime, timedelta, timezone

import pytest
from conftest import requires_db

from app.retrieve import bootstrap as bootstrap_mod
from app.retrieve import keywords as kw
from app.retrieve import recall as recall_mod

pytestmark = requires_db

DIM = 1024
# ★ 测试专用分区，绝不用真实的 'dida'/'dida/gds-agg'：本文件直接跑在活库上，
#   真实记忆一旦增长（2026-11-02 已有 195 条活跃）就会在同分区里和种子数据竞争
#   top-k——哈希词袋桩向量对真 bge-m3 向量近乎随机排序，库越大用例越会被淹没
#   （当天实际挂掉 8 个）。repo 名保留 gds-agg：下方锁定的词表里有它。
ORG = "pytest-recall-org"
OTHER_ORG = "pytest-other-org"
PROJECT = f"{ORG}/gds-agg"
# ★ 同一个 repo 名，挂在另一个 org 下。§6.1 说的就是这种情况：
#   project 分支不带 org_key 的话，两家公司的 gds-agg 会互相召回。
SAME_NAME_OTHER_ORG = f"{OTHER_ORG}/gds-agg"
SESSION = "pytest-recall"


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
    monkeypatch.setattr(recall_mod, "get_embedding_service", lambda: FakeEmbedder())
    # 词表固定住：真库里有 22 个栏位的 aliases，不锁住的话召回结果会随词表编辑而变。
    # ★ B2 之后哈希词袋的 cos≈0 会被 dense 门槛过滤，下方用例的召回依赖 keywords
    #   通道——这些测试关键词必须进词表，query 侧才抽得出来（生产里路由 LLM 也是
    #   从同一个词表抽的，语义一致）。
    monkeypatch.setattr(kw, "load_vocab",
                        lambda *, force=False: frozenset({
                            "婴儿票", "gds-agg", "dida",
                            "验价", "发布", "后端", "改签", "软删",
                            "排除对照", "审计对照", "续命", "不记日志", "评测不续命",
                            "关键词锚点",
                        }))


@pytest.fixture(autouse=True)
def live_data_firewall(monkeypatch):
    """★ 本文件直接跑在活库上，必须挡住真实数据的增长（2026-11-02 真库涨到 195 条
    活跃记忆，把 8 个用例淹了——桩向量对真 bge-m3 向量近乎随机排序，库越大越挂）：
    ① 专用分区：ORG/PROJECT 不再用 dida，真实 project/org 记忆按 §6.1 天然不可见；
    ② user 级记忆全局可见（§6.1 语义如此，分区管不住），给两条通道 SQL 和稳定层的
       user SQL 追加 pytest 内容前缀过滤。和上面锁词表同一个理由：不让本文件之外的
       数据决定测试结果。"""
    # ★ 用正则不用 LIKE 'pytest-recall-%'：psycopg 会把 SQL 文本里裸的 % 当占位符
    #   解析（"only '%s', '%b', '%t' are allowed"），正则没有这个坑。
    visible = "status = 'active' AND content ~ '^pytest-recall-'"
    for mod, names in ((recall_mod, ("_DENSE_SQL", "_KEYWORDS_SQL")),
                       (bootstrap_mod, ("_USER_SQL",))):
        for name in names:
            sql = getattr(mod, name)
            assert "status = 'active'" in sql, f"{name} 结构变了，防火墙需要跟着改"
            monkeypatch.setattr(mod, name, sql.replace("status = 'active'", visible, 1))


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    # ★ ORG 也要建：living_doc.org_key 有外键指向 orgs，测试专用分区的 org 行
    #   在真库里不存在。memories.org_key 没有外键，但口径保持一致。
    for org in (ORG, OTHER_ORG):
        _db.execute("INSERT INTO orgs (org_key, name) VALUES (%s,'pytest 专用组织')"
                    " ON CONFLICT DO NOTHING", (org,))
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM recall_log WHERE session_id=%s", (SESSION,))
    _db.execute("DELETE FROM memories WHERE content LIKE %s", ("pytest-recall-%",))
    _db.execute("DELETE FROM living_doc WHERE content LIKE %s", ("pytest-recall-%",))
    # ★ 按 src_uuid 前缀删而不是按 SESSION：seed_msg 会造"别的会话"的行，
    #   且必须先删 memories（FK 引用 conversation_log）再删这里。
    #   前缀用参数传（%s）而不是写进 SQL 文本：psycopg 会把裸 % 当占位符解析。
    _db.execute("DELETE FROM conversation_log WHERE source='pytest' AND src_uuid LIKE %s",
                ("pytest-recall-%",))
    # orgs 放最后：living_doc.org_key 的外键要求子表先清。
    _db.execute("DELETE FROM orgs WHERE org_key IN (%s, %s)", (ORG, OTHER_ORG))


_msg_seq = 0


def seed_msg(db, *, session=SESSION, content="pytest-recall-源消息") -> int:
    """一条 conversation_log，供记忆溯源（source_msg → 会话）用。"""
    global _msg_seq
    _msg_seq += 1
    return db.query_one(
        "INSERT INTO conversation_log (source, src_uuid, session_id, role, content, msg_ts)"
        " VALUES ('pytest', %s, %s, 'user', %s, now()) RETURNING id",
        (f"pytest-recall-{_msg_seq}", session, content))["id"]


def seed_memory(db, *, content, scope="project", org=ORG, project=PROJECT, role="fact",
                keywords=(), mention_count=1, age_days=0.0, embedding=None,
                subject=None, source_msg=None) -> int:
    """★ scope='user' 时 org_key 必须是 NULL（§6.1），subject 只能是 'user'（CHECK 约束）。"""
    if scope == "user":
        org, project, subject = None, None, "user"
    vector = embedding if embedding is not None else _hash_vector(content)
    return db.query_one(
        "INSERT INTO memories (scope, org_key, project_key, subject, role, content,"
        "                       content_hash, keywords, embedding, mention_count, last_seen_at,"
        "                       source_msg)"
        " VALUES (%s,%s,%s,%s,%s,%s,md5(%s),%s,%s::vector,%s,%s,%s) RETURNING id",
        (scope, org, project, subject or "project", role, content, content,
         list(keywords), vector, mention_count,
         datetime.now(timezone.utc) - timedelta(days=age_days), source_msg))["id"]


def do_recall(db, query, **overrides):
    params = {"query": query, "cwd": None, "org_key": ORG, "project_key": PROJECT,
              "session_id": SESSION, "write_log": False}
    params.update(overrides)
    return recall_mod.recall(**params)


def ids(result):
    return [h.memory_id for h in result.items]


# ─────────────────────────── §6.1 可见性 ───────────────────────────

def test_a_project_memory_from_another_org_with_the_same_repo_name_is_not_recalled(db):
    """★ §6.1 的双保险：project 分支必须带 org_key。少了它，两家公司都有的
    `gds-agg` 会互相召回——在 tiket repo 里召回 traveloka 的怪癖纯是噪声。"""
    mine = seed_memory(db, content="pytest-recall-本仓库的验价流程", keywords=["验价"])
    theirs = seed_memory(db, content="pytest-recall-别家仓库的验价流程", org=OTHER_ORG,
                         project=SAME_NAME_OTHER_ORG, keywords=["验价"])

    got = ids(do_recall(db, "验价流程怎么走"))
    assert mine in got
    assert theirs not in got


def test_org_level_memories_show_up_in_every_repo_of_that_org(db):
    org_wide = seed_memory(db, content="pytest-recall-全组织通用的发布流程", scope="org",
                           project=None, keywords=["发布"])
    theirs = seed_memory(db, content="pytest-recall-别的组织的发布流程", scope="org",
                         org=OTHER_ORG, project=None, keywords=["发布"])

    got = ids(do_recall(db, "发布流程"))
    assert org_wide in got
    assert theirs not in got


def test_user_scope_memories_follow_you_into_every_org(db):
    personal = seed_memory(db, content="pytest-recall-我是后端工程师", scope="user",
                           role="profile", keywords=["后端"])
    got = ids(do_recall(db, "后端", org_key=OTHER_ORG, project_key=None))
    assert personal in got


def test_deleted_memories_are_never_recalled(db):
    gone = seed_memory(db, content="pytest-recall-已经软删掉的", keywords=["软删"])
    db.execute("UPDATE memories SET status='deleted' WHERE id=%s", (gone,))
    assert gone not in ids(do_recall(db, "软删掉的记忆"))


def test_memories_born_in_this_session_are_not_recalled_back_into_it(db):
    """★ 本会话提取的记忆不回注本会话：内容还在上下文里，回注是纯重复噪声
    （用户裁决 2026-11-02）。别的会话照常召回。"""
    msg = seed_msg(db)                       # 源消息属于 SESSION（do_recall 默认会话）
    mine = seed_memory(db, content="pytest-recall-本会话刚出生的", keywords=["婴儿票"],
                       source_msg=msg)
    old = seed_memory(db, content="pytest-recall-上个会话出生的", keywords=["婴儿票"],
                      source_msg=seed_msg(db, session="pytest-recall-别的会话"))

    got = ids(do_recall(db, "婴儿票"))
    assert mine not in got
    assert old in got


def test_session_born_memory_is_recalled_in_a_different_session(db):
    msg = seed_msg(db, session="pytest-recall-别的会话")
    theirs = seed_memory(db, content="pytest-recall-别的会话出生的", keywords=["婴儿票"],
                         source_msg=msg)
    assert theirs in ids(do_recall(db, "婴儿票"))          # 当前会话 ≠ 来源会话


def test_memories_without_source_msg_are_unaffected_by_session_filter(db):
    """人工录入 / 迁移进来的记忆没有溯源消息，任何会话都能召回。"""
    orphan = seed_memory(db, content="pytest-recall-没有来源消息的", keywords=["婴儿票"])
    assert orphan in ids(do_recall(db, "婴儿票"))


# ─────────────────────────── §6.2 两通道 ───────────────────────────

def test_the_keyword_channel_puts_an_exact_identifier_match_first(db):
    """★ keywords 通道存在的**全部理由**：`bookingFlowService` 这种精确标识符，
    dense 经常糊掉。这里两条记忆的正文几乎一样，只有标识符不同。"""
    target = seed_memory(db, content="pytest-recall-这个服务负责编排",
                         keywords=["bookingFlowService"])
    decoy = seed_memory(db, content="pytest-recall-这个服务也负责编排",
                        keywords=["验价"])

    got = do_recall(db, "bookingFlowService 验价 是干什么的").items
    assert got[0].memory_id == target
    assert "keywords" in got[0].channels
    # ★ decoy 照样会出现：它的 keyword `编排` 也在 query 里（overlap=1），
    #   但 target 是 `bookingFlowService` 完整标识符命中（overlap=1）且正文词袋
    #   更贴近（哈希词袋下两者 dense 都过不了 0.5，RRF 里 kw 名次同为 1）——
    #   平局时 RRF 仍按 overlap/名次稳定排序，断言只锁 target 在前。
    #   这才是 keywords 通道的真实作用：不是过滤，是把精确命中的那条顶到前面。
    by_id = {h.memory_id: h for h in got}
    assert by_id[target].score > by_id[decoy].score


def test_the_keyword_match_is_case_insensitive(db):
    """★ 踩过的坑：路由 LLM 吐的 keywords 是原样标识符（`bookingFlowService`），
    query 侧统一小写化，`&&` 直接对不上 ⇒ 整个 keywords 通道静默失效，
    症状和"库里没有相关记忆"一模一样。"""
    target = seed_memory(db, content="pytest-recall-大小写对照", keywords=["RetrievePnr"])
    hit = next(h for h in do_recall(db, "retrievepnr 报错").items if h.memory_id == target)
    assert hit.channels.get("keywords") == 1


def test_a_pure_chinese_query_still_recalls_through_the_dense_channel(db):
    """纯中文短句抽不出标识符 ⇒ keywords 通道整个不参与，RRF 只剩 dense 一路。
    ★ 这是合法结果，不能为了"凑一个通道"去塞泛用词。
    ★ 哈希词袋只认完全相同的 token（md5 槽）：query 两词必须原样出现在正文里，
      cos(0.7071) 才能过 B2 门槛——这是词袋模拟的真实约束，用例造数要顺着它。"""
    wanted = seed_memory(db, content="pytest-recall-先开 索引 再看 迭代扫描", keywords=[])
    result = do_recall(db, "索引 迭代扫描")
    assert wanted in ids(result)
    hit = next(h for h in result.items if h.memory_id == wanted)
    assert hit.channels.get("dense") is not None
    assert hit.channels.get("keywords") is None


def test_a_memory_hit_by_both_channels_outranks_one_hit_by_only_one(db):
    """RRF 的核心性质：两路都认可 > 一路强烈认可。"""
    both = seed_memory(db, content="pytest-recall-pgvector 的 HNSW 索引要开迭代扫描",
                       keywords=["pgvector", "HNSW"])
    one = seed_memory(db, content="pytest-recall-pgvector 的 HNSW 索引要开迭代扫描 迭代扫描 "
                                  "迭代扫描 迭代扫描", keywords=[])

    got = ids(do_recall(db, "pgvector HNSW 迭代扫描"))
    assert got.index(both) < got.index(one)


def test_the_keyword_pool_ignores_rows_whose_overlap_is_zero(db):
    """`&&` 命中但 overlap 计出来是 0 的行不该占名次（防御性：两处的口径必须一致）。"""
    fused = recall_mod.fuse([], [{"id": 1, "overlap": 0}, {"id": 2, "overlap": 2}], rrf_k=60)
    assert set(fused) == {2}
    assert fused[2]["keyword_rank"] == 1


# ─────────────────────────── §6.4 加权 ───────────────────────────
#
# ★ 这两条直接测 `score()` 这个纯函数，不走 SQL：加权是**乘法链**，
#   经过 RRF 归一之后名次本身就会影响分数，走全链路根本断言不出"正好一半"。


def _row(id_, *, scope="project", role="fact", mention=1, age_days=0.0, content="x"):
    return {"id": id_, "scope": scope, "role": role, "content": content,
            "mention_count": mention,
            "last_seen_at": datetime.now(timezone.utc) - timedelta(days=age_days)}


def test_time_decay_follows_the_role_half_life():
    """fact 半衰期 90 天 ⇒ 90 天前的一条正好掉到一半；profile 365 天 ⇒ 几乎没掉。"""
    now = time.time()
    fused = {1: {"rrf": 1.0, "dense_rank": 1, "keyword_rank": None},
             2: {"rrf": 1.0, "dense_rank": 2, "keyword_rank": None}}
    hits = {h.memory_id: h for h in recall_mod.score(
        [_row(1, role="fact", age_days=0), _row(2, role="fact", age_days=90)], fused, now=now)}
    assert hits[2].score == pytest.approx(hits[1].score * 0.5, abs=1e-3)

    hits = {h.memory_id: h for h in recall_mod.score(
        [_row(1, role="profile", scope="user", age_days=0),
         _row(2, role="profile", scope="user", age_days=90)], fused, now=now)}
    assert hits[2].score == pytest.approx(hits[1].score * 0.844, abs=1e-2)


def test_the_mention_boost_is_capped():
    """★ #14：无上界的 log() 跑一段时间后必然压过时间衰减，
    一条被反复纠正的高频错误记忆会永远排在新事实前面。衰减降权 ≠ 遗忘。"""
    now = time.time()
    fused = {1: {"rrf": 1.0, "dense_rank": 1, "keyword_rank": None},
             2: {"rrf": 1.0, "dense_rank": 2, "keyword_rank": None}}
    hits = {h.memory_id: h for h in recall_mod.score(
        [_row(1, mention=1), _row(2, mention=10_000_000)], fused, now=now)}
    # 1.6 / (1 + 0.15·ln2)：分母是 mention=1 那条**自己**的加成，不是 1.0
    assert hits[2].score / hits[1].score == pytest.approx(
        1.6 / (1 + 0.15 * math.log(2)), abs=1e-3)

    # ★ "封顶"的真正含义：再往上加**一点用都没有**。不封顶的话这一条会赢。
    more = {h.memory_id: h for h in recall_mod.score(
        [_row(1, mention=10_000_000), _row(2, mention=10 ** 12)], fused, now=now)}
    assert more[1].score == more[2].score

    # 封顶之后，400 天前的 fact（半衰期 90 天）再怎么高频也翻不了身
    hits = {h.memory_id: h for h in recall_mod.score(
        [_row(1, mention=1, age_days=0), _row(2, mention=10_000_000, age_days=400)],
        fused, now=now)}
    assert hits[2].score < hits[1].score * 0.1


def test_project_scope_outranks_the_same_content_at_user_scope():
    """越贴近当前工作现场越靠前（§6.4 的 scope 权重 1.0 / 0.85 / 0.7）。"""
    now = time.time()
    fused = {1: {"rrf": 1.0, "dense_rank": 1, "keyword_rank": None},
             2: {"rrf": 1.0, "dense_rank": 2, "keyword_rank": None},
             3: {"rrf": 1.0, "dense_rank": 3, "keyword_rank": None}}
    hits = {h.memory_id: h for h in recall_mod.score(
        [_row(1, scope="project"), _row(2, scope="org"), _row(3, scope="user")], fused, now=now)}
    assert hits[1].score > hits[2].score > hits[3].score


def test_a_tie_is_broken_by_id_so_the_order_does_not_drift():
    """不写这一手，同一批数据两次召回的顺序可能不同（SQL 返回顺序不稳定），
    UI 上看着就像"记忆在飘"。"""
    now = time.time()
    fused = {7: {"rrf": 1.0, "dense_rank": 1, "keyword_rank": None},
             3: {"rrf": 1.0, "dense_rank": 2, "keyword_rank": None}}
    ordered = recall_mod.score([_row(7), _row(3)], fused, now=now)
    assert [h.memory_id for h in ordered] == [3, 7]


def test_exclude_ids_drops_what_this_session_already_injected(db):
    """§6.5：每轮召回要排除本 session 已注入的，否则同一句话会被重复塞进上下文。"""
    a = seed_memory(db, content="pytest-recall-排除对照 A", keywords=["排除对照"])
    b = seed_memory(db, content="pytest-recall-排除对照 B", keywords=["排除对照"])

    got = ids(do_recall(db, "排除对照", k=10, exclude_ids=[a]))
    assert a not in got and b in got


def test_the_query_is_augmented_with_the_project_name(db):
    """★ §6.4：短指令（"修复A2 A3"）单独编码基本召不回东西，带上 project 名就能命中。"""
    text = recall_mod.build_query_text("修复A2 A3", prev_assistant_summary="上一轮改了验价",
                                       project_key="dida/gds-agg")
    assert text == "修复A2 A3\n上一轮改了验价\ndida gds-agg"


def test_the_project_name_augmentation_does_not_leak_fragments_into_the_keywords(db):
    """★ 增广只喂 dense 那一路。`dida/gds-agg` 会被标识符正则切成 `dida`/`gds`/`agg`，
    拿这些碎片去和 `keywords[]` 重叠，等于给每条同项目记忆白送几分，
    把 keywords 通道本来要解决的"精确标识符"能力稀释掉。"""
    result = do_recall(db, "bookingFlowService 报空指针")
    assert "gds" not in result.keywords and "agg" not in result.keywords
    assert "bookingflowservice" in result.keywords
    # 但 dense 那一路照样吃到了增广（project 名进了编码文本）
    assert PROJECT.replace("/", " ") in result.query_text


# ─────────────────────── A2：分段编码 + 加权混合（S1/H2） ───────────────────────

def test_the_query_vector_blends_the_summary_tail_instead_of_truncating_it(db):
    """★ A2-②（S1）：max_seq_length=128 是硬护栏（XLM-R tokenizer 下 128 token
    ≈110-120 个汉字），把 summary 拼在 query 后面喂进去必然整段被右截断——
    增广等于白做。改成 query 段与 tail 段分别编码后 0.75/0.25 加权混合。"""
    query = "验价为什么报超时"
    summary = "上一轮定位到是连接池没归还"
    alone = recall_mod.build_query_vector(query, prev_assistant_summary=None,
                                          project_key=None)
    blended = recall_mod.build_query_vector(query, prev_assistant_summary=summary,
                                            project_key=None)
    assert alone != blended                     # tail 参与了向量
    norm = sum(x * x for x in blended) ** 0.5
    assert abs(norm - 1.0) < 1e-6               # 混合后重新归一化


def test_the_blend_vector_with_no_tail_equals_the_plain_query_vector(db):
    """没有 summary/project 增广时不做多余动作：向量就是 query 本体编码。"""
    query = "bookingFlowService 报空指针怎么查"
    plain = recall_mod.build_query_vector(query, prev_assistant_summary=None,
                                          project_key=None)
    via_fake = recall_mod.build_query_vector(query, prev_assistant_summary="",
                                             project_key="")
    assert plain == via_fake


def test_the_tail_contribution_stays_at_a_quarter_weight(db):
    """tail 只该轻微拉偏方向（0.25 权重），不能反客为主盖过 query 本体。"""
    query = "婴儿票规则是什么"
    summary = "完全无关的一段话"
    q = recall_mod.build_query_vector(query, prev_assistant_summary=None,
                                      project_key=None)
    t = recall_mod.get_embedding_service().encode_one(summary)
    blended = recall_mod.build_query_vector(query, prev_assistant_summary=summary,
                                            project_key=None)
    # 与 q 的余弦应该显著高于与 t 的余弦（0.75 权重 + 归一化后的几何关系）。
    cos_q = sum(a * b for a, b in zip(blended, q))
    cos_t = sum(a * b for a, b in zip(blended, t))
    assert cos_q > cos_t


def test_a_project_name_typed_by_the_user_still_counts_as_a_keyword(db):
    """用户自己在 query 里打了 `gds-agg`，它就该以**完整词**的形式命中——
    靠的是词表（`load_vocab` 收了 org 名和 repo 名），不是靠增广。"""
    assert "gds-agg" in do_recall(db, "gds-agg 的验价流程").keywords


# ─────────────────────────── 审计 ───────────────────────────

def test_recall_writes_an_audit_row_with_ids_but_not_content(db):
    """★ `recall_log` 会在 UI 里长期展示。把记忆正文抄一份进来就是**第二份真相源**，
    内容一改就对不上。所以只记 id + 分数 + 通道名次。"""
    wanted = seed_memory(db, content="pytest-recall-审计对照", keywords=["审计对照"])
    result = do_recall(db, "审计对照", write_log=True, source="pytest")

    row = db.query_one("SELECT * FROM recall_log WHERE id=%s", (result.recall_log_id,))
    assert row["injected_ids"] == [wanted]
    assert row["org_key"] == ORG and row["project_key"] == PROJECT
    assert row["source"] == "pytest" and row["latency_ms"] >= 0
    # ★ 关键断言：hits 里只有 id / score / channels，**没有正文**
    assert all(set(hit) == {"id", "score", "channels"} for hit in row["hits"])
    assert all("pytest-recall-审计对照" not in json.dumps(hit, ensure_ascii=False)
               for hit in row["hits"])


def test_the_eval_runner_can_recall_without_touching_the_audit_log(db):
    before = db.query_one("SELECT count(*) c FROM recall_log")["c"]
    seed_memory(db, content="pytest-recall-不记日志", keywords=["不记日志"])
    result = do_recall(db, "不记日志")            # write_log 默认 False
    assert result.recall_log_id is None
    assert db.query_one("SELECT count(*) c FROM recall_log")["c"] == before


# ─────────────────────────── B3 召回即使用 ───────────────────────────

def test_being_recalled_renews_last_seen_at(db):
    """★ B3：衰减只认 last_seen_at（R1/H3）。一条 12 个月没被写入的记忆，只要真被
    召回注入了，就说明它还活着——不续命的话它会按 2^-4 衰减，MENTION_CAP 补不回。"""
    wanted = seed_memory(db, content="pytest-recall-续命", keywords=["续命"], age_days=365)
    old = db.query_one("SELECT last_seen_at FROM memories WHERE id=%s", (wanted,))["last_seen_at"]

    do_recall(db, "续命", write_log=True, source="pytest")

    fresh = db.query_one("SELECT last_seen_at FROM memories WHERE id=%s", (wanted,))["last_seen_at"]
    assert fresh > old, "真被召回注入的记忆，last_seen_at 必须刷新"
    # updated_at 是"内容被修改"的记号，召回不算修改
    row = db.query_one("SELECT updated_at, last_seen_at FROM memories WHERE id=%s", (wanted,))
    assert row["updated_at"] is None or row["updated_at"] < row["last_seen_at"]


def test_eval_recall_does_not_renew_last_seen(db):
    """评测跑批（write_log=False）不回写：否则基线会人为抬高被测记忆的新鲜度。"""
    wanted = seed_memory(db, content="pytest-recall-评测不续命", keywords=["评测不续命"],
                         age_days=365)
    old = db.query_one("SELECT last_seen_at FROM memories WHERE id=%s", (wanted,))["last_seen_at"]

    do_recall(db, "评测不续命")                   # write_log 默认 False

    fresh = db.query_one("SELECT last_seen_at FROM memories WHERE id=%s", (wanted,))["last_seen_at"]
    assert fresh == old, "eval 路径不得触碰 last_seen_at"


def test_only_the_injected_topk_gets_renewed(db):
    """只有实际注入的 top-K 续命——dense pool 命中但没进 top-K 的不沾光。"""
    strong = seed_memory(db, content="pytest-recall-续命-排前 关键词锚点",
                         keywords=["关键词锚点"], mention_count=50)
    weak = seed_memory(db, content="pytest-recall-续命-排后 关键词锚点",
                       keywords=["关键词锚点"], mention_count=1, age_days=365)
    weak_old = db.query_one("SELECT last_seen_at FROM memories WHERE id=%s", (weak,))["last_seen_at"]

    result = do_recall(db, "关键词锚点", write_log=True, source="pytest", k=1)

    assert ids(result) == [strong], "强记忆必须排第一，weak 落在 top-K 外"
    weak_fresh = db.query_one("SELECT last_seen_at FROM memories WHERE id=%s", (weak,))["last_seen_at"]
    assert weak_fresh == weak_old, "top-K 之外的记忆不续命"


# ─────────────────────────── query 侧关键词 ───────────────────────────

def test_identifiers_are_extracted_and_stopwords_are_dropped():
    vocab = frozenset({"婴儿票", "gds-agg"})
    got = kw.extract("the bookingFlowService should return 婴儿票 info", vocab=vocab)
    assert "bookingflowservice" in got
    assert "婴儿票" in got
    assert "the" not in got and "should" not in got


def test_a_short_identifier_is_not_a_keyword():
    """`{2,}` 之后至少 3 个字符：`id`、`a2` 这种会把 `&&` 变成噪声源。"""
    assert kw.extract("改 id 和 A2", vocab=frozenset()) == []


def test_chinese_terms_only_come_from_the_vocabulary():
    """没有分词器（§6.2 的排除清单），所以中文只能靠词表子串命中。"""
    assert kw.extract("系统不支持婴儿票", vocab=frozenset({"婴儿票"})) == ["婴儿票"]
    assert kw.extract("系统不支持婴儿票", vocab=frozenset()) == []


# ─────────────────────────── §6.5 稳定层 ───────────────────────────

def seed_doc(db, *, section_key, content, authority="confirmed", org=ORG, project=None):
    return db.query_one(
        "INSERT INTO living_doc (org_key, project_key, section_key, content, authority)"
        " VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (org, project, section_key, content, authority))["id"]


def test_draft_living_doc_is_not_injected_because_it_has_not_passed_human_review(db):
    """★ 规矩类 100% 人审这条底线的**最后一环**：draft 还没过 §5.7 的第二道，
    注入了就等于绕过审核。

    ★ 两条放在不同 section：`living_doc_uniq` 是 (org, section, project)，
      同一个栏位只能有一行——那本来就是"一个栏位一份成文规范"的意思。"""
    seed_doc(db, section_key="code_style", content="pytest-recall-已确认的规矩",
             authority="confirmed")
    seed_doc(db, section_key="dependency", content="pytest-recall-还是草稿的规矩",
             authority="draft")
    seed_doc(db, section_key="layering", content="pytest-recall-钉死的规矩",
             authority="pinned")

    boot = bootstrap_mod.build(org_key=ORG, project_key=PROJECT)
    assert "pytest-recall-已确认的规矩" in boot.markdown
    assert "pytest-recall-还是草稿的规矩" not in boot.markdown
    assert "【钉死】pytest-recall-钉死的规矩" in boot.markdown


def test_org_level_and_repo_specific_rules_are_both_injected_and_labelled(db):
    seed_doc(db, section_key="code_style", content="pytest-recall-全组织通用")
    seed_doc(db, section_key="code_style", content="pytest-recall-只在这个仓库成立",
             project=PROJECT)
    seed_doc(db, section_key="code_style", content="pytest-recall-别家仓库的", org=OTHER_ORG)

    boot = bootstrap_mod.build(org_key=ORG, project_key=PROJECT)
    assert "pytest-recall-全组织通用" in boot.markdown
    assert "pytest-recall-只在这个仓库成立（本仓库专属）" in boot.markdown
    assert "pytest-recall-别家仓库的" not in boot.markdown
    assert boot.memory_count == 2


def test_a_personal_preference_is_not_part_of_the_stable_layer(db):
    """★ `preference` 是**情境性**的（"在这个项目里坚持用 pytest"），
    无条件注入到每个会话就是噪声，要靠每轮召回按 query 带出来。"""
    seed_memory(db, content="pytest-recall-我是后端工程师", scope="user", role="profile")
    seed_memory(db, content="pytest-recall-在这个项目里坚持用 pytest", scope="user",
                role="preference")

    boot = bootstrap_mod.build(org_key=ORG, project_key=PROJECT)
    assert "我是后端工程师" in boot.markdown
    assert "坚持用 pytest" not in boot.markdown


def test_an_empty_library_is_a_legal_bootstrap(db):
    """P5 才有汇总器，所以 living_doc 长期为空是常态。空 markdown 也要能返回，
    hook 那边照样落本地快照缓存（§4.3.3）。"""
    boot = bootstrap_mod.build(org_key="pytest-nothing-here", project_key=None)
    assert boot.markdown == "" and boot.memory_count == 0
    assert boot.cache_key == "bootstrap-pytest-nothing-here-none"


def test_the_cache_key_never_contains_a_path_separator(db):
    assert "/" not in bootstrap_mod.cache_key(ORG, PROJECT)
