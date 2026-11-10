"""记忆库的写入与纠错（§7.1 的 `/memories*`、§7.2 的 `memory_note` / `memory_correct`）。

★ **HTTP 层和 MCP 层都只是这里的薄适配器**，业务规则一律写在本模块：
  归属推导、scope 的三条 CHECK 前置校验、改 content 必须重算 embedding、
  每次变更都落 `memory_events`（否则"曾经以为 X 后来发现是 Y"这条轨迹就断了）。

★ **纠错不写否决记录**（推翻 §7.2 原文，理由见 decisions-log #77）：
  `polarity='negative'` 只拦 observation 入池（§4.3.2 比对②），**拦不住**一条事实被重新
  写进 `memories`——拿它来防复活是接错了线，反而会误伤同栏位以后所有正经的规矩提案（#67）。
  真正耐久的是"改内容"这条路：UPDATE 之后那行还是 `active`，四动作写入器下次能看见它、
  会 NOOP 或再 UPDATE。整条否掉又不给替代的那种，靠 P7 用纠错结果改进抽取 prompt 来收敛。
"""

from __future__ import annotations

import logging
from typing import Any

from .core import db
from .core.embedding import get_embedding_service
from .core.hashing import content_hash
from .retrieve import keywords as kw
from .retrieve import recall as recall_mod

log = logging.getLogger(__name__)

_LIST_COLUMNS = ("id, scope, org_key, project_key, subject, role, content, keywords, why,"
                 " status, mention_count, revision_count, created_at, updated_at, last_seen_at")

_VALID_ROLES = ("profile", "preference", "pointer", "fact")
_VALID_SCOPES = ("user", "org", "project")
_VALID_SUBJECTS = ("user", "project")
_VALID_FEEDBACK = ("corrected", "useless", "useful")


