"""`app/attribution.py` 服务层的验收（连真库，不加载模型——归属是纯 SQL）。

★ 要验的正是那几段真 SQL：`set_workspace` 的 upsert + `resolved_by='human'`、
  **回溯改判**（`memories.source_msg → conversation_log.cwd` 那条链路）、规则的增改删。
  `test_api_p6.py` 打桩了服务层，抓不到这些。
"""

import pytest
from conftest import requires_db

from app import attribution
from app.library import LibraryError

pytestmark = requires_db

MARK = "pytest-attr-"
CWD = "C:/pytest/attr-repo"
OTHER_CWD = "C:/pytest/other-repo"
ORG = "dida"
NEW_ORG = "personal"
SESSION = f"{MARK}session"


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM memories WHERE content LIKE %s", (MARK + "%",))
    _db.execute("DELETE FROM conversation_log WHERE session_id LIKE %s", (MARK + "%",))
    _db.execute("DELETE FROM workspace_map WHERE cwd LIKE %s OR cwd IN (%s,%s)",
                (MARK + "%", CWD, OTHER_CWD))
    _db.execute("DELETE FROM org_match_rules WHERE pattern LIKE %s", (MARK + "%",))


def seed_convo(db, cwd, *, session=SESSION) -> int:
    return db.query_one(
        "INSERT INTO conversation_log (source, src_uuid, session_id, cwd, role, content)"
        " VALUES ('claude', %s, %s, %s, 'user', %s) RETURNING id",
        (f"{MARK}{cwd}", session, cwd, f"{MARK}原始消息"))["id"]


def seed_memory(db, source_msg, *, org=ORG, project="dida/attr-repo",
                content=None) -> int:
    return db.query_one(
        "INSERT INTO memories (scope, org_key, project_key, subject, role, content,"
        "                       content_hash, source_msg)"
        " VALUES ('project',%s,%s,'project','fact',%s,md5(%s),%s) RETURNING id",
        (org, project, content or f"{MARK}记忆", content or f"{MARK}记忆", source_msg))["id"]


# ─────────────────────────── overview ───────────────────────────

def test_overview_returns_rules_workspaces_and_source_counts(db):
    db.execute("INSERT INTO workspace_map (cwd, org_key, project_key, resolved_by)"
               " VALUES (%s,%s,%s,'default')", (CWD, "personal", "personal/attr-repo"))
    out = attribution.overview()
    assert "rules" in out and "workspaces" in out and "orgs" in out
    assert out["by_source"].get("default", 0) >= 1
    assert any(w["cwd"] == CWD for w in out["workspaces"])


def test_overview_filters_by_resolved_by(db):
    db.execute("INSERT INTO workspace_map (cwd, org_key, project_key, resolved_by)"
               " VALUES (%s,%s,%s,'default'), (%s,%s,%s,'rule')",
               (CWD, "personal", "personal/attr-repo", OTHER_CWD, ORG, "dida/other"))
    out = attribution.overview(resolved_by="default")
    cwds = {w["cwd"] for w in out["workspaces"]}
    assert CWD in cwds and OTHER_CWD not in cwds


# ─────────────────────────── set_workspace ───────────────────────────

def test_set_workspace_marks_it_human(db):
    res = attribution.set_workspace(CWD, org_key=ORG, project_key="dida/attr-repo")
    assert res["resolved_by"] == "human"
    row = db.query_one("SELECT * FROM workspace_map WHERE cwd=%s", (CWD,))
    assert row["resolved_by"] == "human" and row["org_key"] == ORG


def test_set_workspace_retroactively_reassigns_memories(db):
    """★ 核心：目录改判后，源自这个 cwd 的已入库记忆要跟着改 org_key/project_key。"""
    msg = seed_convo(db, CWD)
    m1 = seed_memory(db, msg, org=ORG, project="dida/attr-repo")
    res = attribution.set_workspace(CWD, org_key=NEW_ORG, project_key="personal/attr-repo")
    assert res["memories_moved"] == 1
    row = db.query_one("SELECT org_key, project_key FROM memories WHERE id=%s", (m1,))
    assert row["org_key"] == NEW_ORG and row["project_key"] == "personal/attr-repo"


def test_retroactive_leaves_other_cwd_memories_alone(db):
    """改判 CWD 不能动到别的目录抽出来的记忆。"""
    msg_here = seed_convo(db, CWD)
    msg_there = seed_convo(db, OTHER_CWD, session=f"{MARK}other")
    mine = seed_memory(db, msg_here, org=ORG, project="dida/attr-repo")
    theirs = seed_memory(db, msg_there, org=ORG, project="dida/other-repo",
                         content=f"{MARK}别处的记忆")
    attribution.set_workspace(CWD, org_key=NEW_ORG, project_key="personal/attr-repo")
    assert db.query_one("SELECT org_key FROM memories WHERE id=%s", (mine,))["org_key"] == NEW_ORG
    assert db.query_one("SELECT org_key FROM memories WHERE id=%s", (theirs,))["org_key"] == ORG


def test_retroactive_without_project_key_only_moves_org(db):
    """org 级归属（project_key=None）：只改 org_key，保留原 project_key。"""
    msg = seed_convo(db, CWD)
    m = seed_memory(db, msg, org=ORG, project="dida/attr-repo")
    attribution.set_workspace(CWD, org_key=NEW_ORG, project_key=None)
    row = db.query_one("SELECT org_key, project_key FROM memories WHERE id=%s", (m,))
    assert row["org_key"] == NEW_ORG and row["project_key"] == "dida/attr-repo"


