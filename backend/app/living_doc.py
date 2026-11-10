"""成文规范（`living_doc`）的服务层：树浏览、人工编辑、authority 升降档、版本历史。

★ 和 `library.py`（管 `memories`）并列——HTTP 层是这里的薄适配器（#78），业务规则
  全在本模块。复用 `library.LibraryError`，这样 API 的 `_guard` 不用认第二种异常。

★ **`living_doc` 没有 embedding 列**（成文规范是短文本，一个分区只有一行，比对① 现算
  比维护一列向量便宜，也不会"改了 content 忘了改向量"漂移，见 review._blocked_by）。
  所以这里所有写入都是**纯 SQL**，不碰模型、不需要 503 拦截。

★ 每次改动都写一行 `living_doc_history`（§4.1）：version 单调递增，content 或 authority
  任一变化都算一版。少写历史，"这条规矩什么时候从 draft 变 confirmed、谁改的"就查不到。
"""

from __future__ import annotations

import logging

from .core import db
from .library import LibraryError, not_found

log = logging.getLogger(__name__)

_VALID_AUTHORITY = ("draft", "confirmed", "pinned")

_DOC_COLUMNS = ("d.id, d.org_key, d.project_key, d.section_key, d.content, d.authority,"
                " d.origin, d.origin_ref, d.source_ids, d.version, d.updated_at, s.label")


def tree(*, org_key: str | None = None, project_key: str | None = None,
         section_key: str | None = None, authority: str | None = None,
         include_projects: bool = True, limit: int = 200, offset: int = 0) -> dict:
    """成文规范列表（`GET /v1/living-doc`）。★ 与 §6.5 bootstrap 不同：这里**返回所有
    authority**（含 draft），因为 UI 要让人看见还没生效的草稿；bootstrap 只注入
    confirmed/pinned。默认按 §6.1 的 project 精确匹配裁剪（org 级 + 本 project 级）。"""
    where: list[str] = ["s.enabled"]
    params: dict = {"limit": limit, "offset": offset}
    if org_key:
        # org 级（project IS NULL）+ 本 project 级，和 bootstrap 的可见性一致。
        if include_projects and project_key:
            where.append("d.org_key = %(org)s AND (d.project_key IS NULL"
                         " OR d.project_key = %(proj)s)")
            params["org"], params["proj"] = org_key, project_key
        else:
            where.append("d.org_key = %(org)s" + ("" if include_projects
                                                  else " AND d.project_key IS NULL"))
            params["org"] = org_key
    if section_key:
        where.append("d.section_key = %(sec)s")
        params["sec"] = section_key
    if authority:
        if authority not in _VALID_AUTHORITY:
            raise LibraryError(400, "bad_authority",
                               f"authority 只能是 {_VALID_AUTHORITY}，收到 {authority!r}")
        where.append("d.authority = %(auth)s")
        params["auth"] = authority
    clause = " WHERE " + " AND ".join(where)
    total = db.query_one(
        f"SELECT count(*) AS n FROM living_doc d JOIN doc_sections s USING (section_key)"
        f"{clause}", params)["n"]
    rows = db.query(
        f"SELECT {_DOC_COLUMNS} FROM living_doc d JOIN doc_sections s USING (section_key)"
        f"{clause} ORDER BY s.sort, d.section_key, d.project_key NULLS FIRST, d.id"
        f" LIMIT %(limit)s OFFSET %(offset)s", params)
    return {"total": total, "limit": limit, "offset": offset, "items": rows}


def get(doc_id: int) -> dict:
    row = db.query_one(
        f"SELECT {_DOC_COLUMNS} FROM living_doc d JOIN doc_sections s USING (section_key)"
        f" WHERE d.id = %(id)s", {"id": doc_id})
    if row is None:
        raise not_found("成文规范", doc_id)
    return row


def edit(doc_id: int, content: str, *, note: str | None = None,
         changed_by: str = "human") -> dict:
    """人工直接改正文（`PUT /v1/living-doc/{id}`）。origin 转 `human`、version+1、写 history。"""
    content = (content or "").strip()
    if not content:
        raise LibraryError(400, "empty_content", "content 不能为空")
    with db.transaction() as tx:
        row = tx.execute("SELECT id, content, authority, version FROM living_doc"
                         " WHERE id=%s FOR UPDATE", (doc_id,)).fetchone()
        if row is None:
            raise not_found("成文规范", doc_id)
        if row["content"] == content:
            return get(doc_id)   # 没变就不必叠一版历史
        new_version = row["version"] + 1
        tx.execute(
            "UPDATE living_doc SET content=%s, origin='human', origin_ref=%s,"
            "       version=%s, updated_at=now() WHERE id=%s",
            (content, note, new_version, doc_id))
        tx.execute(
            "INSERT INTO living_doc_history (living_doc_id, version, old_content, new_content,"
            "                               old_authority, new_authority, changed_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (doc_id, new_version, row["content"], content,
             row["authority"], row["authority"], changed_by))
    return get(doc_id)


def set_authority(doc_id: int, authority: str) -> dict:
    """draft→confirmed→pinned（`PATCH /v1/living-doc/{id}/authority`）。content 不变，
    但 authority 变化也算一版历史（version+1），这样"何时被钉死"可追溯。"""
    if authority not in _VALID_AUTHORITY:
        raise LibraryError(400, "bad_authority",
                           f"authority 只能是 {_VALID_AUTHORITY}，收到 {authority!r}")
    with db.transaction() as tx:
        row = tx.execute("SELECT id, content, authority, version FROM living_doc"
                         " WHERE id=%s FOR UPDATE", (doc_id,)).fetchone()
        if row is None:
            raise not_found("成文规范", doc_id)
        if row["authority"] == authority:
            return get(doc_id)   # 幂等：已经是这个档就不叠历史
        new_version = row["version"] + 1
        tx.execute("UPDATE living_doc SET authority=%s, version=%s, updated_at=now()"
                   " WHERE id=%s", (authority, new_version, doc_id))
        tx.execute(
            "INSERT INTO living_doc_history (living_doc_id, version, old_content, new_content,"
            "                               old_authority, new_authority, changed_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,'human')",
            (doc_id, new_version, row["content"], row["content"],
             row["authority"], authority))
    return get(doc_id)


def history(doc_id: int, limit: int = 100) -> list[dict]:
    """版本历史（P6 的 diff 视图会用它；这里先给出数据）。"""
    if db.query_one("SELECT 1 FROM living_doc WHERE id=%(id)s", {"id": doc_id}) is None:
        raise not_found("成文规范", doc_id)
    return db.query(
        "SELECT id, version, old_content, new_content, old_authority, new_authority,"
        "       proposal_id, changed_by, created_at"
        " FROM living_doc_history WHERE living_doc_id=%(id)s"
        " ORDER BY version DESC LIMIT %(limit)s",
        {"id": doc_id, "limit": limit})
