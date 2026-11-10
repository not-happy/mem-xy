"""P1 验收：扫真 transcript → 落库 → 重复扫描不产生新行（§13 P1）。

Gated on MEM_DSN pointing at a migrated database. 写的是真库，所以
session_id 一律带 `pytest-scan-` 前缀、cwd 一律在 `memxy-scan-test/` 下，方便清干净。
"""

import json

import pytest
from conftest import requires_db

from app.ingest.scan import scan_once
from app.ingest.transcripts import SOURCE

pytestmark = requires_db

SESSION = "pytest-scan-0001"
MARKER = "memxy-scan-test"


def _record(uuid, **kw):
    base = {"uuid": uuid, "sessionId": SESSION, "type": "user",
            "timestamp": "2026-10-08T03:30:11.939Z", "gitBranch": "dev"}
    base.update(kw)
    return base


def _user(uuid, content, **kw):
    return _record(uuid, type="user", message={"content": content}, **kw)


def _assistant(uuid, *blocks, **kw):
    return _record(uuid, type="assistant", message={"content": list(blocks)}, **kw)


@pytest.fixture
def tree(tmp_path):
    """一个主会话 + 一个子 agent，目录名带 MARKER 以便清理。"""
    root = tmp_path / MARKER / "projects"
    main = root / "C--x-own-mem-xy"
    main.mkdir(parents=True)

    cwd = str(tmp_path / MARKER / "workdir")
    rows = [
        _user("u1", "不要用 Lombok", cwd=cwd),
        _user("u2", [ {"type": "text", "text": "跑完了，你看下"} ], cwd=cwd),
        _user("u3", "<task-notification>后台任务完成</task-notification>", cwd=cwd),
        _user("u4", "This session is being continued from a previous conversation…", cwd=cwd),
        _user("u5", [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}], cwd=cwd),
        _assistant("a1", {"type": "thinking", "thinking": "用户说不要用 Lombok，那就不引"}, cwd=cwd),
        _assistant("a2", {"type": "thinking", "thinking": "Let me read the file."}, cwd=cwd),
        _assistant("a3", {"type": "text", "text": "结论：不引"}, cwd=cwd),
        _assistant("a4", {"type": "tool_use", "id": "x", "name": "Read", "input": {}}, cwd=cwd),
        {"type": "attachment", "uuid": "x1", "sessionId": SESSION, "cwd": cwd},
    ]
    (main / f"{SESSION}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    sub = main / SESSION / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(
        json.dumps(_user("s1", "只做只读调研，不修改任何文件", cwd=cwd), ensure_ascii=False) + "\n",
        encoding="utf-8")
    return root


@pytest.fixture
def db():
    from app.core import db as _db

    _db.init_pool()
    _cleanup(_db)
    yield _db
    _cleanup(_db)


def _cleanup(_db):
    _db.execute("DELETE FROM conversation_log WHERE source=%s AND session_id LIKE %s",
                (SOURCE, "pytest-scan-%"))
    _db.execute("DELETE FROM workspace_map WHERE cwd LIKE %s", (f"%/{MARKER}/%",))


def test_scan_stores_only_the_filtered_rows(db, tree):
    stats = scan_once(tree)
    assert stats.files == 1                      # 子 agent 不算
    assert stats.records == 10
    assert stats.inserted == 5                   # u1 u2 u4(摘要) a1 a3
    assert stats.roles == {"user": 2, "summary": 1, "thinking": 1, "assistant": 1}
    assert stats.no_key == 0
    assert stats.sessions == {SESSION}

    rows = db.query("SELECT src_uuid, role, turn_no, org_key, project_key, cwd, msg_ts"
                    " FROM conversation_log WHERE session_id=%s ORDER BY id", (SESSION,))
    assert [r["src_uuid"] for r in rows] == ["u1", "u2", "u4", "a1", "a3"]
    # turn_no 是会话内的序号，窗口抽取靠它排序
    assert [r["turn_no"] for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0]["msg_ts"] is not None


def test_rescan_inserts_nothing(db, tree):
    """★ §13 P1 硬断言 ①：重复扫描不产生新行。"""
    first = scan_once(tree)
    second = scan_once(tree)          # 全新缓存 ⇒ 真的重读重插一遍
    assert first.inserted == 5
    assert second.inserted == 0
    assert second.conflicts == 5
    assert db.query_one("SELECT count(*)::int AS n FROM conversation_log"
                        " WHERE session_id=%s", (SESSION,))["n"] == 5


def test_unchanged_cache_skips_the_file_entirely(db, tree):
    cache: dict = {}
    scan_once(tree, unchanged=cache)
    stats = scan_once(tree, unchanged=cache)
    assert stats.files_skipped == 1
    assert stats.records == 0


def test_dry_run_writes_nothing_and_does_not_poison_the_cache(db, tree):
    cache: dict = {}
    dry = scan_once(tree, write=False, unchanged=cache)
    assert dry.candidates == 5
    assert dry.dry_run is True
    assert db.query_one("SELECT count(*)::int AS n FROM conversation_log"
                        " WHERE session_id=%s", (SESSION,))["n"] == 0
    # ★ 空跑要是把 (size, mtime) 写进缓存，紧接着的真扫描会把所有文件跳过
    assert cache == {}
    assert scan_once(tree, unchanged=cache).inserted == 5


def test_rows_which_lack_a_uuid_are_skipped_and_counted(db, tree):
    path = next(tree.glob("*/*.jsonl"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_user("", "没有 uuid", cwd="C:/x"), ensure_ascii=False) + "\n")
    stats = scan_once(tree)
    assert stats.no_key == 1
    assert stats.inserted == 5


def test_subagents_can_be_included_but_are_excluded_by_default(db, tree):
    assert scan_once(tree).candidates == 5          # 只有主会话
    stats = scan_once(tree, include_subagents=True)
    assert stats.files == 2
    # 子 agent 的 user 行是父 agent 写的任务书，纳入后多出一条候选（§3.1）。
    # inserted 只有 1 是因为主会话那 5 行上一句已经入库、被幂等键挡掉了。
    assert stats.candidates == 6
    assert stats.inserted == 1
    assert db.query_one("SELECT count(*)::int AS n FROM conversation_log"
                        " WHERE session_id=%s", (SESSION,))["n"] == 6


def test_unmatched_cwd_falls_to_personal_never_null(db, tree):
    """★ §5.1：NULL 会让可见性谓词无从匹配，那些记忆永远召不回来。"""
    stats = scan_once(tree)
    orgs = {r["org_key"] for r in db.query(
        "SELECT DISTINCT org_key FROM conversation_log WHERE session_id=%s", (SESSION,))}
    assert orgs == {"personal"}
    assert None not in orgs
    # 这条 cwd 不在任何 seed 规则里，所以进 default 桶、等着 UI 让人确认
    assert len(stats.default_cwds) == 1
    assert db.query_one("SELECT resolved_by FROM workspace_map WHERE cwd LIKE %s",
                        (f"%/{MARKER}/%",))["resolved_by"] == "default"


def test_project_key_satisfies_the_check_constraint(db, tree):
    scan_once(tree)
    keys = {r["project_key"] for r in db.query(
        "SELECT DISTINCT project_key FROM conversation_log WHERE session_id=%s", (SESSION,))}
    assert keys == {"personal/workdir"}
