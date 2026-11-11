"""P1 摄取编排：扫 transcript → 过滤（§3）→ 归属（§5.1）→ 落 `conversation_log`。

★ **幂等靠 `UNIQUE(source, src_uuid)` + `ON CONFLICT DO NOTHING`**（§3.4）。
transcript 每行自带 uuid，所以重扫、崩溃重放、watcher 重入全部天然幂等，
不需要 `msg_index` 计数，也不需要额外的状态表。

★ **全量重扫而不是增量 tail**：38709 行 / 167MB 解析一遍约几秒，换来的是
`turn_no`（窗口抽取要用）在任何时候都完全确定。未改动的文件按 (size, mtime) 跳过，
所以 watcher 反复调 `scan_once()` 的常态开销只是 73 次 stat。

★ **这里不写 `extraction_cursor`**：那张表记的是"抽取消费到哪了"，而 `prompt_ver` 是 NOT NULL，
P1 还没有任何 prompt 版本可言，现在建行只能填占位符。留给 P2 第一次抽取时按真实版本建。
"""

from __future__ import annotations

import collections
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from psycopg import sql

from ..core import db
from . import filters
from .attribution import Attribution, Resolver, normalize_cwd
from .transcripts import (DEFAULT_ROOT, SOURCE, discover, estimate_tokens,
                          iter_records, parse_ts)

log = logging.getLogger(__name__)

BATCH = 500

_COLUMNS = ("source", "src_uuid", "session_id", "org_key", "project_key", "cwd",
            "git_branch", "role", "content", "turn_no", "tokens_est", "msg_ts")


@dataclass
class ScanStats:
    files: int = 0
    files_skipped: int = 0
    records: int = 0
    candidates: int = 0
    inserted: int = 0
    no_key: int = 0  # 过了过滤但缺 sessionId/uuid ⇒ 没幂等键，跳过（非零说明格式变了）
    dry_run: bool = False
    roles: collections.Counter = field(default_factory=collections.Counter)
    orgs: collections.Counter = field(default_factory=collections.Counter)
    sessions: set[str] = field(default_factory=set)
    cwds: set[str] = field(default_factory=set)
    default_cwds: set[str] = field(default_factory=set)  # 落到 personal 默认桶的目录

    @property
    def conflicts(self) -> int:
        """重复扫描时"候选 - 实际新增"就是被幂等键挡掉的行数。空跑下无意义。"""
        return 0 if self.dry_run else self.candidates - self.inserted

    @property
    def ratio(self) -> float:
        return self.candidates / self.records if self.records else 0.0

    def as_dict(self) -> dict:
        out = {
            "files": self.files, "files_skipped": self.files_skipped,
            "records": self.records, "candidates": self.candidates,
            "no_key": self.no_key, "ratio": round(self.ratio, 4),
            "roles": dict(self.roles), "orgs": dict(self.orgs),
            "sessions": len(self.sessions), "cwds": len(self.cwds),
            "default_cwds": sorted(self.default_cwds),
        }
        if not self.dry_run:
            out["inserted"] = self.inserted
            out["conflicts"] = self.conflicts
        return out


def _insert_batch(conn, rows: list[tuple]) -> int:
    """一条多值 INSERT，`RETURNING id` 的行数就是真正新增的行数。

    ★ 用 `executemany` 拿不到每行的结果，就没法区分"新增"和"已存在"，
    而这两个数的差正是幂等性的验收指标。
    """
    if not rows:
        return 0
    values = sql.SQL(",").join([sql.SQL("(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")] * len(rows))
    stmt = sql.SQL("INSERT INTO conversation_log ({cols}) VALUES {vals}"
                   " ON CONFLICT (source, src_uuid) DO NOTHING RETURNING id").format(
        cols=sql.SQL(",").join(map(sql.Identifier, _COLUMNS)), vals=values)
    flat = [v for row in rows for v in row]
    return len(conn.execute(stmt, flat).fetchall())


