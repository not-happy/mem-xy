"""词表管理服务层（§11 词表页 → `/v1/sections`）。

`doc_sections` 是**受控词表**（§5.3）：栏位不自由生成，22 个由 seed 落定。这一页是
§14 item1「复核 aliases」的工具——让人看清每栏位靠哪些词去向量匹配、改 label/aliases、
调 sort/applies_to、启用/停用。

★ **改 label/aliases 必须重算 `match_embedding`**：§5.3 的栏位匹配拿 content 的向量
  vs `match_embedding`（= `match_text(label,aliases)` 的向量）。改了词却不重算，等于让
  这一栏位拿着**旧向量**去匹配新词——`load_sections` 又会跳过 embedding 为 NULL 的行，
  所以「改了没重算」的净效果是这一栏位悄悄下线。重算走 `seed.recompute_section_embeddings()`
  （只有 22 条短文本，同步、不进 job 队列，§5.3）。

★ 重算要模型就绪，所以**写路径在 API 层 503 拦截**（和 `/memories` 写路径同构）。
★ 停用（enabled=false）是安全的「下线」：不删数据、只是不再作为匹配候选；删除有引用守卫。
"""

from __future__ import annotations

import logging

from .core import db
from .core.seed import match_text, recompute_section_embeddings
from .library import LibraryError, not_found

log = logging.getLogger(__name__)

_VALID_APPLIES = ("org", "project", "both")

# 删栏位前要查的引用：前两个是硬 FK（删了炸库），后一个持真实数据（删了会孤儿）。
_SECTION_REFS = (
    ("raw_observations", "观察"),
    ("living_doc", "成文规范"),
    ("doc_proposals", "提案"),
)


# ─────────────────────────── 读 ───────────────────────────

def overview() -> dict:
    """词表页首屏：全部栏位（含 aliases / 是否已算 embedding / 是否 stale）+ 计数。"""
    rows = db.query(
        "SELECT section_key, label, aliases, applies_to, org_key, sort, enabled,"
        "       match_text, (match_embedding IS NOT NULL) AS has_embedding"
        " FROM doc_sections ORDER BY sort, section_key")
    for r in rows:
        # stale = 存着的 match_text 跟当前 label/aliases 算出来的对不上，或压根没 embedding。
        # 正常编辑流程会同步重算，所以一般为 False；True 说明有人绕过这一页直接改了库。
        r["stale"] = (not r["has_embedding"]) or r["match_text"] != match_text(r["label"], r["aliases"])
        r.pop("match_text", None)   # 内部判据，不必回前端
    return {
        "sections": rows,
        "total": len(rows),
        "enabled_count": sum(1 for r in rows if r["enabled"]),
        "missing_embedding": sum(1 for r in rows if not r["has_embedding"]),
        "stale_count": sum(1 for r in rows if r["stale"]),
        "applies_options": list(_VALID_APPLIES),
    }


# ─────────────────────────── 增删改 ───────────────────────────

def create_section(*, section_key: str, label: str, aliases: list[str] | None = None,
                   applies_to: str = "org", org_key: str | None = None,
                   sort: int = 100, enabled: bool = True) -> dict:
    """新增一个栏位，并**当场算好 match_embedding**（否则它不可匹配，等于没加）。"""
    section_key = (section_key or "").strip()
    label = (label or "").strip()
    aliases = _clean_aliases(aliases)
    _validate_key(section_key)
    if not label:
        raise LibraryError(400, "bad_label", "label 不能为空")
    _validate_applies(applies_to)
    _validate_org(org_key)
    if db.query_one("SELECT 1 FROM doc_sections WHERE section_key=%(k)s", {"k": section_key}):
        raise LibraryError(409, "section_exists", f"栏位 {section_key!r} 已存在")
    # match_text / match_embedding 先留空，INSERT 后由 recompute 一并填（见下方注释）
    db.execute(
        "INSERT INTO doc_sections (section_key, label, aliases, applies_to, org_key, sort, enabled)"
        " VALUES (%(k)s,%(l)s,%(a)s,%(ap)s,%(o)s,%(s)s,%(e)s)",
        {"k": section_key, "l": label, "a": aliases, "ap": applies_to,
         "o": org_key, "s": sort, "e": enabled})
    recompute_section_embeddings()
    return _get_section(section_key)


