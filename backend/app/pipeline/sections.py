"""§5.3 栏位匹配：本地、同步、不进队列。

拿 `content` 的 bge-m3 向量 vs `doc_sections.match_embedding`（label + aliases 的向量），
22 行顺序扫描，取 top-1。分数太低、或 top-1 与 top-2 贴得太近 → 交人工（`stage='section'`）。

★ **为什么不让 LLM 自选**（#50）：3+3 实测里同一条信息三次跑出 2–3 个不同栏位
  （Lombok → dependency/code_style/dependency；JDK17 → local_env/build_env/build_env）。
  栏位一飘，§4.3.2 的两次向量比对就在错误的分区里做——同栏位查重查不到重复，
  否决记忆也拦不住已经否决过的提议。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core import db
from ..core.config import get_settings
from ..core.embedding import as_list, get_embedding_service


@dataclass(frozen=True)
class Section:
    section_key: str
    label: str
    applies_to: str
    org_key: str | None
    sort: int
    embedding: list[float]


@dataclass(frozen=True)
class SectionMatch:
    section_key: str
    score: float
    margin: float
    runner_up: str | None
    org_key: str | None
    candidates: int


SQL = ("SELECT section_key, label, applies_to, org_key, sort, match_embedding "
       "FROM doc_sections WHERE enabled ORDER BY sort, section_key")


def load_sections(conn=None, *, has_project: bool = True) -> list[Section]:
    """`applies_to='project'` 的栏位（一供应商一份）只有在有 project_key 时才是合法候选。"""
    rows = conn.execute(SQL).fetchall() if conn is not None else db.query(SQL)
    out = []
    for r in rows:
        if r["applies_to"] == "project" and not has_project:
            continue
        if r["match_embedding"] is None:
            continue  # 词表改了没重算 embedding，宁可不匹配也不要匹配到零向量
        out.append(Section(section_key=r["section_key"], label=r["label"],
                           applies_to=r["applies_to"], org_key=r["org_key"],
                           sort=r["sort"],
                           embedding=as_list(r["match_embedding"])))
    return out


def match(content: str, sections: list[Section], *, embedding: list[float] | None = None,
          min_score: float | None = None, min_margin: float | None = None) -> SectionMatch | None:
    """返回 None 表示"匹配不出"，调用方必须把它送进 `pending_items(stage='section')`。"""
    if not sections:
        return None
    settings = get_settings()
    floor = settings.section_match_min_score if min_score is None else min_score
    margin_floor = settings.section_match_min_margin if min_margin is None else min_margin

    vector = embedding if embedding is not None else get_embedding_service().encode_one(content)
    scored = sorted(
        ((sum(a * b for a, b in zip(vector, s.embedding)), s) for s in sections),
        key=lambda pair: pair[0],
        reverse=True,
    )
    top_score, top = scored[0]
    runner_score, runner = (scored[1] if len(scored) > 1 else (0.0, None))
    if top_score < floor:
        return None
    margin = top_score - runner_score
    if margin < margin_floor:
        return None  # 两个栏位一样像 = 词表分不开，人来定
    return SectionMatch(section_key=top.section_key, score=round(top_score, 4),
                        margin=round(margin, 4),
                        runner_up=runner.section_key if runner else None,
                        org_key=top.org_key, candidates=len(sections))
