"""重跑页服务层（§11 重跑页 → `/v1/replay`）。

★ **范围**：只读 `replay_runs` / `replay_actions` + 逐条/批量 apply + 丢弃。
  *生产*这些表的 shadow 重跑（拿新 `prompt_ver` 把 `conversation_log` 重新抽一遍、与现有
  记忆 diff 出 ADD/UPDATE/DELETE/NOOP）是 **P7**，不在这一页。这一页是「审阅 + 落地」那半。

apply 语义（§8 中断点 6：`replay_actions.applied` 逐条可续）：
  ADD    → `library.create_memory(proposed_content, **proposed_meta)`，actor='replay'
  UPDATE → `library.update_memory(target_memory_id, content=proposed_content, **meta)`
  DELETE → `library.delete_memory(target_memory_id)`（软删，留 memory_events 轨迹）
  NOOP   → 什么都不做，只标 applied
每 apply 一条就 `applied=true`；一个 run 全部 applied 后 `status→'applied'` + `finished_at`。

★ **幂等**：ADD 撞上「同桶同内容已存在」(duplicate)、DELETE 撞上「已是 deleted」(already_deleted)
  都视为*目标态已达成*，照样标 applied——这样批量 apply 崩在中途也能安全续跑（§8）。
★ ADD/UPDATE 要算 embedding，所以 apply 在 **API 层 503 拦截**（和 /memories 写路径同构）。

`proposed_meta` 约定（生产方 P7 填，这一页只消费）：可带
  scope / subject / role / keywords / why / cwd / org_key / project_key —— 缺的用 library 的默认。
"""

from __future__ import annotations

import logging

from . import library
from .core import db
from .library import LibraryError, not_found

log = logging.getLogger(__name__)

# proposed_meta 里会透传给 create_memory / update_memory 的键（其余忽略）
_CREATE_KEYS = ("scope", "subject", "role", "keywords", "why", "cwd", "org_key", "project_key")
_UPDATE_KEYS = ("role", "scope", "subject", "keywords", "why", "cwd")

# 这些 LibraryError code 表示「目标态已达成」，apply 视为成功（幂等续跑）
_ALREADY_DONE = {"duplicate", "already_deleted", "already_active"}


# ─────────────────────────── 读 ───────────────────────────

def list_runs() -> dict:
    """全部重跑 run（按创建时间倒序），每个带动作计数（总数/已 apply/待 apply + 按动作分类）。"""
    runs = db.query(
        "SELECT id, prompt_ver, scope_filter, status, created_at, finished_at"
        " FROM replay_runs ORDER BY created_at DESC, id DESC")
    counts = {r["run_id"]: r for r in db.query(
        "SELECT run_id,"
        "  count(*)::int AS total,"
        "  count(*) FILTER (WHERE applied)::int AS applied,"
        "  count(*) FILTER (WHERE NOT applied)::int AS pending,"
        "  count(*) FILTER (WHERE action='ADD')::int AS add,"
        "  count(*) FILTER (WHERE action='UPDATE')::int AS upd,"
        "  count(*) FILTER (WHERE action='DELETE')::int AS del,"
        "  count(*) FILTER (WHERE action='NOOP')::int AS noop"
        " FROM replay_actions GROUP BY run_id")}
    for run in runs:
        c = counts.get(run["id"], {})
        run["action_counts"] = {
            "total": c.get("total", 0), "applied": c.get("applied", 0),
            "pending": c.get("pending", 0),
            "by_action": {"ADD": c.get("add", 0), "UPDATE": c.get("upd", 0),
                          "DELETE": c.get("del", 0), "NOOP": c.get("noop", 0)},
        }
    return {"runs": runs}


def run_detail(run_id: int) -> dict:
    """一个 run 的全部动作，每条带上**目标记忆的当前内容/状态**，好让 UI 摊开 old→new diff。"""
    run = _get_run(run_id)
    actions = db.query(
        "SELECT id, action, target_memory_id, proposed_content, proposed_meta, applied"
        " FROM replay_actions WHERE run_id=%(r)s ORDER BY id", {"r": run_id})
    # 一次性把涉及的目标记忆捞回来（UPDATE/DELETE 要显示当前正文做对比）
    target_ids = [a["target_memory_id"] for a in actions if a["target_memory_id"] is not None]
    targets = {}
    if target_ids:
        rows = db.query(
            "SELECT id, content, status, scope, role FROM memories WHERE id = ANY(%(ids)s)",
            {"ids": target_ids})
        targets = {r["id"]: r for r in rows}
    for a in actions:
        t = targets.get(a["target_memory_id"])
        a["target_content"] = t["content"] if t else None
        a["target_status"] = t["status"] if t else None
        a["target_exists"] = t is not None
    run["actions"] = actions
    return run


