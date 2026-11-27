"""归属管理服务层（§5.1 / §7.1 的 `GET/PUT /v1/attribution`）。

纯 SQL，不碰模型、不调 git（`Resolver` 已经在摄取时把归属结果缓存进 `workspace_map` 了）。
这一层给「归属管理」页用，做三件事：

1. **看**：列 `org_match_rules`（规则）+ `workspace_map`（每个目录判成了什么），
   重点是 `resolved_by='default'` 的那批——没命中任何规则、暂落 personal 默认桶，等人确认。
2. **改**：人工把某个目录的归属定下来（`resolved_by='human'`，规则不再覆盖它）。
3. **回溯**：目录改判后，把**已经入库**的记忆按 `source_msg → conversation_log.cwd`
   找到、批量改 `org_key`/`project_key`（§5.1「已入库的记忆按 source_msg 回溯改 org_key」）。

★ 规则和 workspace_map 是两份东西：规则是"怎么判"，workspace_map 是"判过的结果缓存"。
  改规则不会自动重判已缓存的目录（那要重扫），所以页面上两者分开列、分开改。
"""

from __future__ import annotations

import logging

from .core import db
from .library import LibraryError, not_found

log = logging.getLogger(__name__)

_VALID_RESOLVED_BY = ("rule", "human", "default")


# ─────────────────────────── 读 ───────────────────────────

def overview(*, resolved_by: str | None = None, org_key: str | None = None,
             limit: int = 500) -> dict:
    """归属管理页的首屏：规则 + 目录缓存 + 各来源计数。"""
    rules = db.query(
        "SELECT id, pattern_type, pattern, org_key, project_key, priority, enabled"
        " FROM org_match_rules ORDER BY priority, id")

    where: list[str] = []
    params: dict = {"limit": limit}
    if resolved_by:
        where.append("resolved_by = %(resolved_by)s")
        params["resolved_by"] = resolved_by
    if org_key:
        where.append("org_key = %(org)s")
        params["org"] = org_key
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    workspaces = db.query(
        "SELECT cwd, org_key, project_key, match_rule_id, resolved_by, enabled, resolved_at"
        f" FROM workspace_map{clause} ORDER BY resolved_by, cwd LIMIT %(limit)s", params)

    by_source = {r["resolved_by"]: r["n"] for r in db.query(
        "SELECT resolved_by, count(*)::int AS n FROM workspace_map GROUP BY resolved_by")}
    orgs = db.query("SELECT org_key, name, is_personal, enabled FROM orgs ORDER BY org_key")

    return {"rules": rules, "workspaces": workspaces,
            "by_source": by_source, "orgs": orgs,
            "default_count": by_source.get("default", 0)}


# ─────────────────────────── 改单个目录 ───────────────────────────

def set_workspace(cwd: str, *, org_key: str, project_key: str | None = None,
                  retroactive: bool = True) -> dict:
    """人工把一个目录的归属定下来（`resolved_by='human'`，此后规则不再覆盖它）。

    `retroactive=True`（默认）时，顺带把已经入库、源自这个 cwd 的记忆改判到新分区。
    返回改判的行数，页面上好给人一个"影响了几条"的交代。
    """
    if not cwd or not cwd.strip():
        raise LibraryError(400, "bad_cwd", "cwd 不能为空")
    if not org_key or not org_key.strip():
        raise LibraryError(400, "bad_org", "org_key 不能为空")
    cwd = cwd.strip()
    org_key = org_key.strip()
    project_key = project_key.strip() if project_key else None

    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO workspace_map (cwd, org_key, project_key, resolved_by)"
            " VALUES (%(cwd)s,%(org)s,%(proj)s,'human')"
            " ON CONFLICT (cwd) DO UPDATE SET"
            "   org_key=EXCLUDED.org_key, project_key=EXCLUDED.project_key,"
            "   resolved_by='human', enabled=TRUE, resolved_at=now()",
            {"cwd": cwd, "org": org_key, "proj": project_key})
        moved = 0
        if retroactive:
            moved = _reassign_memories(tx, cwd, org_key, project_key)
    return {"cwd": cwd, "org_key": org_key, "project_key": project_key,
            "resolved_by": "human", "memories_moved": moved}


