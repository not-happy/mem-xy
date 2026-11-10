"""组织管理服务层（§11 组织管理页 → `/v1/orgs`、`/v1/endpoints`）。

管两样东西：

1. **orgs**：检索分区。★ org 只作检索分区、**不是权限边界**（§14 已定，不做信任/权限模型）。
   增删改 + `retention_days`（保留期，P7 清理会读它）。
2. **llm_endpoints**：端点的**非敏感字段**（base_url / model / api_protocol / max_input_tokens / note）。
   ★ **token 绝不在这张表、也绝不经这个端点进出**——它只走 `~/.memxy/secrets.toml`
   或环境变量（§2.1）。这一层连 `llm_token()` 都不调，从根上杜绝把 token 带回 UI。

★ 删 org 有守卫：org 被 memories/pending/规则/词表等引用时直接拒（409），
  提示改用「停用」（enabled=false）——停用不丢数据，只是不再作为归属目标。
"""

from __future__ import annotations

import logging

from .core import db
from .core.config import get_settings
from .library import LibraryError, not_found

log = logging.getLogger(__name__)

_VALID_PROTOCOLS = ("anthropic", "openai", "ollama")

# 删 org 前要查的引用表：前四个是硬 FK（删了会炸库），后两个持真实数据（删了会孤儿）。
# 任一有行就拒绝删除，让人改用停用。
_ORG_REFS = (
    ("memories", "记忆"),
    ("pending_items", "待办"),
    ("raw_observations", "观察"),
    ("living_doc", "成文规范"),
    ("workspace_map", "目录归属"),
    ("org_match_rules", "归属规则"),
    ("doc_sections", "定制词表"),
)


# ─────────────────────────── 读 ───────────────────────────

def overview() -> dict:
    """组织管理页首屏：orgs（带各自数据量）+ 端点（非敏感）+ 当前 task→endpoint 映射。"""
    settings = get_settings()
    orgs = db.query(
        "SELECT org_key, name, is_personal, retention_days, enabled, created_at"
        " FROM orgs ORDER BY is_personal DESC, org_key")
    # 每个 org 有多少 active 记忆 / 待办，给人一个"删了会丢多少"的直观数
    counts = {r["org_key"]: r for r in db.query(
        "SELECT o.org_key,"
        "  (SELECT count(*)::int FROM memories m WHERE m.org_key=o.org_key AND m.status='active') AS memories,"
        "  (SELECT count(*)::int FROM pending_items p WHERE p.org_key=o.org_key AND p.status='pending') AS pending"
        " FROM orgs o")}
    for o in orgs:
        c = counts.get(o["org_key"], {})
        o["memory_count"] = c.get("memories", 0)
        o["pending_count"] = c.get("pending", 0)

    endpoints = db.query(
        "SELECT endpoint_key, base_url, model, api_protocol, max_input_tokens, enabled, note"
        " FROM llm_endpoints ORDER BY endpoint_key")
    return {
        "orgs": orgs,
        "endpoints": endpoints,
        # task→endpoint 映射来自 config（不是 DB），这里只读展示，让人知道哪个任务走哪个端点
        "default_endpoint": settings.default_endpoint,
        "task_endpoints": settings.task_endpoints,
    }


# ─────────────────────────── orgs 增删改 ───────────────────────────

def create_org(*, org_key: str, name: str, is_personal: bool = False,
               retention_days: int | None = None, enabled: bool = True) -> dict:
    org_key = (org_key or "").strip()
    name = (name or "").strip()
    if not org_key:
        raise LibraryError(400, "bad_org_key", "org_key 不能为空")
    if not org_key.replace("-", "").replace("_", "").isalnum():
        raise LibraryError(400, "bad_org_key",
                           f"org_key 只能是字母/数字/-/_，收到 {org_key!r}")
    if not name:
        raise LibraryError(400, "bad_name", "name 不能为空")
    if db.query_one("SELECT 1 FROM orgs WHERE org_key=%(k)s", {"k": org_key}):
        raise LibraryError(409, "org_exists", f"org {org_key!r} 已存在")
    db.execute(
        "INSERT INTO orgs (org_key, name, is_personal, retention_days, enabled)"
        " VALUES (%(k)s,%(n)s,%(p)s,%(r)s,%(e)s)",
        {"k": org_key, "n": name, "p": is_personal, "r": retention_days, "e": enabled})
    return _get_org(org_key)


def update_org(org_key: str, *, name: str | None = None,
               retention_days: int | None = None, enabled: bool | None = None,
               is_personal: bool | None = None) -> dict:
    _get_org(org_key)   # 404 判掉
    sets, params = [], {"k": org_key}
    if name is not None:
        if not name.strip():
            raise LibraryError(400, "bad_name", "name 不能为空")
        sets.append("name=%(n)s"); params["n"] = name.strip()
    if retention_days is not None:
        sets.append("retention_days=%(r)s"); params["r"] = retention_days
    if enabled is not None:
        sets.append("enabled=%(e)s"); params["e"] = enabled
    if is_personal is not None:
        sets.append("is_personal=%(p)s"); params["p"] = is_personal
    if sets:
        db.execute(f"UPDATE orgs SET {', '.join(sets)} WHERE org_key=%(k)s", params)
    return _get_org(org_key)


