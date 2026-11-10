"""指标服务层（plan §10 的指标表 → `GET /v1/metrics`）。

★ 纯 SQL 聚合，不碰模型、不需要 503 拦截。和 `attribution.py`/`living_doc.py` 同构：
  业务规则在这儿，`app/api/metrics.py` 只是薄适配器。

指标分三类，返回体里分开标注，别让 UI 把"待测"当成"0"：
- **可直接算**：挂起率、改判率、p50/p95 延迟、纠错次数、空 section 数、栏位待裁决率、
  规矩类人审吞吐——都是 count / percentile，一条 SQL 出数。
- **只给候选池**：规矩漏判率。它本质是"人工抽样看有多少本该是 observation"，
  自动算不出比率，只能把含规范性措辞的 active 记忆捞出来给人抽（§10 唯一手段）。
- **待测**：hit@5 / MRR@10。要 `eval_recall_cases` + 召回 runner 跑一遍才有数；
  这里只报用例条数，0 条就明确标 `待测`，绝不写"暂定 X"（§10 方法论硬规矩）。
"""

from __future__ import annotations

import logging

from .core import db
from .core.config import get_settings

log = logging.getLogger(__name__)

# §10「规矩漏判率」的规范性措辞——含这些词的 active 记忆，本该被路由成 observation
# 走人审，若被误判成 atomic_fact 就静默入库、永不注入。这是唯一能捞回它们的口子。
_NORMATIVE_WORDS = ("必须", "禁止", "不要", "一律", "每次", "不允许", "应该")


def _org_clause(column: str, org_key: str | None, params: dict) -> str:
    if not org_key:
        return ""
    params["org"] = org_key
    return f" AND {column} = %(org)s"


def overview(*, org_key: str | None = None, latency_window_days: int = 30,
             throughput_window_days: int = 14, sample_limit: int = 30) -> dict:
    """一次把 §10 的指标全算出来。`org_key` 给了就按分区裁剪（route_decisions 等表都有这列）。"""
    settings = get_settings()
    thresholds = settings.route_confidence_thresholds

    return {
        "org_key": org_key,
        "suspend_rate": _suspend_rate(org_key),
        "override_rate": _override_rate(org_key, thresholds),
        "rule_miss": _rule_miss_candidates(org_key, sample_limit),
        "latency": _latency(org_key, latency_window_days),
        "corrections": _corrections(org_key),
        "empty_sections": _empty_sections(org_key),
        "section_pending_rate": _section_pending_rate(org_key),
        "review_throughput": _review_throughput(org_key, throughput_window_days),
        "recall_eval": _recall_eval(org_key),
        "llm_usage": llm_usage(),
    }


# ─────────────────── LLM token 消耗（llm_usage 表） ───────────────────

def llm_usage(*, today_only: bool = False) -> dict:
    """每次 LLM 调用一行的审计表 → 消耗汇总 + 分任务明细 + 队列进度。

    ★ 不按 org 裁剪：`llm_usage` 记的是"网关烧了多少 token"，是全局成本视角，
      一次 route 调用服务整个窗口，拆不到分区头上。
    """
    where = "WHERE created_at >= date_trunc('day', now())" if today_only else ""
    total = db.query_one(f"""
        SELECT count(*)::int                                   AS calls,
               count(*) FILTER (WHERE NOT ok)::int             AS failed,
               coalesce(sum(input_tokens), 0)::bigint          AS input_tokens,
               coalesce(sum(output_tokens), 0)::bigint         AS output_tokens,
               coalesce(sum(input_tokens + output_tokens), 0)::bigint AS total_tokens,
               round(avg(latency_ms) FILTER (WHERE ok))::float  AS avg_ms
          FROM llm_usage {where}
    """)
    by_task = db.query(f"""
        SELECT task, count(*)::int AS calls,
               sum(input_tokens)::bigint  AS input_tokens,
               sum(output_tokens)::bigint AS output_tokens,
               sum(input_tokens + output_tokens)::bigint AS total_tokens,
               round(avg(latency_ms) FILTER (WHERE ok))::float AS avg_ms,
               count(*) FILTER (WHERE NOT ok)::int AS failed
          FROM llm_usage {where}
         GROUP BY task ORDER BY sum(input_tokens + output_tokens) DESC
    """)
    queue = {r["status"]: r["n"] for r in db.query(
        "SELECT status, count(*)::int AS n FROM jobs GROUP BY status")}
    # 近 24 小时逐小时 token 柱状图序列（前端手画 SVG，不引图表库）。
    series = db.query("""
        SELECT to_char(date_trunc('hour', created_at), 'MM-DD HH24:00') AS hour,
               sum(input_tokens + output_tokens)::bigint AS tokens,
               count(*)::int AS calls
          FROM llm_usage
         WHERE created_at >= date_trunc('hour', now()) - interval '23 hours'
         GROUP BY 1 ORDER BY 1
    """)
    return {"today_only": today_only, "total": total, "by_task": by_task,
            "queue": queue, "series": series}


# ─────────────────── 挂起率 / 栏位待裁决率 ───────────────────