def test_retroactive_false_does_not_touch_memories(db):
    msg = seed_convo(db, CWD)
    m = seed_memory(db, msg, org=ORG, project="dida/attr-repo")
    res = attribution.set_workspace(CWD, org_key=NEW_ORG, project_key="personal/x",
                                    retroactive=False)
    assert res["memories_moved"] == 0
    assert db.query_one("SELECT org_key FROM memories WHERE id=%s", (m,))["org_key"] == ORG


def test_set_workspace_is_idempotent_on_second_move(db):
    """已经是目标分区的记忆不该被重复计入 moved（IS DISTINCT FROM 保护）。"""
    msg = seed_convo(db, CWD)
    seed_memory(db, msg, org=NEW_ORG, project="personal/attr-repo")
    res = attribution.set_workspace(CWD, org_key=NEW_ORG, project_key="personal/attr-repo")
    assert res["memories_moved"] == 0


def test_set_workspace_rejects_empty_org(db):
    with pytest.raises(LibraryError) as exc:
        attribution.set_workspace(CWD, org_key="  ")
    assert exc.value.status == 400 and exc.value.code == "bad_org"


def test_set_workspace_rejects_empty_cwd(db):
    with pytest.raises(LibraryError) as exc:
        attribution.set_workspace("", org_key=ORG)
    assert exc.value.status == 400 and exc.value.code == "bad_cwd"


def test_a_human_row_is_not_downgraded_by_a_second_default_persist(db):
    """§5.1 边界：人工定过的目录，Resolver 的自动写回不能覆盖它。"""
    attribution.set_workspace(CWD, org_key=ORG, project_key="dida/attr-repo")
    from app.ingest.attribution import Attribution, Resolver
    Resolver._persist(CWD, Attribution("personal", "personal/attr-repo", "default"))
    row = db.query_one("SELECT org_key, resolved_by FROM workspace_map WHERE cwd=%s", (CWD,))
    assert row["resolved_by"] == "human" and row["org_key"] == ORG


# ─────────────────────────── rules ───────────────────────────

def test_upsert_rule_creates_then_updates(db):
    created = attribution.upsert_rule(pattern_type="cwd_prefix", pattern=f"{MARK}C:/new/",
                                      org_key="personal", priority=80)
    assert created["id"] and created["pattern_type"] == "cwd_prefix"
    updated = attribution.upsert_rule(rule_id=created["id"], pattern_type="cwd_prefix",
                                      pattern=f"{MARK}C:/new/", org_key=ORG, priority=10)
    assert updated["id"] == created["id"] and updated["org_key"] == ORG
    assert updated["priority"] == 10


def test_upsert_rule_rejects_bad_pattern_type(db):
    with pytest.raises(LibraryError) as exc:
        attribution.upsert_rule(pattern_type="nonsense", pattern=f"{MARK}x", org_key=ORG)
    assert exc.value.status == 400 and exc.value.code == "bad_pattern_type"


def test_delete_rule_removes_it(db):
    created = attribution.upsert_rule(pattern_type="cwd_prefix", pattern=f"{MARK}C:/del/",
                                      org_key=ORG)
    assert attribution.delete_rule(created["id"]) == {"deleted": created["id"]}
    assert db.query_one("SELECT 1 FROM org_match_rules WHERE id=%s", (created["id"],)) is None


def test_delete_a_missing_rule_raises_not_found(db):
    with pytest.raises(LibraryError) as exc:
        attribution.delete_rule(999_999_999)
    assert exc.value.status == 404


def test_updating_a_missing_rule_raises_not_found(db):
    with pytest.raises(LibraryError) as exc:
        attribution.upsert_rule(rule_id=999_999_999, pattern_type="cwd_prefix",
                                pattern=f"{MARK}x", org_key=ORG)
    assert exc.value.status == 404


# ─────────────────────────── B7/W7：规则变更清缓存 ───────────────────────────

def _seed_cache_rows(db):
    """造三行缓存：rule 判的、default 兜底的、human 人工定的。"""
    db.execute("INSERT INTO workspace_map (cwd, org_key, project_key, resolved_by)"
               " VALUES (%s,%s,'dida/attr-repo','rule')", (CWD, ORG))
    db.execute("INSERT INTO workspace_map (cwd, org_key, project_key, resolved_by)"
               " VALUES (%s,'personal',NULL,'default')", (OTHER_CWD,))
    db.execute("INSERT INTO workspace_map (cwd, org_key, project_key, resolved_by)"
               " VALUES (%s,'dida','dida/kept','human')", (MARK + "C:/human-only/",))


def _cache_cwds(db) -> set:
    return {r["cwd"] for r in db.query("SELECT cwd FROM workspace_map")}


def test_upsert_rule_clears_rule_and_default_cache_but_keeps_human(db):
    _seed_cache_rows(db)
    attribution.upsert_rule(pattern_type="cwd_prefix", pattern=f"{MARK}C:/new/",
                            org_key=ORG, priority=80)
    # rule/default 两行被清（下次扫描按新规则重判），human 行保留
    assert _cache_cwds(db) == {MARK + "C:/human-only/"}


def test_delete_rule_clears_rule_and_default_cache_but_keeps_human(db):
    created = attribution.upsert_rule(pattern_type="cwd_prefix", pattern=f"{MARK}C:/del/",
                                      org_key=ORG)
    # upsert 已清过一轮，再补三行验证 delete 的行为
    _seed_cache_rows(db)
    attribution.delete_rule(created["id"])
    assert _cache_cwds(db) == {MARK + "C:/human-only/"}


def test_updating_a_missing_rule_does_not_touch_the_cache(db):
    _seed_cache_rows(db)
    with pytest.raises(LibraryError):
        attribution.upsert_rule(rule_id=999_999_999, pattern_type="cwd_prefix",
                                pattern=f"{MARK}x", org_key=ORG)
    assert len(_cache_cwds(db)) == 3  # 失败路径不清缓存
