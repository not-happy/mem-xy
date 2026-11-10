"""SessionStart 稳定层组装（§6.5）。

和 §6.4 的每轮召回是两回事：

| | 稳定层（这里） | 动态召回（recall.py） |
|---|---|---|
| 时机 | 会话开始，一次 | 每轮，跟着 query 走 |
| 内容 | **已成文的规矩** + user 级稳定记忆 | 跟当前问题相关的零碎事实 |
| 来源 | `living_doc`（authority=confirmed/pinned）+ `memories` | `memories` |
| 要不要向量 | ★ 不要，纯 SQL | 要，encode 一次 |

★ `draft` 的 living_doc **不注入**：还没过人工那一道（§5.7 的第二道），
  注入了就等于绕过审核。这是"规矩类 100% 人审"这条底线的最后一环。
★ `living_doc` 为空是常态（P5 才有汇总器），此时稳定层只有 user 级记忆，甚至全空。
  空 markdown 是合法返回值，hook 那边照样落本地快照缓存。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core import db

_DOC_SQL = """
SELECT d.section_key, s.label, s.sort, d.content, d.authority, d.project_key
  FROM living_doc d JOIN doc_sections s USING (section_key)
 WHERE d.authority IN ('confirmed','pinned') AND s.enabled
   AND ( (d.org_key = %(org)s AND d.project_key IS NULL)
      OR (d.org_key = %(org)s AND d.project_key = %(proj)s) )
 ORDER BY s.sort, d.section_key, d.project_key NULLS FIRST, d.id
"""

# ★ 只取 profile/pointer，不取 preference：preference 是**情境性**的
#   （"在这个项目里坚持用 pytest"），无条件注入到每个会话就是噪声，靠每轮召回带出来。
_USER_SQL = """
SELECT id, role, content
  FROM memories
 WHERE status = 'active' AND scope = 'user' AND role IN ('profile','pointer')
 ORDER BY last_seen_at DESC, id
 LIMIT %(limit)s
"""


@dataclass
class Bootstrap:
    org_key: str
    project_key: str | None
    markdown: str
    sections: list[dict] = field(default_factory=list)
    memory_count: int = 0
    cache_key: str = ""

    def as_dict(self) -> dict:
        return {
            "org_key": self.org_key, "project_key": self.project_key,
            "markdown": self.markdown, "sections": self.sections,
            "memory_count": self.memory_count, "cache_key": self.cache_key,
        }


def cache_key(org_key: str, project_key: str | None) -> str:
    """本地快照缓存文件名（§4.3.3）。`/` 不能进文件名，换成 `-`。"""
    return f"bootstrap-{org_key}-{(project_key or 'none').replace('/', '-')}"


def build(*, org_key: str, project_key: str | None = None,
          user_memory_limit: int = 20) -> Bootstrap:
    docs = db.query(_DOC_SQL, {"org": org_key, "proj": project_key})
    users = db.query(_USER_SQL, {"limit": user_memory_limit})

    sections: list[dict] = []
    for row in docs:
        if not sections or sections[-1]["section_key"] != row["section_key"]:
            sections.append({"section_key": row["section_key"],
                             "label": row["label"], "items": []})
        # ★ pinned 标出来：它是"人工钉死、汇总器不许改"的那一档（§5.6），
        #   注入时让 AI 知道这条的权威级别比旁边的高。
        prefix = "【钉死】" if row["authority"] == "pinned" else ""
        where = "" if row["project_key"] is None else "（本仓库专属）"
        sections[-1]["items"].append(f"{prefix}{row['content']}{where}")

    blocks = [f"## {s['label']}\n" + "\n".join(f"- {i}" for i in s["items"])
              for s in sections]
    if users:
        blocks.append("## 关于我\n" + "\n".join(f"- {r['content']}" for r in users))

    return Bootstrap(
        org_key=org_key, project_key=project_key,
        markdown="\n\n".join(blocks),
        sections=sections,
        memory_count=len(docs) + len(users),
        cache_key=cache_key(org_key, project_key),
    )