def _suspend_rate(org_key: str | None) -> dict:
    """挂起率 = pending_items / 总抽取项（route_decisions 一行 = 一条抽取判定）。"""
    p: dict = {}
    org = _org_clause("org_key", org_key, p)
    pend = db.query_one(
        f"SELECT count(*)::int AS n FROM pending_items WHERE TRUE{org}", p)["n"]
    total = db.query_one(
        f"SELECT count(*)::int AS n FROM route_decisions WHERE TRUE{org}", p)["n"]
    return {"pending": pend, "total_routed": total,
            "rate": round(pend / total, 4) if total else None}


def _section_pending_rate(org_key: str | None) -> dict:
    """栏位待裁决率 = pending_items(stage='section') / 总抽取项。
    ★ 它高说明向量匹配 top-1 分数低或 top-1/top-2 太接近——直接信号是 `aliases` 缺词。"""
    p: dict = {}
    org = _org_clause("org_key", org_key, p)
    sec = db.query_one(
        f"SELECT count(*)::int AS n FROM pending_items"
        f" WHERE stage='section'{org}", p)["n"]
    total = db.query_one(
        f"SELECT count(*)::int AS n FROM route_decisions WHERE TRUE{org}", p)["n"]
    return {"section_pending": sec, "total_routed": total,
            "rate": round(sec / total, 4) if total else None}


# ─────────────────── 改判率（按 kind 分开） ───────────────────

def _override_rate(org_key: str | None, thresholds: dict[str, float]) -> dict:
    """改判率 = 高置信却被人工改判 / 该 kind 的高置信判定总数。

    ★ **必须按 kind 分开看**（§10）：这是最危险的指标——模型高置信却判错，
      这些会静默污染记忆库。只对设了阈值的 kind（目前 atomic_fact=0.85）算"高置信"口径；
      observation 不设阈值（#51），discard 一律不进待办，都没有阈值，只报总改判数。
    """
    p: dict = {}
    org = _org_clause("org_key", org_key, p)
    rows = db.query(
        f"SELECT kind, overridden, confidence FROM route_decisions WHERE TRUE{org}", p)

    by_kind: dict[str, dict] = {}
    for r in rows:
        k = r["kind"]
        b = by_kind.setdefault(k, {"total": 0, "overridden": 0, "threshold": thresholds.get(k),
                                   "high_conf": 0, "high_conf_overridden": 0})
        b["total"] += 1
        thr = thresholds.get(k)
        conf = float(r["confidence"]) if r["confidence"] is not None else None
        if r["overridden"]:
            b["overridden"] += 1
        if thr is not None and conf is not None and conf >= thr:
            b["high_conf"] += 1
            if r["overridden"]:
                b["high_conf_overridden"] += 1
    for b in by_kind.values():
        b["high_conf_override_rate"] = (
            round(b["high_conf_overridden"] / b["high_conf"], 4) if b["high_conf"] else None)
        b["override_rate"] = round(b["overridden"] / b["total"], 4) if b["total"] else None
    return {"by_kind": by_kind, "thresholds": thresholds}


# ─────────────────── 规矩漏判率（只给候选池，比率人工抽） ───────────────────

def _rule_miss_candidates(org_key: str | None, sample_limit: int) -> dict:
    """含规范性措辞的 active 记忆 = 疑似"本该是 observation 却入了库"的候选池。

    ★ 比率算不出来（要人工看每条到底是不是规矩），所以只返回 count + 样本，
      标 `manual`。§10：这是唯一能发现"静默丢失的规矩"的手段。
    """
    p: dict = {"limit": sample_limit}
    org = _org_clause("org_key", org_key, p)
    # content 里含任一规范性措辞的 active 记忆
    p.update({f"nw{i}": f"%{w}%" for i, w in enumerate(_NORMATIVE_WORDS)})
    cond = " OR ".join([f"content LIKE %(nw{i})s" for i in range(len(_NORMATIVE_WORDS))])
    where = f"status='active' AND ({cond}){org}"
    total = db.query_one(
        f"SELECT count(*)::int AS n FROM memories WHERE {where}", p)["n"]
    sample = db.query(
        f"SELECT id, scope, subject, role, content FROM memories WHERE {where}"
        f" ORDER BY updated_at DESC LIMIT %(limit)s", p)
    return {"method": "manual", "words": list(_NORMATIVE_WORDS),
            "candidate_count": total, "sample": sample}


# ─────────────────── 延迟 p50/p95 ───────────────────

def _latency(org_key: str | None, window_days: int) -> dict:
    p: dict = {"days": window_days}
    org = _org_clause("org_key", org_key, p)
    row = db.query_one(
        f"""SELECT count(*)::int AS n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
            FROM recall_log
            WHERE created_at >= now() - make_interval(days => %(days)s){org}""", p)
    return {"window_days": window_days, "samples": row["n"],
            "p50_ms": round(row["p50"], 1) if row["p50"] is not None else None,
            "p95_ms": round(row["p95"], 1) if row["p95"] is not None else None}


# ─────────────────── 纠错次数 ───────────────────

