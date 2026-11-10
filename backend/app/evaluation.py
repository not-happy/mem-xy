"""召回评测：指标计算 + 候选用例文件 + runner（EXPERT-REVIEW Phase 1.1/1.2）。

指标语义（plan §10 硬规矩：能算的给数字，算不了标待测，绝不编数）：

- **正例**（expected_ids 非空）：top-k 里出现任一期望 id 记命中；hit@5 = 命中数/正例数，
  MRR@10 = 首个命中名次倒数的均值（top-10 外记 0）。
- **否定例**（must_not_ids 非空）：top-k 里出现任一禁中 id 记泄漏。**不计入**
  hit@5/MRR——两个方向的指标混在一起会互相稀释，分开报。
- 一条用例允许同时带 expect 与 must_not（"该召回 A 且绝不带出 B"），两边分别计入。

runner 用 `recall(write_log=False)`：评测跑批不污染 recall_log（"本轮真注入了什么"
的审计，UI 在看）。p50/p95 取 recall 内部计时（真实 embedding + 真实 PG，
§14 的 400ms 预算就是对这个口径）。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

from .core import db
from .core.config import get_settings

log = logging.getLogger(__name__)

# 候选清单默认落在项目根（工作文件，不混进 backend 代码目录）
DEFAULT_CANDIDATES_FILE = Path(__file__).resolve().parents[2] / "eval-candidates.md"

_CASE_HEADER = "## case"
_FIELDS = ("query", "expect", "must_not", "org", "project", "label")


# ─────────────────── 指标纯函数 ───────────────────

def hit_at_k(expected: list[int], ranked: list[int], k: int = 5) -> bool:
    return bool(set(expected) & set(ranked[:k]))


def reciprocal_rank(expected: list[int], ranked: list[int], cap: int = 10) -> float:
    """首个命中名次的倒数；top-`cap` 内没命中记 0（MRR@10 的口径）。"""
    for rank, memory_id in enumerate(ranked[:cap], start=1):
        if memory_id in expected:
            return 1.0 / rank
    return 0.0


def leaked(must_not: list[int], ranked: list[int], k: int = 5) -> bool:
    return bool(set(must_not) & set(ranked[:k]))


def percentile(values: list[int], p: float) -> int | None:
    """nearest-rank 百分位。空列表返回 None（待测，不编 0）。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[idx]


# ─────────────────── 候选用例文件（人可编辑的行协议） ───────────────────

@dataclass
class CaseDraft:
    query: str
    expect: list[int] = field(default_factory=list)
    must_not: list[int] = field(default_factory=list)
    org: str | None = None
    project: str | None = None
    label: str | None = None
    note: str | None = None    # 生成时的参考（原记忆摘录），导入不入库


def render_candidate_file(drafts: list[CaseDraft], header_lines: list[str]) -> str:
    out = list(header_lines)
    for draft in drafts:
        out.append("")
        out.append(_CASE_HEADER)
        out.append(f"- query: {draft.query}")
        out.append(f"- expect: {', '.join(str(i) for i in draft.expect)}")
        out.append(f"- must_not: {', '.join(str(i) for i in draft.must_not)}")
        out.append(f"- org: {draft.org or ''}")
        out.append(f"- project: {draft.project or ''}")
        out.append(f"- label: {draft.label or ''}")
        if draft.note:
            out.append(f"<!-- {draft.note} -->")
    return "\n".join(out) + "\n"


def parse_candidate_file(text: str) -> tuple[list[CaseDraft], list[str]]:
    """解析候选清单。返回 (drafts, errors)——坏块记入 errors（带块号），好块照常返回，
    让人一次看到所有问题而不是修一个报一个。"""
    drafts: list[CaseDraft] = []
    errors: list[str] = []
    blocks = [b for b in text.split(_CASE_HEADER)[1:]]
    for no, block in enumerate(blocks, start=1):
        raw: dict[str, str] = {}
        note = None
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("<!--") and line.endswith("-->"):
                note = line[4:-3].strip()
                continue
            if not line.startswith("- "):
                continue
            key, _, value = line[2:].partition(":")
            key, value = key.strip(), value.strip()
            if key in _FIELDS:
                raw[key] = value
        if not raw.get("query"):
            errors.append(f"第 {no} 块缺 query，已跳过")
            continue
        try:
            expect = _parse_ids(raw.get("expect", ""))
            must_not = _parse_ids(raw.get("must_not", ""))
        except ValueError as exc:
            errors.append(f"第 {no} 块 id 解析失败（{exc}），已跳过")
            continue
        if not expect and not must_not:
            errors.append(f"第 {no} 块 expect 与 must_not 都为空（用例没有判定意义），已跳过")
            continue
        drafts.append(CaseDraft(
            query=raw["query"], expect=expect, must_not=must_not,
            org=raw.get("org") or None, project=raw.get("project") or None,
            label=raw.get("label") or None, note=note))
    return drafts, errors


def _parse_ids(value: str) -> list[int]:
    if not value:
        return []
    return [int(part) for part in (p.strip() for p in value.split(",")) if part]


