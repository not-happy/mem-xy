"""`app/living_doc.py` 服务层的验收（连真库，不加载模型——living_doc 没有 embedding 列）。

★ 这一层是纯 SQL，所以要验的正是 SQL 本身：占位符不能混用位置(%s)和命名(%(name)s)、
  edit/set_authority 要 version+1 并写 history、幂等（同值不叠版）、tree 的可见性裁剪。
  `test_api_docs.py` 打桩了服务层，抓不到这些；#87 那个 history 500 正是真库才暴露的。
"""

import pytest
from conftest import requires_db

from app import living_doc
from app.library import LibraryError

pytestmark = requires_db

ORG = "dida"
PROJECT = "dida/pytest-livingdoc"
SECTION = "dependency"
MARK = "pytest-ld-"


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


def seed_doc(db, content, *, authority="draft", section=SECTION, project=PROJECT, org=ORG) -> int:
    return db.query_one(
        "INSERT INTO living_doc (org_key, project_key, section_key, content, authority)"
        " VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (org, project, section, content, authority))["id"]


# ─────────────────────────── get / tree ───────────────────────────

def test_get_returns_the_row_with_its_section_label(db):
    doc_id = seed_doc(db, f"{MARK}禁止引入 Lombok")
    row = living_doc.get(doc_id)
    assert row["id"] == doc_id and row["content"] == f"{MARK}禁止引入 Lombok"
    assert row["label"]            # JOIN doc_sections 带出中文栏位名


def test_get_a_missing_doc_raises_not_found(db):
    with pytest.raises(LibraryError) as exc:
        living_doc.get(999_999_999)
    assert exc.value.status == 404 and exc.value.code == "not_found"


def test_tree_returns_every_authority_including_draft(db):
    """★ 与 §6.5 bootstrap 不同：tree 要让人看见还没生效的 draft。"""
    seed_doc(db, f"{MARK}draft 一条", authority="draft")
    seed_doc(db, f"{MARK}confirmed 一条", authority="confirmed", section="answer_style")
    out = living_doc.tree(org_key=ORG, project_key=PROJECT)
    contents = {i["content"] for i in out["items"]}
    assert f"{MARK}draft 一条" in contents and f"{MARK}confirmed 一条" in contents
    assert out["total"] == len(out["items"])


def test_tree_filters_by_a_single_authority(db):
    seed_doc(db, f"{MARK}draft 一条", authority="draft")
    seed_doc(db, f"{MARK}confirmed 一条", authority="confirmed", section="answer_style")
    out = living_doc.tree(org_key=ORG, project_key=PROJECT, authority="confirmed")
    # ★ 必须按 MARK 前缀过滤再断言（#69 的同一条教训）：真库 dida org 下已经有
    #   真实 confirmed 规范，全量 set 相等断言会在真实数据长出来后假失败。
    seeded = {i["content"] for i in out["items"] if i["content"].startswith(MARK)}
    assert seeded == {f"{MARK}confirmed 一条"}


def test_tree_rejects_a_bad_authority(db):
    with pytest.raises(LibraryError) as exc:
        living_doc.tree(org_key=ORG, authority="bogus")
    assert exc.value.status == 400 and exc.value.code == "bad_authority"


def test_tree_without_a_project_shows_only_org_level_rows(db):
    """include_projects=False ⇒ 只返回 org 级（project IS NULL），不漏 project 级。"""
    seed_doc(db, f"{MARK}org 级", project=None)
    seed_doc(db, f"{MARK}project 级", project=PROJECT)
    out = living_doc.tree(org_key=ORG, include_projects=False)
    contents = {i["content"] for i in out["items"]}
    assert f"{MARK}org 级" in contents and f"{MARK}project 级" not in contents


# ─────────────────────────── edit ───────────────────────────

def test_edit_bumps_version_and_writes_history(db):
    doc_id = seed_doc(db, f"{MARK}旧正文", authority="draft")
    out = living_doc.edit(doc_id, f"{MARK}新正文", note="改错了")
    assert out["content"] == f"{MARK}新正文" and out["version"] == 2
    assert out["origin"] == "human"
    hist = living_doc.history(doc_id)
    assert len(hist) == 1
    assert hist[0]["old_content"] == f"{MARK}旧正文"
    assert hist[0]["new_content"] == f"{MARK}新正文"
    assert hist[0]["changed_by"] == "human"


def test_edit_to_the_same_content_is_a_no_op(db):
    """同值不叠版：否则每次手滑点保存都多一行 history，版本噪声淹没真改动。"""
    doc_id = seed_doc(db, f"{MARK}正文", authority="draft")
    out = living_doc.edit(doc_id, f"{MARK}正文")
    assert out["version"] == 1
    assert living_doc.history(doc_id) == []


def test_edit_strips_whitespace_and_rejects_empty(db):
    doc_id = seed_doc(db, f"{MARK}正文")
    with pytest.raises(LibraryError) as exc:
        living_doc.edit(doc_id, "   ")
    assert exc.value.status == 400 and exc.value.code == "empty_content"


def test_edit_a_missing_doc_raises_not_found(db):
    with pytest.raises(LibraryError) as exc:
        living_doc.edit(999_999_999, f"{MARK}新正文")
    assert exc.value.status == 404


# ─────────────────────────── set_authority ───────────────────────────

def test_set_authority_bumps_version_and_records_the_change(db):
    doc_id = seed_doc(db, f"{MARK}正文", authority="draft")
    out = living_doc.set_authority(doc_id, "confirmed")
    assert out["authority"] == "confirmed" and out["version"] == 2
    hist = living_doc.history(doc_id)
    assert hist[0]["old_authority"] == "draft" and hist[0]["new_authority"] == "confirmed"
    # content 没变，但 authority 变了也算一版
    assert hist[0]["old_content"] == hist[0]["new_content"] == f"{MARK}正文"


def test_set_authority_to_the_same_value_is_a_no_op(db):
    doc_id = seed_doc(db, f"{MARK}正文", authority="confirmed")
    out = living_doc.set_authority(doc_id, "confirmed")
    assert out["version"] == 1
    assert living_doc.history(doc_id) == []


def test_set_authority_rejects_a_bad_value(db):
    doc_id = seed_doc(db, f"{MARK}正文")
    with pytest.raises(LibraryError) as exc:
        living_doc.set_authority(doc_id, "bogus")
    assert exc.value.status == 400 and exc.value.code == "bad_authority"


# ─────────────────────────── history ───────────────────────────

def test_history_returns_versions_newest_first(db):
    """★ #87 回归：history 曾混用 %s 和 %(limit)s 占位符 → psycopg 抛
    'positional placeholders require a sequence' → 500。这里连真库跑，钉死它。"""
    doc_id = seed_doc(db, f"{MARK}v1", authority="draft")
    living_doc.edit(doc_id, f"{MARK}v2")
    living_doc.edit(doc_id, f"{MARK}v3")
    hist = living_doc.history(doc_id)
    assert [h["version"] for h in hist] == [3, 2]      # DESC，且不含初始 v1（无历史行）
    assert hist[0]["new_content"] == f"{MARK}v3"


def test_history_respects_the_limit(db):
    doc_id = seed_doc(db, f"{MARK}v1")
    for n in range(2, 6):
        living_doc.edit(doc_id, f"{MARK}v{n}")
    assert len(living_doc.history(doc_id, limit=2)) == 2


def test_history_of_a_missing_doc_raises_not_found(db):
    with pytest.raises(LibraryError) as exc:
        living_doc.history(999_999_999)
    assert exc.value.status == 404