def _reassign_memories(conn, cwd: str, org_key: str, project_key: str | None) -> int:
    """把源自这个 cwd 的记忆改判到新分区。

    链路：`memories.source_msg → conversation_log.id`，`conversation_log.cwd` 是目录。
    ★ 只改由这个 cwd 抽出来的记忆，别的分区的记忆一律不碰。project_key 给了就一起改，
      没给（org 级归属）就只改 org_key、保留原 project_key。
    """
    if project_key is not None:
        cur = conn.execute(
            "UPDATE memories SET org_key=%(org)s, project_key=%(proj)s, updated_at=now()"
            " WHERE source_msg IN (SELECT id FROM conversation_log WHERE cwd=%(cwd)s)"
            "   AND (org_key IS DISTINCT FROM %(org)s OR project_key IS DISTINCT FROM %(proj)s)",
            {"org": org_key, "proj": project_key, "cwd": cwd})
    else:
        cur = conn.execute(
            "UPDATE memories SET org_key=%(org)s, updated_at=now()"
            " WHERE source_msg IN (SELECT id FROM conversation_log WHERE cwd=%(cwd)s)"
            "   AND org_key IS DISTINCT FROM %(org)s",
            {"org": org_key, "cwd": cwd})
    return cur.rowcount or 0


# ─────────────────────────── 规则 ───────────────────────────

def _invalidate_rule_cache() -> int:
    """规则增改删后，清掉规则/默认来源的 workspace_map 缓存行（B7/W7）。

    ★ 只清 `resolved_by<>'human'` 的行——人工确认过的归属优先级高于一切规则
      （§5.1 边界情形），必须保留。清掉的行下次 resolve() 会按**新规则**重判
      （git remote 现查 + match_rules + _persist 回写），不再沿用旧规则的历史缓存。
    进行中的扫描握着自己的内存 _cache，把这一批扫完再失效——可接受的单次窗口。
    """
    removed = db.execute("DELETE FROM workspace_map WHERE resolved_by <> 'human'") or 0
    if removed:
        log.info("归属规则变更：清掉 %d 条规则/默认缓存行，下次扫描按新规则重判", removed)
    return removed


def upsert_rule(*, pattern_type: str, pattern: str, org_key: str,
                project_key: str | None = None, priority: int = 50,
                enabled: bool = True, rule_id: int | None = None) -> dict:
    """新增或改一条 `org_match_rules`。

    ★ B7（W7）：写完规则就清掉规则/默认来源的 workspace_map 缓存行——
      下次扫描所有非人工目录按新规则重判（每个目录多一次 git remote，一次性成本）。
      人工确认过的行保留，不被新规则推翻。
    """
    if pattern_type not in ("cwd_prefix", "git_remote_path", "git_remote_host"):
        raise LibraryError(400, "bad_pattern_type",
                           f"pattern_type 只能是 cwd_prefix/git_remote_path/git_remote_host，"
                           f"收到 {pattern_type!r}")
    if not pattern or not pattern.strip():
        raise LibraryError(400, "bad_pattern", "pattern 不能为空")
    if not org_key or not org_key.strip():
        raise LibraryError(400, "bad_org", "org_key 不能为空")
    params = {"pt": pattern_type, "pat": pattern.strip(), "org": org_key.strip(),
              "proj": project_key.strip() if project_key else None,
              "prio": priority, "en": enabled}
    if rule_id is None:
        new_id = db.query_one(
            "INSERT INTO org_match_rules (pattern_type, pattern, org_key, project_key,"
            "                            priority, enabled)"
            " VALUES (%(pt)s,%(pat)s,%(org)s,%(proj)s,%(prio)s,%(en)s) RETURNING id",
            params)["id"]
    else:
        params["id"] = rule_id
        cur = db.execute(
            "UPDATE org_match_rules SET pattern_type=%(pt)s, pattern=%(pat)s,"
            "   org_key=%(org)s, project_key=%(proj)s, priority=%(prio)s, enabled=%(en)s"
            " WHERE id=%(id)s", params)
        if not cur:
            raise not_found("归属规则", rule_id)
        new_id = rule_id
    _invalidate_rule_cache()
    return db.query_one("SELECT id, pattern_type, pattern, org_key, project_key, priority,"
                        " enabled FROM org_match_rules WHERE id=%(id)s", {"id": new_id})


def delete_rule(rule_id: int) -> dict:
    """删一条规则，并清掉规则/默认来源的 workspace_map 缓存行（B7/W7）——
    下次扫描按剩余规则重判，靠旧规则判进去的目录不再固化。人工行保留。"""
    cur = db.execute("DELETE FROM org_match_rules WHERE id=%(id)s", {"id": rule_id})
    if not cur:
        raise not_found("归属规则", rule_id)
    _invalidate_rule_cache()
    return {"deleted": rule_id}