# ─────────────────────────── apply / 丢弃 ───────────────────────────

def apply_run(run_id: int, *, action_ids: list[int] | None = None) -> dict:
    """逐条 apply 一个 run 里**未 applied** 的动作（给了 action_ids 就只 apply 那个子集）。

    ★ 撞 LibraryError 就停在那条、如实报告（已 apply 的不回滚——逐条可续，§8）；
      但「目标态已达成」类（duplicate/already_deleted）算成功，继续往下。
    """
    run = _get_run(run_id)
    if run["status"] == "discarded":
        raise LibraryError(409, "run_discarded", f"run #{run_id} 已丢弃，不能再 apply")
    if action_ids:
        rows = db.query(
            "SELECT id, action, target_memory_id, proposed_content, proposed_meta, applied"
            " FROM replay_actions WHERE run_id=%(r)s AND id = ANY(%(ids)s) AND NOT applied"
            " ORDER BY id", {"r": run_id, "ids": action_ids})
    else:
        rows = db.query(
            "SELECT id, action, target_memory_id, proposed_content, proposed_meta, applied"
            " FROM replay_actions WHERE run_id=%(r)s AND NOT applied ORDER BY id", {"r": run_id})

    applied, failed = 0, None
    for row in rows:
        try:
            _apply_one(row)
        except LibraryError as exc:
            if exc.code in _ALREADY_DONE:
                _mark_applied(row["id"])   # 目标态已达成，幂等地标 applied
                applied += 1
                continue
            failed = {"action_id": row["id"], "code": exc.code, "message": exc.message}
            break
        _mark_applied(row["id"])
        applied += 1

    status = _refresh_run_status(run_id)
    out = {"run_id": run_id, "applied": applied, "run_status": status}
    if failed:
        out["failed"] = failed
    return out


def discard_run(run_id: int) -> dict:
    """丢弃一个 run（status→'discarded'）。已 applied/已 discarded 的不能再丢。"""
    run = _get_run(run_id)
    if run["status"] in ("applied", "discarded"):
        raise LibraryError(409, "run_" + run["status"],
                           f"run #{run_id} 已是 {run['status']}，不能丢弃")
    db.execute("UPDATE replay_runs SET status='discarded', finished_at=now() WHERE id=%(r)s",
               {"r": run_id})
    return {"run_id": run_id, "status": "discarded"}


def _apply_one(row: dict) -> None:
    """把一条 replay_action 落到记忆库。actor='replay'，memory_events 留轨迹。"""
    action = row["action"]
    meta = row["proposed_meta"] or {}
    if action == "NOOP":
        return
    if action == "ADD":
        kwargs = {k: meta[k] for k in _CREATE_KEYS if k in meta}
        library.create_memory(content=row["proposed_content"] or "", actor="replay", **kwargs)
    elif action == "UPDATE":
        if row["target_memory_id"] is None:
            raise LibraryError(400, "bad_action", "UPDATE 缺 target_memory_id")
        kwargs = {k: meta[k] for k in _UPDATE_KEYS if k in meta}
        library.update_memory(row["target_memory_id"], content=row["proposed_content"],
                              actor="replay", **kwargs)
    elif action == "DELETE":
        if row["target_memory_id"] is None:
            raise LibraryError(400, "bad_action", "DELETE 缺 target_memory_id")
        library.delete_memory(row["target_memory_id"], actor="replay", note="replay apply")
    else:
        raise LibraryError(400, "bad_action", f"未知动作 {action!r}")


def _mark_applied(action_id: int) -> None:
    db.execute("UPDATE replay_actions SET applied=true WHERE id=%(a)s", {"a": action_id})


def _refresh_run_status(run_id: int) -> str:
    """全部动作都 applied 了就把 run 收成 'applied'（NOOP 也算 applied）。"""
    pending = db.query_one(
        "SELECT count(*)::int AS n FROM replay_actions WHERE run_id=%(r)s AND NOT applied",
        {"r": run_id})["n"]
    if pending == 0:
        db.execute(
            "UPDATE replay_runs SET status='applied', finished_at=now()"
            " WHERE id=%(r)s AND status <> 'applied'", {"r": run_id})
        return "applied"
    return _get_run(run_id)["status"]


def _get_run(run_id: int) -> dict:
    row = db.query_one(
        "SELECT id, prompt_ver, scope_filter, status, created_at, finished_at"
        " FROM replay_runs WHERE id=%(r)s", {"r": run_id})
    if row is None:
        raise not_found("重跑 run", run_id)
    return row
