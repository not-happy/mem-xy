"""A11：TOKEN_BUDGET 影子对比实验——{12000, 40000} 两档窗口切分对照。

用法（项目根目录下）：
    python bench/bench_token_budget.py --dry            # 只看两档切分统计，不调 LLM
    python bench/bench_token_budget.py --sessions 20    # 抽 20 个活跃会话做 LLM 重放对照
    python bench/bench_token_budget.py --budgets 12000 40000 --sessions 5

背景：S7——TOKEN_BUDGET=12000 是 32k 上下限时代定的值；#106 网关实测
（≥124643 tok、prefill ≈34ms/k tok）后，12k 意味着同样多的对话要拆成
更多窗口、付更多次 prefill 与固定开销。本脚本回答三件事：
  1. 两档预算下同一批会话各切多少窗（窗口数 ≈ LLM 调用数 ≈ 时间）；
  2. 大窗会不会撞"单次输出 max_tokens=4096"的顶（items 过多被截断的风险）；
  3. 抽样窗口在新预算下重判，与 12k 档的旧判定（route_decisions 现状）
     做改判对照——预算变大会把更多上下文塞进单窗，抽取粒度可能变化。

★ 影子语义：不写 route_decisions / pending_items / extraction_cursor /
  route_checkpoints，LLM 用量以 task='route_budget_replay' 落 llm_usage。
★ 结论落在 stdout（可直接贴 decisions-log），不自动切换 TOKEN_BUDGET——
  切不切由人看完报告再定（ROADMAP A11 验收即"实验报告落 bench/ 并记
  decisions-log；切换后评测集不回归"）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core import db  # noqa: E402
from app.core.llm import LLMParseError  # noqa: E402
from app.pipeline import router  # noqa: E402
from app.pipeline.windows import Row, slice_windows  # noqa: E402
from app.prompts import route as route_prompt  # noqa: E402

DEFAULT_BUDGETS = (12000, 40000)
# 单次输出上限（core/llm.py structured 调用 max_tokens）：items 每条 ~150-300 tok，
# 加上工具调用 JSON 包装，4096 大约撑 15-20 条 items——超过就有截断风险。
MAX_OUTPUT_TOKENS = 4096
DEFAULT_CACHE = Path(os.environ.get("TEMP", "/tmp")) / "bench_token_budget.json"


def _pick_sessions(limit: int) -> list[dict]:
    """抽近期有新素材的会话（与 route 队列同一口径，取最活跃的先看）。"""
    return db.query(
        """
        SELECT c.source, c.session_id, count(*) AS n
          FROM conversation_log c
         GROUP BY c.source, c.session_id
         ORDER BY count(*) DESC
         LIMIT %(limit)s
        """,
        {"limit": limit})


def _load_rows(source: str, session_id: str) -> list[Row]:
    rows = db.query(router.ROW_SQL, {"source": source, "session": session_id})
    return [Row(id=r["id"], role=r["role"], content=r["content"],
                turn_no=r["turn_no"], tokens_est=r["tokens_est"]) for r in rows]


def _split(rows: list[Row], budget: int):
    return slice_windows(rows, token_budget=budget)


def _cut_stats(rows: list[Row], budgets: tuple[int, ...]) -> dict:
    """不调 LLM：两档预算各切多少窗、最大窗多大、多少行。"""
    out = {}
    for b in budgets:
        windows = _split(rows, b)
        sizes = [w.tokens for w in windows]
        out[b] = {
            "windows": len(windows),
            "total_tokens": sum(sizes),
            "max_window_tokens": max(sizes) if sizes else 0,
            "avg_window_tokens": round(sum(sizes) / len(sizes)) if sizes else 0,
            "rows": len(rows),
            "over_12k_windows": sum(1 for s in sizes if s > 12000),
        }
    return out


def _replay_window(window, ctx, client) -> dict:
    started = time.perf_counter()
    try:
        result = client.structured(
            "route_budget_replay",
            system=route_prompt.system_prompt(),
            messages=[{"role": "user", "content": router._user_message(window, ctx)}],
            tool=route_prompt.TOOL,
            schema=route_prompt.RouteResult)
    except LLMParseError as exc:
        return {"error": f"parse_failed: {str(exc)[:120]}", "seconds": time.perf_counter() - started}
    cost = time.perf_counter() - started
    kinds: dict[str, int] = {}
    for item in result.items:
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    return {"error": None, "items": len(result.items), "kinds": kinds,
            "window_tokens": window.tokens, "seconds": round(cost, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description="A11 TOKEN_BUDGET 两档影子对比")
    parser.add_argument("--budgets", type=int, nargs="+", default=list(DEFAULT_BUDGETS),
                        help="对比的预算档位（默认 12000 40000）")
    parser.add_argument("--sessions", type=int, default=0,
                        help="抽几个会话做 LLM 重放（0=只做切分统计，不调 LLM）")
    parser.add_argument("--windows-per-session", type=int, default=1,
                        help="每会话抽几个窗口重放（默认 1，取最大的窗口）")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    budgets = tuple(args.budgets)
    args.cache = Path(args.cache)
    if args.no_cache and args.cache.exists():
        args.cache.unlink()

    db.init_pool(min_size=1, max_size=2)
    try:
        sessions = _pick_sessions(max(args.sessions, 20) if args.sessions else 20)
        print(f"TOKEN_BUDGET 影子对比：budgets={budgets}，"
              f"会话候选 {len(sessions)} 个（按消息量取最活跃）")
        print(f"route prompt_ver = {route_prompt.PROMPT_VER}，"
              f"当前生产 TOKEN_BUDGET = {router.TOKEN_BUDGET}")

        # ── 第一部分：纯切分统计（不花钱）─────────────────────────
        agg = {b: {"windows": 0, "total_tokens": 0} for b in budgets}
        print("\n═══ 切分统计（全部候选会话）═══")
        print(f"{'session':<24} {'rows':>5} " + " ".join(f"{b:>7}" for b in budgets))
        for s in sessions:
            rows = _load_rows(s["source"], s["session_id"])
            if not rows:
                continue
            stats = _cut_stats(rows, budgets)
            line = f"{s['session_id'][:22]:<24} {len(rows):>5} "
            for b in budgets:
                line += f" {stats[b]['windows']:>6}w "
                agg[b]["windows"] += stats[b]["windows"]
                agg[b]["total_tokens"] += stats[b]["total_tokens"]
            print(line)
        base = agg[budgets[0]]["windows"] or 1
        print("\n汇总：")
        for b in budgets:
            w = agg[b]["windows"]
            print(f"  budget={b}: {w} 窗（相对 {budgets[0]} 档 {w / base:.0%}）")

        # ── 第二部分：抽样重放（花钱，可跳过）─────────────────────
        if not args.sessions:
            print("\n（未指定 --sessions，跳过 LLM 重放；切分统计已可估算调用数收益）")
            _print_verdict(agg, budgets, None)
            return 0

        from app.core.llm import get_llm_client
        client = get_llm_client()

        cache: dict = {}
        if args.cache.exists():
            cache = json.loads(args.cache.read_text(encoding="utf-8"))

        big = max(budgets)
        samples = []          # (session, window, ctx, cache_key)
        for s in sessions[:args.sessions]:
            rows = _load_rows(s["source"], s["session_id"])
            windows = _split(rows, big)
            if not windows:
                continue
            ctx = router._session_ctx(s["session_id"], _first_raw(s))
            # 抽最大的窗口——预算差异在这里最显著，撞输出顶的风险也在这里
            for window in sorted(windows, key=lambda w: -w.tokens)[:args.windows_per_session]:
                key = f"{s['session_id']}:{window.anchor_id}:{big}"
                samples.append((s, window, ctx, key))

        print(f"\n═══ 重放 {len(samples)} 个大窗（budget={big} 档）═══")
        replay = []
        for n, (s, window, ctx, key) in enumerate(samples, 1):
            hit = cache.get(key)
            if hit is None:
                hit = _replay_window(window, ctx, client)
                cache[key] = hit
                args.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            if hit.get("error"):
                print(f"[{n}/{len(samples)}] {s['session_id'][:12]}… 窗 {window.anchor_id}"
                      f"（{window.tokens} tok）→ {hit['error']}")
            else:
                est_items_room = MAX_OUTPUT_TOKENS // 250
                risk = "⚠可能撞输出顶" if hit["items"] >= est_items_room else ""
                print(f"[{n}/{len(samples)}] {s['session_id'][:12]}… 窗 {window.anchor_id}"
                      f"（{window.tokens} tok，{hit['seconds']}s）→ {hit['items']} items "
                      f"{hit.get('kinds', {})} {risk}")
            replay.append(hit)

        ok = [r for r in replay if not r.get("error")]
        parse_fail = [r for r in replay if r.get("error")]
        _print_verdict(agg, budgets, {
            "replayed": len(replay),
            "parse_failed": len(parse_fail),
            "max_items": max((r["items"] for r in ok), default=0),
            "avg_seconds": round(sum(r["seconds"] for r in ok) / len(ok), 1) if ok else 0,
            "est_items_room_at_4096": MAX_OUTPUT_TOKENS // 250,
        })
        return 0
    finally:
        db.close_pool()


def _first_raw(s: dict) -> dict:
    row = db.query_one(router.ROW_SQL, {"source": s["source"], "session": s["session_id"]})
    return row or {}


def _print_verdict(agg: dict, budgets: tuple[int, ...], replay: dict | None) -> None:
    print("\n═══ 结论（供 decisions-log 引用）═══")
    b_small, b_big = budgets[0], budgets[-1]
    saved = agg[b_small]["windows"] - agg[b_big]["windows"]
    print(f"  预算 {b_small}→{b_big}：候选会话窗口数 {agg[b_small]['windows']} → "
          f"{agg[b_big]['windows']}（省 {saved} 次调用，"
          f"{saved / max(agg[b_small]['windows'], 1):.0%}）")
    if replay:
        print(f"  大窗重放：{replay['replayed']} 窗，parse_failed={replay['parse_failed']}，"
              f"单窗 items 最多 {replay['max_items']}（4096 输出顶约容纳 "
              f"{replay['est_items_room_at_4096']} 条），平均 {replay['avg_seconds']}s/窗")
        if replay["max_items"] >= replay["est_items_room_at_4096"]:
            print("  ⚠ 已有窗口 items 接近输出顶：切换前先确认 max_tokens 或分段写出，"
                  "否则 items 会被截断（事实静默丢失）。")
        elif replay["parse_failed"] == 0:
            print("  ✓ 抽样未发现撞顶或解析失败，可进入评测集回归后切换。")
    print("  本脚本不切换 TOKEN_BUDGET——确认后改 app/pipeline/router.py:TOKEN_BUDGET 并"
          "跑 backend/tests 回归。")


if __name__ == "__main__":
    raise SystemExit(main())