def _corrections(org_key: str | None) -> dict:
    """memory_feedback(kind='corrected') = 记忆准确性的真实信号。
    ★ feedback 表没有 org_key，靠 memory_id join memories 拿分区。"""
    p: dict = {}
    if org_key:
        p["org"] = org_key
        join = "JOIN memories m ON m.id = f.memory_id"
        org_c = " AND m.org_key = %(org)s"
    else:
        join = ""
        org_c = ""
    rows = db.query(
        f"SELECT f.kind, count(*)::int AS n FROM memory_feedback f {join}"
        f" WHERE TRUE{org_c} GROUP BY f.kind", p)
    by_kind = {r["kind"]: r["n"] for r in rows}
    return {"by_kind": by_kind, "corrected": by_kind.get("corrected", 0)}


# ─────────────────── 空 section 数 ───────────────────

def _empty_sections(org_key: str | None) -> dict:
    """enabled 的 doc_sections 里没有对应 living_doc 行的 = 空栏位。
    ★ §10：`local_env`/`env_pitfalls` 长期为空就是判据有洞（#47）。
    给了 org_key 就只算该 org 有没有填过（org 级 + 它名下 project 级都算）。"""
    total = db.query_one("SELECT count(*)::int AS n FROM doc_sections WHERE enabled")["n"]
    if org_key:
        empty = db.query(
            "SELECT s.section_key, s.label, s.sort FROM doc_sections s"
            " WHERE s.enabled AND NOT EXISTS (SELECT 1 FROM living_doc d"
            "   WHERE d.section_key=s.section_key AND d.org_key=%(org)s)"
            " ORDER BY s.sort", {"org": org_key})
    else:
        empty = db.query(
            "SELECT s.section_key, s.label, s.sort FROM doc_sections s"
            " WHERE s.enabled AND NOT EXISTS (SELECT 1 FROM living_doc d"
            "   WHERE d.section_key=s.section_key) ORDER BY s.sort")
    return {"enabled_total": total, "empty_count": len(empty), "empty": empty}


# ─────────────────── 规矩类人审吞吐 ───────────────────

def _review_throughput(org_key: str | None, window_days: int) -> dict:
    """规矩类（model_kind='observation'）待办的日均新增 vs 日均处理。

    ★ 规矩类 100% 进人审。积压持续增长（新增 > 处理）说明"全审"扛不住了——
      §10：得回头给 prompt 加 few-shot 把准确率提上去，而不是把阈值加回来。
      pending 建卡时 kind 一律预选 observation，真正的模型判定在 `model_kind`。
    """
    p: dict = {"days": window_days}
    org = _org_clause("org_key", org_key, p)
    base = "suggestion->>'model_kind' = 'observation'"
    backlog = db.query_one(
        f"SELECT count(*)::int AS n FROM pending_items"
        f" WHERE {base} AND status='pending'{org}", p)["n"]
    new = db.query_one(
        f"SELECT count(*)::int AS n FROM pending_items"
        f" WHERE {base} AND created_at >= now() - make_interval(days => %(days)s){org}", p)["n"]
    resolved = db.query_one(
        f"SELECT count(*)::int AS n FROM pending_items"
        f" WHERE {base} AND status IN ('done','rejected','expired')"
        f"   AND resolved_at >= now() - make_interval(days => %(days)s){org}", p)["n"]
    daily_new = round(new / window_days, 2) if window_days else None
    daily_resolved = round(resolved / window_days, 2) if window_days else None
    return {
        "window_days": window_days, "backlog": backlog,
        "new": new, "resolved": resolved,
        "daily_new": daily_new, "daily_resolved": daily_resolved,
        "net_backlog_growth": (new - resolved),
        "sustained_growth": bool(new > resolved),
    }


# ─────────────────── 召回评测（hit@5 / MRR@10）：待测 ───────────────────

def _recall_eval(org_key: str | None) -> dict:
    p: dict = {}
    org = _org_clause("org_key", org_key, p)
    n = db.query_one(
        f"SELECT count(*)::int AS n FROM eval_recall_cases WHERE TRUE{org}", p)["n"]
    if n == 0:
        # ★ 没有用例就没有 hit@5/MRR。0 用例时明确标待测，不编数（§10 方法论硬规矩）。
        return {"cases": 0, "status": "待测", "hit_at_5": None, "mrr_at_10": None,
                "p95_ms": None, "run_at": None}
    # eval_runs 无 org 维度（runner 全量跑），分区视图下也报最近一次全局结果
    run = db.query_one(
        "SELECT run_at, hit_at_5, mrr, p95_ms FROM eval_runs ORDER BY id DESC LIMIT 1")
    if run is None:
        return {"cases": n, "status": "需跑 runner", "hit_at_5": None,
                "mrr_at_10": None, "p95_ms": None, "run_at": None}
    return {
        "cases": n,
        "status": "已实测" if run["hit_at_5"] is not None else "已跑（无正例）",
        "hit_at_5": float(run["hit_at_5"]) if run["hit_at_5"] is not None else None,
        "mrr_at_10": float(run["mrr"]) if run["mrr"] is not None else None,
        "p95_ms": run["p95_ms"],
        "run_at": run["run_at"].strftime("%Y-%m-%d %H:%M"),
    }