def scan_once(
    root: Path | str = DEFAULT_ROOT,
    *,
    include_subagents: bool = False,
    include_forks: bool = False,
    write: bool = True,
    unchanged: dict[str, tuple[int, float]] | None = None,
) -> ScanStats:
    """扫一遍并落库。`unchanged` 是调用方持有的 (size, mtime) 缓存，跨次调用复用以跳过没动的文件。

    `include_forks` 默认 False：fork 影子副本的 uuid 全部被重写、内容与父会话高度重复，
    入库会绕过 `UNIQUE(source, src_uuid)` 幂等、给 router 排出成批重复待审卡
    （decisions-log #110，详见 transcripts 模块 docstring）。

    `write=False` 是空跑：过滤和归属照做，但不写 `conversation_log`，
    用来在动数据库之前把留存数字和 bench 基线（§13 P1）对一遍。
    ★ 空跑仍会写 `workspace_map`——那是归属结果的缓存，不写的话每个目录下次还得再调一遍
    `git remote`。它是纯派生数据，删掉无损失。
    """
    stats = ScanStats(dry_run=not write)
    cache = unchanged if unchanged is not None else {}
    resolver = Resolver()
    attributions: dict[str, Attribution] = {}

    for item in discover(root):
        if item.is_subagent and not include_subagents:
            continue
        if item.is_fork and not include_forks:
            continue
        try:
            st = os.stat(item.path)
        except OSError:
            continue
        stats.files += 1
        stamp = (st.st_size, st.st_mtime)
        if cache.get(item.path) == stamp:
            stats.files_skipped += 1
            continue

        rows: list[tuple] = []
        turn_by_session: collections.Counter = collections.Counter()
        sessions_in_file: set[str] = set()
        cwds_in_file: set[str] = set()

        def flush() -> None:
            if rows and write:
                with db.transaction() as conn:
                    stats.inserted += _insert_batch(conn, rows)
            rows.clear()

        for record in iter_records(item.path):
            stats.records += 1
            parsed = filters.parse_record(record)
            if parsed is None:
                continue

            session_id = record.get("sessionId") or ""
            src_uuid = record.get("uuid") or ""
            # ★ 没有 uuid 就没法做幂等键，硬塞空串会让这些行互相冲突、静默丢掉，
            #   所以宁可跳过并计数——数字非零说明 transcript 格式变了，得回来处理。
            if not session_id or not src_uuid:
                stats.no_key += 1
                continue

            cwd = normalize_cwd(record.get("cwd"))
            attr = attributions.get(cwd)
            if attr is None:
                attr = attributions[cwd] = resolver.resolve(cwd)

            stats.candidates += 1
            stats.roles[parsed.role] += 1
            stats.orgs[attr.org_key] += 1
            sessions_in_file.add(session_id)
            if cwd:
                cwds_in_file.add(cwd)
            turn_by_session[session_id] += 1
            rows.append((
                SOURCE, src_uuid, session_id,
                attr.org_key, attr.project_key, cwd or None, record.get("gitBranch"),
                parsed.role, parsed.content, turn_by_session[session_id],
                estimate_tokens(parsed.content), parse_ts(record),
            ))
            if len(rows) >= BATCH:
                flush()

        flush()
        stats.sessions |= sessions_in_file
        stats.cwds |= cwds_in_file
        # ★ 空跑不能写缓存：文件并没被消费，写了会让紧接着的真扫描把 73 个文件全跳过。
        if write:
            cache[item.path] = stamp

    stats.default_cwds = {
        c for c, a in attributions.items() if c and a.resolved_by == "default"
    }
    log.info("scan done: %s", stats.as_dict())
    return stats


# ★ 跨调用复用的 (size, mtime) 缓存。**只在常驻进程里有意义**：
#   全量解析 195MB 要 15s，而 Stop hook 每轮结束都会触发一次扫描。
#   有了它，第二次以后只是几十次 `os.stat`（§13 P1）。
_UNCHANGED: dict[str, tuple[int, float]] = {}


def scan_and_queue(*, include_subagents: bool = False) -> dict:
    """扫一遍 transcript → 给**有新素材的会话**各排一个 `route` job。

    这是"对话结束 → 记忆落库"那条闭环的入口：Stop hook 打一下 `/v1/ingest/scan`，
    这里排队，worker 慢慢抽。★ 扫描本身不进 hook 的 400ms 预算——hook 只管发请求，
    拿到 202 就走，真正的活在队列里干。
    """
    from ..pipeline import router

    stats = scan_once(include_subagents=include_subagents, unchanged=_UNCHANGED)
    # ★ only_new=True：只给"游标之后有新素材"的会话排 route。冷启动首扫会把所有历史
    #   文件都当变了，不滤就会把全部历史会话重排一遍（详见 router.queue_sessions）。
    queued = router.queue_sessions(sorted(stats.sessions), only_new=True)
    return {**stats.as_dict(), "route_queued": queued["queued"],
            "route_already_queued": queued["already_queued"]}