class LibraryError(Exception):
    """能被翻译成 4xx 的业务错误。★ 别拿它装 DB 异常——那些要走 redact（§2.1）。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_found(what: str, identifier: Any) -> LibraryError:
    return LibraryError(404, "not_found", f"{what} {identifier} 不存在")


# ─────────────────────────── 归属与 scope 校验 ───────────────────────────

def resolve_bucket(*, cwd: str | None, org_key: str | None, project_key: str | None,
                   scope: str) -> tuple[str | None, str | None]:
    """把 (cwd, 显式 org/project, scope) 收敛成合法的 (org_key, project_key)。

    ★ 三条 CHECK 必须在**入库前**挡住，否则 psycopg 抛的是 IntegrityViolation → 500，
      而用户看到的该是 400：
        scope='user'    ⇒ org_key 和 project_key 都必须 NULL（记忆跟着人走，不分区）
        scope='org'     ⇒ org_key 必填，project_key 必须 NULL
        scope='project' ⇒ 两个都必填（§6.1 的双保险：project 分支也要带 org_key）
    """
    if scope not in _VALID_SCOPES:
        raise LibraryError(400, "bad_scope", f"scope 只能是 {_VALID_SCOPES}，收到 {scope!r}")

    if org_key is None or (scope == "project" and project_key is None):
        attribution = recall_mod.attribute(cwd)
        org_key = org_key or attribution.org_key
        if scope == "project" and project_key is None:
            project_key = attribution.project_key

    if scope == "user":
        return None, None
    if scope == "org":
        if not org_key:
            raise LibraryError(400, "missing_org", "scope='org' 需要 org_key，或给一个能判定的 cwd")
        return org_key, None
    if not org_key or not project_key:
        raise LibraryError(400, "missing_project",
                           "scope='project' 需要 org_key + project_key，或给一个能判定的 cwd")
    return org_key, project_key


def _check_subject(scope: str, subject: str) -> str:
    if subject not in _VALID_SUBJECTS:
        raise LibraryError(400, "bad_subject", f"subject 只能是 {_VALID_SUBJECTS}，收到 {subject!r}")
    # ★ CHECK 是 NOT (scope='user' AND subject='project')；反过来（project 范围讲用户的事）合法
    if scope == "user" and subject == "project":
        raise LibraryError(400, "bad_subject", "scope='user' 时 subject 只能是 'user'")
    return subject


# ─────────────────────────── 查询 ───────────────────────────

def list_memories(*, org_key: str | None = None, project_key: str | None = None,
                  scope: str | None = None, subject: str | None = None,
                  role: str | None = None, status: str | None = "active",
                  q: str | None = None, include_all_scopes: bool = False,
                  limit: int = 50, offset: int = 0) -> dict:
    """分页列表。★ **不返回 embedding**（1024 维浮点，列表页要它没用还费带宽）。

    `include_all_scopes=False`（默认）时按 §6.1 的可见性裁剪：user 级穿透 +
    本 org + 本 project。传 True 是给 UI/MCP 的"浏览全库"用的，那条路不做注入。
    """
    where: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status:
        where.append("status = %(status)s")
        params["status"] = status
    if scope:
        where.append("scope = %(scope)s")
        params["scope"] = scope
    if subject:
        where.append("subject = %(subject)s")
        params["subject"] = subject
    if role:
        where.append("role = %(role)s")
        params["role"] = role
    if not include_all_scopes:
        where.append("""( scope = 'user'
                     OR (scope = 'org'     AND org_key = %(org)s)
                     OR (scope = 'project' AND org_key = %(org)s AND project_key = %(proj)s) )""")
        params["org"] = org_key
        params["proj"] = project_key
    else:
        if org_key:
            where.append("(org_key = %(org)s OR scope = 'user')")
            params["org"] = org_key
    if q:
        # 只读浏览用的粗筛：正文 ILIKE 或关键词命中。**不是**语义检索，那是 memory_recall
        where.append("""( content ILIKE %(like)s
                     OR EXISTS (SELECT 1 FROM unnest(keywords) w WHERE w ILIKE %(like)s) )""")
        params["like"] = f"%{q}%"

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.query_one(f"SELECT count(*) AS n FROM memories{clause}", params)["n"]
    rows = db.query(f"SELECT {_LIST_COLUMNS} FROM memories{clause}"
                    f" ORDER BY updated_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
                    params)
    return {"total": total, "limit": limit, "offset": offset, "items": rows}


def get_memory(memory_id: int) -> dict:
    row = db.query_one(f"SELECT {_LIST_COLUMNS} FROM memories WHERE id = %(id)s",
                       {"id": memory_id})
    if row is None:
        raise not_found("记忆", memory_id)
    return row


def list_events(memory_id: int, *, limit: int = 100) -> list[dict]:
    get_memory(memory_id)      # 顺带把 404 判掉，免得返回一个空列表让人以为"没有历史"
    return db.query(
        "SELECT id, action, old_content, new_content, actor, source_msg, created_at"
        "  FROM memory_events WHERE memory_id = %(id)s ORDER BY id DESC LIMIT %(limit)s",
        {"id": memory_id, "limit": limit})


# ─────────────────────────── 写入 ───────────────────────────

def create_memory(*, content: str, scope: str = "project", subject: str | None = None,
                  role: str = "fact", keywords: list[str] | None = None,
                  why: str | None = None, cwd: str | None = None,
                  org_key: str | None = None, project_key: str | None = None,
                  actor: str = "human") -> dict:
    content = (content or "").strip()
    if not content:
        raise LibraryError(400, "empty_content", "content 不能为空")
    if role not in _VALID_ROLES:
        raise LibraryError(400, "bad_role", f"role 只能是 {_VALID_ROLES}，收到 {role!r}")
    if subject is None:
        subject = "user" if scope == "user" else "project"
    subject = _check_subject(scope, subject)
    org, project = resolve_bucket(cwd=cwd, org_key=org_key, project_key=project_key, scope=scope)

    if keywords is None:
        keywords = kw.extract(content)
    # ★ embedding 在事务**外**算：单条 ~240ms，没必要占着连接（§5.5 的同一条纪律）
    embedding = get_embedding_service().encode_one(content)
    digest = content_hash(content)

    with db.transaction() as conn:
        # ★ 手工新增没有 source_msg，而 UNIQUE(source_msg, content_hash) 里的 NULL 互不相等
        #   ⇒ 数据库层面拦不住重复。这里自己按"同桶同内容且还活着"判一次，回 409（§7.1 约定）。
        dup = conn.execute(
            "SELECT id FROM memories WHERE content_hash = %s AND status = 'active'"
            "  AND scope = %s AND subject = %s AND role = %s"
            "  AND org_key IS NOT DISTINCT FROM %s AND project_key IS NOT DISTINCT FROM %s",
            (digest, scope, subject, role, org, project)).fetchone()
        if dup is not None:
            raise LibraryError(409, "duplicate",
                               f"同桶里已经有一条内容相同的活跃记忆 #{dup['id']}")

        row = conn.execute(
            f"""INSERT INTO memories (scope, org_key, project_key, subject, role, content,
                                      content_hash, keywords, why, embedding, prompt_ver)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual') RETURNING {_LIST_COLUMNS}""",
            (scope, org, project, subject, role, content, digest, keywords, why, embedding),
        ).fetchone()
        _event(conn, row["id"], "ADD", None, content, actor=actor)
    log.info("memory #%s created by %s (%s/%s)", row["id"], actor, scope, role)
    return dict(row)


