"""§5.2 路由器 + §5.4 阈值。一个窗口一次 LLM 调用，判完就地分流。

分流去向（§5.4）：
    observation      → 无阈值，100% 进人工待办（`pending_items`），审过才落线索池
    atomic_fact      → confidence ≥ 0.85 进 `write_facts` job；不够的进待办
    discard          → confidence ≥ 0.95 只记 `route_decisions`；不够的进待办
                       （丢掉的东西捡不回来，所以 discard 也要严）
栏位匹配不出        → `pending_items(stage='section')`
LLM 输出不合 schema → `pending_items(stage='route', reason='llm_parse_failed')`，绝不静默丢（§2.2）
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from psycopg import Connection

from ..core import db
from ..core.config import get_settings
from ..core.llm import LLMError, LLMParseError, get_llm_client
from ..core.redact import redact
from ..prompts import route as route_prompt
from . import sections as section_matcher
from .windows import Row, Window, slice_windows

log = logging.getLogger(__name__)

# 实测（bench/bench_windows.py，2026-10-23 全量 26 会话）：不切的话每窗 token
# p99=14403、max=18924，都还在端点 32000 上限内。但 `estimate_tokens` 是估算，
# 按 12000 切一刀只多 8 个窗口（371→379），却能保证即使估算偏低一半也不撞上限。
TOKEN_BUDGET = 12000

# §3.3「若其覆盖的原始消息已入库，则降权避免重复抽取」。
# 实测 112 条压缩摘要**没有一条**是会话首行——摘要必然由它前面的消息生成，
# 而那些消息已经在库里、也已经被路由过了。所以"降权"在这里等于"不路由"，
# 直接省掉 30% 的 route 调用（371 → 259），且不丢信息。
# ★ 如果 P5 评测发现漏抽了 AI 自己踩的坑（那类藏在被两级筛丢掉的 thinking 里，
#   而摘要会把它捞回来），把这里改成 True 再跑一遍对比。
ROUTE_SUMMARIES = False

ROW_SQL = """
SELECT id, role, content, turn_no, coalesce(tokens_est, 0) AS tokens_est,
       org_key, project_key, cwd, git_branch
  FROM conversation_log
 WHERE source = %(source)s AND session_id = %(session)s
 ORDER BY id