# ─────────────────── 导入（幂等） ───────────────────

def import_cases(drafts: list[CaseDraft]) -> dict:
    """写入 eval_recall_cases。按 (query_text, org, project, expect, must_not) 全等
    判重跳过——重复导入同一份清单不产生重复用例；同一 query 配不同判定
    （如正例 + 否定例）是两条用例，不算重。"""
    inserted = skipped = 0
    for draft in drafts:
        exists = db.query_one(
            "SELECT id FROM eval_recall_cases"
            " WHERE query_text = %s AND org_key IS NOT DISTINCT FROM %s"
            "   AND project_key IS NOT DISTINCT FROM %s"
            "   AND expected_ids = %s::bigint[] AND must_not_ids = %s::bigint[]",
            (draft.query, draft.org, draft.project, draft.expect, draft.must_not))
        if exists:
            skipped += 1
            continue
        db.execute(
            "INSERT INTO eval_recall_cases"
            " (label, query_text, org_key, project_key, expected_ids, must_not_ids)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (draft.label, draft.query, draft.org, draft.project,
             draft.expect, draft.must_not))
        inserted += 1
    return {"inserted": inserted, "skipped": skipped}


# ─────────────────── runner ───────────────────

@dataclass
class RunSummary:
    total: int = 0
    positives: int = 0
    negatives: int = 0
    hit_at_5: float | None = None       # 无正例 → None（待测）
    mrr_at_10: float | None = None
    leak_count: int = 0
    leak_rate: float | None = None      # 无否定例 → None
    p50_ms: int | None = None
    p95_ms: int | None = None
    missed: list[dict] = field(default_factory=list)   # 未命中/泄漏明细，供人工看
    run_id: int | None = None


def run_eval(k: int | None = None, write_run: bool = True) -> RunSummary:
    """全量用例跑一遍召回并落 eval_runs。加载 bge-m3（首条前预热，冷启动不计入延迟）。"""
    from .core.embedding import get_embedding_service
    from .retrieve.recall import recall

    cases = db.query("SELECT id, label, query_text, org_key, project_key,"
                     " expected_ids, must_not_ids FROM eval_recall_cases ORDER BY id")
    if not cases:
        raise RuntimeError("eval_recall_cases 为空——先 --gen-candidates 生成并经人工确认导入")

    top_k = k or get_settings().recall_k
    get_embedding_service().encode_one("warmup")   # 模型冷启动 15–33s，不计进任何用例

    summary = RunSummary(total=len(cases))
    latencies: list[int] = []
    hits = 0
    rr_total = 0.0
    for case in cases:
        expected = list(case["expected_ids"] or [])
        must_not = list(case["must_not_ids"] or [])
        result = recall(query=case["query_text"], org_key=case["org_key"],
                        project_key=case["project_key"], k=top_k,
                        session_id="eval-runner", write_log=False)
        ranked = [hit.memory_id for hit in result.items]
        latencies.append(result.latency_ms)
        if expected:
            summary.positives += 1
            if hit_at_k(expected, ranked, k=top_k):
                hits += 1
            rr_total += reciprocal_rank(expected, ranked)
            if not hit_at_k(expected, ranked, k=top_k):
                summary.missed.append({"case_id": case["id"], "query": case["query_text"],
                                       "expected": expected, "got": ranked})
        if must_not:
            summary.negatives += 1
            if leaked(must_not, ranked, k=top_k):
                summary.leak_count += 1
                summary.missed.append({"case_id": case["id"], "query": case["query_text"],
                                       "must_not": must_not, "got": ranked})

    if summary.positives:
        summary.hit_at_5 = hits / summary.positives
        summary.mrr_at_10 = rr_total / summary.positives
    if summary.negatives:
        summary.leak_rate = summary.leak_count / summary.negatives
    summary.p50_ms = percentile(latencies, 50)
    summary.p95_ms = percentile(latencies, 95)

    if write_run:
        summary.run_id = _persist_run(summary, top_k)
    return summary


def _persist_run(summary: RunSummary, top_k: int) -> int:
    config = json.dumps({"k": top_k, "cases": summary.total,
                         "positives": summary.positives, "negatives": summary.negatives,
                         "p50_ms": summary.p50_ms})
    notes = f"missed={len(summary.missed)}"
    if summary.leak_count:
        notes += f"; leak={summary.leak_count}/{summary.negatives}"
    # db.execute 返回 rowcount 拿不到新 id，用 RETURNING（query_one 的池连接退出即 commit）
    row = db.query_one(
        "INSERT INTO eval_runs (prompt_ver, config, hit_at_5, mrr, p95_ms, notes)"
        " VALUES (NULL, %s, %s, %s, %s, %s) RETURNING id",
        (config, summary.hit_at_5, summary.mrr_at_10, summary.p95_ms, notes))
    return row["id"]


def latest_run() -> dict | None:
    return db.query_one(
        "SELECT id, run_at, config, hit_at_5, mrr, p95_ms, notes"
        " FROM eval_runs ORDER BY id DESC LIMIT 1")