def update_memory(memory_id: int, *, content: str | None = None, role: str | None = None,
                  scope: str | None = None, subject: str | None = None,
                  keywords: list[str] | None = None, why: str | None = None,
                  cwd: str | None = None, actor: str = "human") -> dict:
    """改 content/role/scope/keywords。★ 改了 content 就**必须**重算 embedding，
    否则向量还是旧意思的，召回会命中一条正文对不上的记忆——这种错查起来极其费劲。"""
    old = get_memory(memory_id)
    new_content = content.strip() if content is not None else old["content"]
    if not new_content:
        raise LibraryError(400, "empty_content", "content 不能为空")
    new_role = role or old["role"]
    if new_role not in _VALID_ROLES:
        raise LibraryError(400, "bad_role", f"role 只能是 {_VALID_ROLES}，收到 {new_role!r}")
    new_scope = scope or old["scope"]
    new_subject = _check_subject(new_scope, subject or old["subject"])
    new_keywords = keywords if keywords is not None else (
        kw.extract(new_content) if new_content != old["content"] else old["keywords"])

    if new_scope != old["scope"] or new_subject != old["subject"]:
        # 换桶 = 换检索分区，org/project 必须跟着重算，否则会留下 §6.1 要防的串台
        org, project = resolve_bucket(cwd=cwd, org_key=old["org_key"],
                                      project_key=old["project_key"], scope=new_scope)
    else:
        org, project = old["org_key"], old["project_key"]

    embedding = (get_embedding_service().encode_one(new_content)
                 if new_content != old["content"] else None)

    with db.transaction() as conn:
        # ★ 全用位置参数：psycopg3 不允许 `%s` 和 `%(name)s` 混在同一条语句里
        row = conn.execute(
            f"""UPDATE memories SET content=%s, content_hash=%s, keywords=%s, role=%s,
                        scope=%s, subject=%s, org_key=%s, project_key=%s,
                        why=coalesce(%s, why),
                        embedding=coalesce(%s::vector, embedding),
                        revision_count=revision_count + %s, updated_at=now()
                 WHERE id=%s RETURNING {_LIST_COLUMNS}""",
            (new_content, content_hash(new_content), new_keywords, new_role, new_scope,
             new_subject, org, project, why, embedding,
             1 if new_content != old["content"] else 0, memory_id),
        ).fetchone()
        _event(conn, memory_id, "UPDATE", old["content"], new_content, actor=actor)
    return dict(row)


def delete_memory(memory_id: int, *, note: str | None = None, actor: str = "human",
                  restore: bool = False) -> dict:
    """软删（`status='deleted'`）。★ 不真删：`conversation_log` 不可变、`memory_events`
    留着轨迹，删掉一行会让"这条以前存在过"变成无法追溯的事。"""
    old = get_memory(memory_id)
    target = "active" if restore else "deleted"
    if old["status"] == target:
        raise LibraryError(409, "already_" + target, f"记忆 #{memory_id} 已经是 {target}")
    with db.transaction() as conn:
        row = conn.execute(
            f"UPDATE memories SET status=%s, updated_at=now() WHERE id=%s"
            f" RETURNING {_LIST_COLUMNS}", (target, memory_id)).fetchone()
        _event(conn, memory_id, "RESTORE" if restore else "DELETE",
               old["content"], note or old["content"], actor=actor)
    return dict(row)


def correct_memory(memory_id: int, *, correction: str | None = None, note: str | None = None,
                   actor: str = "model") -> dict:
    """§7.2 的 `memory_correct`：给了替代文本就 UPDATE，没给就整条软删。

    ★ 两种情况都写 `memory_feedback(kind='corrected')`——那是 §10 里"记忆准确性"的
      唯一真实信号，也是 P7 拿去做 改进 few-shot 的原料。
    """
    correction = (correction or "").strip()
    if correction:
        row = update_memory(memory_id, content=correction, actor=actor)
        action = "updated"
    else:
        row = delete_memory(memory_id, note=note, actor=actor)
        action = "deleted"
    add_feedback(memory_id, kind="corrected", note=note)
    return {"memory_id": memory_id, "action": action, "status": row["status"],
            "content": row["content"]}


def add_feedback(memory_id: int, *, kind: str, note: str | None = None,
                 recall_log_id: int | None = None) -> dict:
    if kind not in _VALID_FEEDBACK:
        raise LibraryError(400, "bad_kind", f"kind 只能是 {_VALID_FEEDBACK}，收到 {kind!r}")
    get_memory(memory_id)
    row = db.query_one(
        "INSERT INTO memory_feedback (memory_id, kind, note, recall_log_id)"
        " VALUES (%(id)s, %(kind)s, %(note)s, %(log)s) RETURNING id, kind, created_at",
        {"id": memory_id, "kind": kind, "note": note, "log": recall_log_id})
    return dict(row)


def _event(conn, memory_id: int, action: str, old: str | None, new: str | None,
           *, actor: str, source_msg: int | None = None) -> None:
    conn.execute(
        "INSERT INTO memory_events (memory_id, action, old_content, new_content, source_msg, actor)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (memory_id, action, old, new, source_msg, actor))