"""


@dataclass
class RouteStats:
    sessions: int = 0
    windows: int = 0
    windows_skipped: int = 0
    calls: int = 0
    items: int = 0
    pending: int = 0
    facts: int = 0
    discarded: int = 0
    section_unmatched: int = 0
    cross_org: int = 0
    parse_failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    kinds: Counter = field(default_factory=Counter)

    _COUNTED = ("sessions", "windows", "windows_skipped", "calls", "items", "pending",
                "facts", "discarded", "section_unmatched", "cross_org", "parse_failed",
                "input_tokens", "output_tokens")

    def merge(self, other: "RouteStats") -> None:
        for name in self._COUNTED:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.seconds += other.seconds
        self.kinds.update(other.kinds)

    def as_dict(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        out["kinds"] = dict(self.kinds)
        out["seconds"] = round(self.seconds, 2)
        return out


@dataclass(frozen=True)
class SessionCtx:
    """一个会话的归属信息。`context` 是喂给模型的上下文块。"""

    session_id: str
    org_key: str | None
    project_key: str | None
    context: str


def _session_ctx(session_id: str, first_row: dict) -> SessionCtx:
    # ★ 这段上下文是"content 必须自包含"能落地的前提：模型得先知道自己在哪个仓库，
    #   才写得出"在 gds-agg 项目中…"而不是"该项目中…"。
    context = (f"组织：{first_row.get('org_key') or '未知'}\n"
               f"项目：{first_row.get('project_key') or '（未归属到具体项目）'}\n"
               f"目录：{first_row.get('cwd') or '未知'}\n"
               f"分支：{first_row.get('git_branch') or '未知'}")
    return SessionCtx(session_id=session_id, org_key=first_row.get("org_key"),
                      project_key=first_row.get("project_key"), context=context)


def _user_message(window: Window, ctx: SessionCtx) -> str:
    parts = [f"【会话上下文】\n{ctx.context}", f"【对话片段】\n{window.as_text()}"]
    if window.is_summary:
        parts.append("【注意】上面这段是上下文压缩摘要，不是刚发生的对话，"
                     "内容多半已经在别处记过了；只抽其中确实是新规矩/新事实的部分。")
    return "\n\n".join(parts)


def _insert_decision(conn: Connection, window: Window, item, ctx: SessionCtx) -> int:
    """返回 `route_decisions.id`——待办卡片要记住它，人工裁决时才能回写
    `human_output`/`overridden`（§5.7），否则 §2.3 的改判率永远算不出来。"""
    return conn.execute(
        "INSERT INTO route_decisions (source_msg, raw_snippet, prompt_ver, kind, confidence,"
        "                            model_output, org_key)"
        " VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING id",
        (window.anchor_id, window.rows[0].content[:500], route_prompt.PROMPT_VER,
         item.kind, item.confidence,
         json.dumps(item.model_dump(exclude_none=True), ensure_ascii=False), ctx.org_key),
    ).fetchone()["id"]


def _queue_for_review(conn: Connection, window: Window, item, ctx: SessionCtx,
                      reason: str, stats: RouteStats, decision_id: int) -> None:
    """§5.4：待办卡片**预选 observation**（偏向规矩侧），一眼就能改回 atomic_fact。

    偏向是安全的：误判成 observation 只是多审一次，后面还有汇总 + 审 diff 两道能拦；
    误判成 atomic_fact 则一条本该每次注入的规矩被静默存成零碎事实，永远发现不了。
    """
    stats.pending += 1
    text = item.content or window.as_text()[:500]
    matched = section_matcher.match(
        text, section_matcher.load_sections(conn, has_project=bool(ctx.project_key)))
    if matched is None:
        stats.section_unmatched += 1
        reason = f"{reason}+section_unmatched"
    elif matched.org_key and ctx.org_key and matched.org_key != ctx.org_key:
        # 会话归属 dida，却匹配到 personal 的栏位（local_env 之类）。不是错误——
        # 本机环境的事确实该进 personal 的栏位——但这是 §14 待审词表
        # （local_env vs build_env）最直接的观测信号，要报出来。
        stats.cross_org += 1

    suggestion = item.model_dump(exclude_none=True)
    suggestion.update({
        "kind": "observation",       # 预选，人一眼就能改回 model_kind
        "model_kind": item.kind,
        "decision_id": decision_id,
        "section_key": matched.section_key if matched else None,
        "section_score": matched.score if matched else None,
        "section_runner_up": matched.runner_up if matched else None,
    })
    # ★ ON CONFLICT (dedup_hash) DO NOTHING：job 被租约回收后重复执行时，
    #   同一素材不再排出第二套卡（decisions-log #70 承认的缺口）。
    #   stats.pending 仍照加——它统计的是"路由判定送审多少次"，不是"插了几行"。
    #   索引是部分唯一（WHERE dedup_hash IS NOT NULL），推断必须带上同样的谓词。
    conn.execute(
        "INSERT INTO pending_items (org_key, project_key, stage, raw_content, source_msg,"
        "                            suggestion, reason, prompt_ver, status, dedup_hash)"
        " VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'pending',%s)"
        " ON CONFLICT (dedup_hash) WHERE dedup_hash IS NOT NULL DO NOTHING",
        (ctx.org_key, ctx.project_key, "route" if matched else "section",
         text[:4000], window.anchor_id, json.dumps(suggestion, ensure_ascii=False),
         reason, route_prompt.PROMPT_VER,
         _dedup_hash(window.anchor_id, item.kind, text)),
    )


def _decide(conn: Connection, item, window: Window, ctx: SessionCtx,
            stats: RouteStats) -> dict | None:
    """按阈值判定一条。返回要进 `write_facts` 的 payload；None 表示不进（已记日志或已进待办）。"""
    stats.kinds[item.kind] += 1
    decision_id = _insert_decision(conn, window, item, ctx)

    if item.kind == "observation":
        # ★ 规矩类不设置信度阈值（#51）：confidence 在这一类上实测毫无区分度，
        #   判错也报 0.93–1.0，0.95 阈值一条也拦不住。全审是唯一能捞回来的办法。
        _queue_for_review(conn, window, item, ctx, "observation_needs_review", stats,
                          decision_id)
        return None

    if item.kind == "discard":
        # ★ discard **不进待办**（2026-10-23 真跑后改的）。原设计是"低于 0.95 就送人工，
        #   因为丢掉的素材不可恢复"，真跑 3 个窗口才发现这个前提是错的：
        #   ① discard 的 content 是空的，卡片只能退回整段窗口原文，一个窗口有几条 discard
        #      就有几张一模一样的卡（实测 6 条 discard 里 3 条完全重复）；
        #   ② 人一旦点"通过"，预选的 observation 会把**原始 transcript** 写进线索池；
        #   ③ 它并**不是**不可恢复的——route_decisions 留了 kind/confidence/model_output，
        #      conversation_log 是不可变真相源，prompt_ver 一升就全量重判（§2.3）。
        #   漏判的检查交给 §10 的人工抽样，不靠待办队列。
        stats.discarded += 1
        return None

    floor = get_settings().route_confidence_thresholds.get(item.kind)
    if floor is not None and item.confidence < floor:
        _queue_for_review(conn, window, item, ctx, f"below_threshold:{floor}", stats,
                          decision_id)
        return None

    stats.facts += 1
    return {"source_msg": window.anchor_id, **item.model_dump(exclude_none=True)}


def route_session(session_id: str, *, source: str = "claude-code",
                  conn: Connection | None = None, llm=None,
                  limit: int | None = None,
                  on_window: Callable[[int, int], None] | None = None) -> RouteStats:
    """把一个会话（游标之后的部分）跑完路由。

    ★ 事务边界（§5.5，EXPERT-REVIEW B1）：**LLM 调用全部发生在事务外**。
      旧实现把整个会话（实测 ≈8 分钟：9 窗口 × 53s/次）包在一个事务里，
      连接被 idle-in-transaction 占住，池上限才 8——几个并发会话就能把读路径
      的 400ms 预算拖垮。现在拆三段：
        ① 读（短连接）：游标 + 会话行 → 切窗口
        ② LLM（无事务）：逐窗口判定，结果先收集在内存
        ③ 写（一个短事务）：route_decisions + pending_items + 游标 + write_facts job
      "要么都成、要么重跑还是这批窗口"的重试语义由 ③ 保证：中途任何失败
      游标都不推进。② 抛 LLMError 时什么都没写，行为与旧版回滚一致。

    传 `conn` 时三段都用它、事务由调用方管（测试、CLI 的兼容路径）；
    缺省时自管。`on_window(第几个窗口, 窗口总数)` 在每个窗口开头回调一次，
    给调用方续租约、写进度的机会（见 worker._handle_route）。
    """
    client = llm or get_llm_client()
    stats = RouteStats(sessions=1)

    if conn is not None:
        prepared = _prepare(conn, session_id, source)
        if prepared is not None:
            ctx, todo = prepared
            outcomes = _call_llm(todo, ctx, client, stats, limit=limit, on_window=on_window)
            _write_outcomes(conn, source=source, ctx=ctx, stats=stats, outcomes=outcomes)
        return stats

    with db.connect() as reader:
        prepared = _prepare(reader, session_id, source)
        if prepared is None:
            return stats
        ctx, todo = prepared
        outcomes = _call_llm(todo, ctx, client, stats, limit=limit, on_window=on_window)
    with db.transaction() as tx:
        _write_outcomes(tx, source=source, ctx=ctx, stats=stats, outcomes=outcomes)
    return stats


def _prepare(conn: Connection, session_id: str,
             source: str) -> tuple[SessionCtx, list[Window]] | None:
    """段①：读游标与会话行，切出「游标之后」的待判窗口。无行返回 None。"""
    cursor = conn.execute(
        "SELECT last_log_id, prompt_ver FROM extraction_cursor WHERE source=%s AND session_id=%s",
        (source, session_id),
    ).fetchone()
    last_log_id = cursor["last_log_id"] if cursor else 0
    if cursor and cursor["prompt_ver"] != route_prompt.PROMPT_VER:
        # prompt 换版 ⇒ 全量重判。route_decisions 带 prompt_ver，两版判定并存，
        # §2.3 的改判率就是这么算出来的；memories 有 UNIQUE(source_msg, content_hash)
        # 兜着，重跑不会产生重复行。pending_items 的 dedup_hash 同样含 prompt_ver。
        log.info("prompt_ver %s → %s，会话 %s 从头重抽",
                 cursor["prompt_ver"], route_prompt.PROMPT_VER, session_id[:8])
        last_log_id = 0

    rows = conn.execute(ROW_SQL, {"source": source, "session": session_id}).fetchall()
    if not rows:
        return None
    ctx = _session_ctx(session_id, rows[0])
    parsed = [Row(id=r["id"], role=r["role"], content=r["content"],
                  turn_no=r["turn_no"], tokens_est=r["tokens_est"]) for r in rows]
    todo = [w for w in slice_windows(parsed, token_budget=TOKEN_BUDGET)
            if w.anchor_id > last_log_id]
    return ctx, todo


@dataclass
class _Outcome:
    """段② 一个窗口的 LLM 判定产物。

    `result=None 且 parse_error 非空` = 输出不合 schema（素材必须落待办，§2.2）；
    `skipped=True` = 压缩摘要窗口，不路由但要推进游标（§3.3）。
    """

    window: Window
    result: object | None = None
    parse_error: str | None = None
    skipped: bool = False


def _call_llm(todo: list[Window], ctx: SessionCtx, client, stats: RouteStats, *,
              limit: int | None,
              on_window: Callable[[int, int], None] | None) -> list[_Outcome]:
    """段②：逐窗口调 LLM，**不持有任何数据库事务**。"""
    outcomes: list[_Outcome] = []
    for idx, window in enumerate(todo, 1):
        if limit is not None and stats.calls >= limit:
            break  # 本次只跑这么多，下次从游标接着跑
        if on_window is not None:
            on_window(idx, len(todo))
        if window.is_summary and not ROUTE_SUMMARIES:
            stats.windows_skipped += 1
            outcomes.append(_Outcome(window, skipped=True))
            continue
        stats.windows += 1
        started = time.perf_counter()
        try:
            result = client.structured(
                "route",
                system=route_prompt.system_prompt(),
                messages=[{"role": "user", "content": _user_message(window, ctx)}],
                tool=route_prompt.TOOL,
                schema=route_prompt.RouteResult,
            )
        except LLMParseError as exc:
            # ★ 不能 raise：这一窗的素材会永久丢失（§2.2）。落到待办，人来兜。
            outcomes.append(_Outcome(window, parse_error=redact(str(exc))[:2000]))
            continue
        except LLMError:
            raise  # 传输失败交给队列重试；此时还没写库 ⇒ 游标不动，重跑还是这批窗口
        stats.calls += 1
        stats.seconds += time.perf_counter() - started
        outcomes.append(_Outcome(window, result=result))
    return outcomes


def _write_outcomes(conn: Connection, *, source: str, ctx: SessionCtx, stats: RouteStats,
                    outcomes: list[_Outcome]) -> None:
    """段③：把段② 的产物写库。调用方保证这里跑在一个短事务里。"""
    facts: list[dict] = []
    done_through = 0  # ★ 只在窗口**整个处理完**之后才推进；没进 outcomes 的窗口不动游标
    for oc in outcomes:
        if oc.skipped:
            done_through = oc.window.anchor_id
            continue
        if oc.result is None:
            stats.parse_failed += 1
            stats.pending += 1
            _insert_parse_failure(conn, oc.window, ctx, oc.parse_error)
            done_through = oc.window.anchor_id
            continue
        for item in oc.result.items:
            stats.items += 1
            payload = _decide(conn, item, oc.window, ctx, stats)
            if payload:
                facts.append(payload)
        done_through = oc.window.anchor_id

    _finish(conn, source=source, ctx=ctx, stats=stats, facts=facts, new_cursor=done_through)


def _insert_parse_failure(conn: Connection, window: Window, ctx: SessionCtx,
                          error: str | None) -> None:
    conn.execute(
        "INSERT INTO pending_items (org_key, project_key, stage, raw_content,"
        "                            source_msg, reason, prompt_ver, status, last_error,"
        "                            dedup_hash)"
        " VALUES (%s,%s,'route',%s,%s,'llm_parse_failed',%s,'pending',%s,%s)"
        " ON CONFLICT (dedup_hash) WHERE dedup_hash IS NOT NULL DO NOTHING",
        (ctx.org_key, ctx.project_key, window.as_text()[:4000],
         window.anchor_id, route_prompt.PROMPT_VER, error,
         _dedup_hash(window.anchor_id, "llm_parse_failed", window.as_text()[:4000])),
    )


def _dedup_hash(source_msg: int, kind: str, content: str) -> str:
    """待办卡的幂等键（EXPERT-REVIEW A3 / decisions-log #70）。

    租约被回收后同一 job 被执行两遍时，没有它同一批素材会排出两套待办卡。
    ★ 键里含 prompt_ver：换版全量重判（§2.3）时同一素材要能出新卡，
      挡掉的只是**同一版**判定下的重复消费。
    """
    raw = f"{route_prompt.PROMPT_VER}|{source_msg}|{kind}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _finish(conn: Connection, *, source: str, ctx: SessionCtx, stats: RouteStats,
            facts: list[dict], new_cursor: int) -> None:
    """写 job + 推游标，必须在同一个事务里：要么都成，要么下次重跑还是这批窗口。"""
    if not new_cursor:
        return
    if facts:
        from ..queue import jobs

        jobs.enqueue(
            "write_facts",
            {"session_id": ctx.session_id, "source": source,
             "prompt_ver": route_prompt.PROMPT_VER,
             # ★ 归属放在 payload 顶层而不是每条 item 里：一个会话只有一个 cwd，
             #   逐条重复存既浪费又给写入器留下"同批不同归属"这种不该存在的情况。
             "org_key": ctx.org_key, "project_key": ctx.project_key,
             "items": facts},
            org_key=ctx.org_key,
            dedup_key=f"write_facts:{source}:{ctx.session_id}:{new_cursor}",
            conn=conn,
        )
    conn.execute(
        "INSERT INTO extraction_cursor (source, session_id, last_log_id, prompt_ver, updated_at)"
        " VALUES (%s,%s,%s,%s,now())"
        " ON CONFLICT (source, session_id) DO UPDATE"
        "   SET last_log_id=EXCLUDED.last_log_id, prompt_ver=EXCLUDED.prompt_ver,"
        "       updated_at=now()",
        (source, ctx.session_id, new_cursor, route_prompt.PROMPT_VER),
    )


def _sessions_with_new_material(session_ids: list[str], source: str) -> list[str]:
    """只挑出「游标之后确实还有新 conversation_log 行」的会话。

    ★ `extraction_cursor.last_log_id` 是"增量消费到哪"的游标。从没抽过的会话没有
      cursor 行，`COALESCE(...,0)` 把它当 0 ⇒ 全部行都算新素材（正确：新会话就该全抽）。
      已到顶的历史会话（cursor=max id）这里被滤掉，自动扫描就不会再给它们排空跑 job。
    """
    if not session_ids:
        return []
    rows = db.query(
        """
        SELECT DISTINCT cl.session_id
          FROM conversation_log cl
          LEFT JOIN extraction_cursor ec
                 ON ec.source = cl.source AND ec.session_id = cl.session_id
         WHERE cl.source = %s
           AND cl.session_id = ANY(%s)
           AND cl.id > COALESCE(ec.last_log_id, 0)
        """,
        (source, session_ids))
    return [r["session_id"] for r in rows]


def queue_sessions(session_ids: list[str], *, source: str = "claude-code",
                   only_new: bool = False) -> dict:
    """给这批会话各排一个 `route` job。

    `dedup_key` 用 `route:{source}:{session_id}`，所以反复排是幂等的——
    队列里已经有同一个会话的活 job 就返回 None，不会排两遍。

    ★ `only_new=True`（Stop hook 的自动增量路径用）先滤掉"游标之后没有新素材"的会话。
      worker 冷启动第一次 scan 会把所有 transcript 文件都当"变了"（(size,mtime) 缓存空），
      不滤的话全部历史会话都会被重新排一遍——即便 route 因游标到顶而空跑，也白占队列、
      把 jobs 表刷脏，看着像在重跑历史。滤过后自动扫描才是真正的"仅增量"。
      手动回填（`queue_all_sessions`）不走这个开关：它要强制全排，配合先重置游标来重抽。
    """
    from ..queue import jobs

    if only_new:
        session_ids = _sessions_with_new_material(session_ids, source)
    queued = skipped = 0
    for session_id in session_ids:
        job_id = jobs.enqueue(
            "route", {"session_id": session_id, "source": source},
            dedup_key=f"route:{source}:{session_id}")
        if job_id is None:
            skipped += 1
        else:
            queued += 1
    return {"sessions": len(session_ids), "queued": queued, "already_queued": skipped}


def queue_all_sessions(*, source: str = "claude-code",
                       limit_sessions: int | None = None) -> dict:
    """★ 全量回填走这条路：**每会话排一个 `route` job，让后台 worker 慢慢抽**。

    不能塞进同步 CLI：真跑实测单次 route ≈53s（thinking ON），259 个窗口串行
    就是 ≈3.8 小时纯 LLM 时间，命令行会挂在那儿动不了（C.9）。
    """
    session_ids = [
        r["session_id"] for r in db.query(
            "SELECT DISTINCT session_id FROM conversation_log WHERE source=%s"
            " ORDER BY session_id", (source,))
    ]
    if limit_sessions:
        session_ids = session_ids[:limit_sessions]
    return queue_sessions(session_ids, source=source)


def route_all(*, source: str = "claude-code", limit_sessions: int | None = None,
              limit_windows: int | None = None, only_session: str | None = None,
              llm=None) -> RouteStats:
    """逐会话跑，**每个会话的写入各自一个短事务**：一个会话炸了不影响已经落库的
    其它会话。LLM 调用在事务外（见 `route_session` 的三段式说明）。"""
    total = RouteStats()
    if only_session:
        session_ids = [only_session]
    else:
        session_ids = [
            r["session_id"] for r in db.query(
                "SELECT DISTINCT session_id FROM conversation_log WHERE source=%s"
                " ORDER BY session_id", (source,))
        ]
    if limit_sessions:
        session_ids = session_ids[:limit_sessions]

    client = llm or get_llm_client()
    client.refresh_endpoints()
    for session_id in session_ids:
        try:
            stats = route_session(session_id, source=source, llm=client, limit=limit_windows)
        except Exception as exc:  # noqa: BLE001 — 单会话失败不能拖垮整批
            log.error("会话 %s 路由失败，本会话未写入（游标未推进，重跑即可）：%s",
                      session_id[:8], redact(str(exc))[:300])
            continue
        total.merge(stats)
        log.info("会话 %s：窗口 %d / 判定 %d / 待办 %d / 事实 %d",
                 session_id[:8], stats.windows, stats.items, stats.pending, stats.facts)
    return total