def update_section(section_key: str, *, label: str | None = None,
                   aliases: list[str] | None = None, applies_to: str | None = None,
                   org_key: str | None = None, sort: int | None = None,
                   enabled: bool | None = None) -> dict:
    """改栏位。★ 动了 label/aliases 就重算 embedding；只改 sort/enabled 等则不碰模型。"""
    _get_section(section_key)   # 404 判掉
    sets, params = [], {"k": section_key}
    touched_match = False       # 是否改了参与向量匹配的字段（label / aliases）
    if label is not None:
        if not label.strip():
            raise LibraryError(400, "bad_label", "label 不能为空")
        sets.append("label=%(l)s"); params["l"] = label.strip(); touched_match = True
    if aliases is not None:
        cleaned = _clean_aliases(aliases)
        sets.append("aliases=%(a)s"); params["a"] = cleaned; touched_match = True
    if applies_to is not None:
        _validate_applies(applies_to)
        sets.append("applies_to=%(ap)s"); params["ap"] = applies_to
    if org_key is not None:
        _validate_org(org_key)
        sets.append("org_key=%(o)s"); params["o"] = org_key
    if sort is not None:
        sets.append("sort=%(s)s"); params["s"] = sort
    if enabled is not None:
        sets.append("enabled=%(e)s"); params["e"] = enabled
    if sets:
        # ★ 刻意**不**在这里写 match_text：留给 recompute 把 match_text 和 embedding 一起更新。
        #   若在此把 match_text 改成新值，recompute 会误判「已是最新」而跳过，留下配错词的旧向量。
        db.execute(f"UPDATE doc_sections SET {', '.join(sets)} WHERE section_key=%(k)s", params)
    if touched_match:
        recompute_section_embeddings()
    return _get_section(section_key)


def delete_section(section_key: str) -> dict:
    """删栏位。★ 被观察/成文规范/提案引用就拒（409），提示改用停用——停用不丢数据。"""
    _get_section(section_key)
    refs = []
    for table, label in _SECTION_REFS:
        n = db.query_one(
            f"SELECT count(*)::int AS n FROM {table} WHERE section_key=%(k)s",
            {"k": section_key})["n"]
        if n:
            refs.append(f"{label} {n}")
    if refs:
        raise LibraryError(409, "section_in_use",
                           f"栏位 {section_key!r} 仍被引用（{'、'.join(refs)}）；"
                           "不能删，请改为停用（enabled=false）")
    db.execute("DELETE FROM doc_sections WHERE section_key=%(k)s", {"k": section_key})
    return {"deleted": section_key}


def recompute() -> dict:
    """手动触发全表重算（页面「重算 embedding」按钮）。返回重算了几行。"""
    n = recompute_section_embeddings()
    return {"recomputed": n}


# ─────────────────────────── 内部 ───────────────────────────

def _get_section(section_key: str) -> dict:
    row = db.query_one(
        "SELECT section_key, label, aliases, applies_to, org_key, sort, enabled,"
        "       (match_embedding IS NOT NULL) AS has_embedding"
        " FROM doc_sections WHERE section_key=%(k)s", {"k": section_key})
    if row is None:
        raise not_found("词表栏位", section_key)
    return row


def _clean_aliases(aliases: list[str] | None) -> list[str]:
    """去空白、去空串、去重（保序）。aliases 直接进 match_text，脏词会污染匹配。"""
    if not aliases:
        return []
    out: list[str] = []
    for a in aliases:
        a = (a or "").strip()
        if a and a not in out:
            out.append(a)
    return out


def _validate_key(section_key: str) -> None:
    if not section_key:
        raise LibraryError(400, "bad_section_key", "section_key 不能为空")
    if not section_key.replace("-", "").replace("_", "").isalnum():
        raise LibraryError(400, "bad_section_key",
                           f"section_key 只能是字母/数字/-/_，收到 {section_key!r}")


def _validate_applies(applies_to: str) -> None:
    if applies_to not in _VALID_APPLIES:
        raise LibraryError(400, "bad_applies_to",
                           f"applies_to 只能是 {_VALID_APPLIES}，收到 {applies_to!r}")


def _validate_org(org_key: str | None) -> None:
    """org_key 为 NULL = 通用词表（所有 org 可用）；给了就必须是已存在的 org。"""
    if org_key is None:
        return
    if not db.query_one("SELECT 1 FROM orgs WHERE org_key=%(k)s", {"k": org_key}):
        raise LibraryError(400, "bad_org_key", f"org {org_key!r} 不存在")