def delete_org(org_key: str) -> dict:
    """删 org。★ 被任何数据/规则引用就拒（409），提示改用停用——停用不丢数据。"""
    org = _get_org(org_key)
    if org["is_personal"]:
        raise LibraryError(409, "personal_org",
                           f"{org_key!r} 是个人默认桶（未匹配目录都落这里），不能删；可改为停用")
    refs = []
    for table, label in _ORG_REFS:
        n = db.query_one(
            f"SELECT count(*)::int AS n FROM {table} WHERE org_key=%(k)s", {"k": org_key})["n"]
        if n:
            refs.append(f"{label} {n}")
    if refs:
        raise LibraryError(409, "org_in_use",
                           f"org {org_key!r} 仍被引用（{'、'.join(refs)}）；不能删，请改为停用（enabled=false）")
    db.execute("DELETE FROM orgs WHERE org_key=%(k)s", {"k": org_key})
    return {"deleted": org_key}


def _get_org(org_key: str) -> dict:
    row = db.query_one(
        "SELECT org_key, name, is_personal, retention_days, enabled, created_at"
        " FROM orgs WHERE org_key=%(k)s", {"k": org_key})
    if row is None:
        raise not_found("组织", org_key)
    return row


# ─────────────────────────── llm_endpoints ───────────────────────────

def upsert_endpoint(*, endpoint_key: str, base_url: str, model: str,
                    api_protocol: str = "anthropic", max_input_tokens: int | None = None,
                    enabled: bool = True, note: str | None = None) -> dict:
    """新增或改一个端点的**非敏感字段**。★ 不接受、不存储任何 token（§2.1）。
    改完调 `refresh_endpoints()`，让运行中的 LLM client 立刻用上新值（否则它缓存旧的）。"""
    endpoint_key = (endpoint_key or "").strip()
    if not endpoint_key:
        raise LibraryError(400, "bad_endpoint_key", "endpoint_key 不能为空")
    if not (base_url or "").strip():
        raise LibraryError(400, "bad_base_url", "base_url 不能为空")
    if not (model or "").strip():
        raise LibraryError(400, "bad_model", "model 不能为空")
    if api_protocol not in _VALID_PROTOCOLS:
        raise LibraryError(400, "bad_protocol",
                           f"api_protocol 只能是 {_VALID_PROTOCOLS}，收到 {api_protocol!r}")
    params = {"k": endpoint_key, "u": base_url.strip(), "m": model.strip(),
              "p": api_protocol, "t": max_input_tokens, "e": enabled,
              "n": (note or None)}
    db.execute(
        "INSERT INTO llm_endpoints (endpoint_key, base_url, model, api_protocol,"
        "                            max_input_tokens, enabled, note)"
        " VALUES (%(k)s,%(u)s,%(m)s,%(p)s,%(t)s,%(e)s,%(n)s)"
        " ON CONFLICT (endpoint_key) DO UPDATE SET"
        "   base_url=EXCLUDED.base_url, model=EXCLUDED.model, api_protocol=EXCLUDED.api_protocol,"
        "   max_input_tokens=EXCLUDED.max_input_tokens, enabled=EXCLUDED.enabled, note=EXCLUDED.note",
        params)
    _reload_llm_client()
    return _get_endpoint(endpoint_key)


def delete_endpoint(endpoint_key: str) -> dict:
    """删端点。★ 是当前 default_endpoint 或任一 task 的目标就拒——删了会让那条任务无处可发。"""
    _get_endpoint(endpoint_key)
    settings = get_settings()
    if settings.default_endpoint == endpoint_key:
        raise LibraryError(409, "endpoint_in_use",
                           f"{endpoint_key!r} 是默认端点，不能删；先在 config 里改 default_endpoint")
    users = [t for t, k in settings.task_endpoints.items() if k == endpoint_key]
    if users:
        raise LibraryError(409, "endpoint_in_use",
                           f"{endpoint_key!r} 仍被任务 {'、'.join(users)} 使用，不能删")
    db.execute("DELETE FROM llm_endpoints WHERE endpoint_key=%(k)s", {"k": endpoint_key})
    _reload_llm_client()
    return {"deleted": endpoint_key}


def _get_endpoint(endpoint_key: str) -> dict:
    row = db.query_one(
        "SELECT endpoint_key, base_url, model, api_protocol, max_input_tokens, enabled, note"
        " FROM llm_endpoints WHERE endpoint_key=%(k)s", {"k": endpoint_key})
    if row is None:
        raise not_found("端点", endpoint_key)
    return row


def _reload_llm_client() -> None:
    """端点变了让运行进程重新加载。★ 失败不阻断写入（DB 已是真相源，worker 下次也会重读）。"""
    try:
        from .core.llm import get_llm_client
        get_llm_client().refresh_endpoints()
    except Exception:  # noqa: BLE001
        log.exception("refresh_endpoints after endpoint change failed")
