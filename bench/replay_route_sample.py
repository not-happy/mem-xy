"""1.4 抽样清单影子重放：拿新 route prompt 把可溯源记忆的**真实源窗口**重判一遍。

用法（项目根目录下）：
    python bench/replay_route_sample.py --dry    # 只看窗口分组与期望，不调 LLM
    python bench/replay_route_sample.py          # 影子重放 + 改判对照

背景：route prompt 升到 v2（第二判据 + 对照 few-shot，#109）后，需要回答
"1.4 判出的 13 条净漏判，新 prompt 能把几条改判成 observation？18 条零碎
会不会被矫枉过正？"——本脚本对 rule-miss-sample.md 里有 source_msg 溯源的
条目，重建当时的窗口（router 同一套 slice_windows + _user_message），只读重放。

★ 影子语义：不写 route_decisions / pending_items / extraction_cursor，
  纯对照。LLM 用量以 task='route_replay' 落 llm_usage（不污染 route 统计）。
★ 对照口径：判定 规矩/已成文 → 期望 observation；零碎 → 期望 atomic_fact。
  重放输出的 content 是改写版，用字符 bigram Jaccard 找最接近原记忆的 item；
  相似度低于 0.10 视为"该记忆在新输出里没有对应 item"（可能被合并或没抽出来），
  单独列出人工看。
★ 容错与续跑：单窗口 LLMParseError（模型输出不合 schema、重试耗尽）不再中断
  全局——生产链路里这种窗口会落 pending 卡由人兜，重放里记为 parse_failed 继续。
  每个窗口的结果缓存进 JSON（默认 %TEMP%/replay_route_v2.json），崩了重跑
  自动跳过已完成窗口；--no-cache 强制全部重放。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.commands.rulemiss import DEFAULT_SAMPLE_FILE, parse_verdicts  # noqa: E402
from app.core import db  # noqa: E402
from app.core.llm import LLMParseError  # noqa: E402
from app.pipeline import router  # noqa: E402
from app.pipeline.windows import Row, slice_windows  # noqa: E402
from app.prompts import route as route_prompt  # noqa: E402

EXPECT = {"规矩": "observation", "已成文": "observation", "零碎": "atomic_fact"}
SIM_FLOOR = 0.10
DEFAULT_CACHE = Path(os.environ.get("TEMP", "/tmp")) / "replay_route_v2.json"


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sim(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def _load_targets() -> list[dict]:
    """清单判定 × memories 表 → 重放目标（含期望 kind）。"""
    verdicts, errors = parse_verdicts(DEFAULT_SAMPLE_FILE.read_text(encoding="utf-8"))
    if errors:
        raise SystemExit(f"清单里有非法判定值: {errors}")
    rows = db.query(
        "SELECT m.id, m.content, m.source_msg, c.session_id, c.source"
        "  FROM memories m LEFT JOIN conversation_log c ON c.id = m.source_msg"
        " WHERE m.id = ANY(%(ids)s) ORDER BY m.id",
        {"ids": list(verdicts)})
    for r in rows:
        r["verdict"] = verdicts[r["id"]]
        r["expect"] = EXPECT.get(r["verdict"])
    return rows


def _rebuild_window(source_msg: int, session_id: str, source: str):
    """按 router 的口径重建 source_msg 所在的窗口。找不到返回 None。"""
    rows = db.query(router.ROW_SQL, {"source": source, "session": session_id})
    if not rows:
        return None, None
    ctx = router._session_ctx(session_id, rows[0])
    parsed = [Row(id=r["id"], role=r["role"], content=r["content"],
                  turn_no=r["turn_no"], tokens_est=r["tokens_est"]) for r in rows]
    for w in slice_windows(parsed, token_budget=router.TOKEN_BUDGET):
        if w.anchor_id == source_msg:
            return w, ctx
    return None, ctx


def main() -> int:
    parser = argparse.ArgumentParser(description="1.4 清单影子重放（route-v2 改判对照）")
    parser.add_argument("--dry", action="store_true", help="只看分组，不调 LLM")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE),
                        help="窗口结果缓存 JSON（默认 TEMP/replay_route_v2.json，续跑用）")
    parser.add_argument("--no-cache", action="store_true", help="清掉缓存，全部窗口重放")
    args = parser.parse_args()
    args.cache = Path(args.cache)
    if args.no_cache and args.cache.exists():
        args.cache.unlink()

    db.init_pool(min_size=1, max_size=2)
    try:
        targets = _load_targets()
        replayable = [t for t in targets if t["session_id"]]
        blind = [t for t in targets if not t["session_id"]]
        by_window: dict[int, list[dict]] = {}
        for t in replayable:
            by_window.setdefault(t["source_msg"], []).append(t)

        print(f"prompt_ver = {route_prompt.PROMPT_VER}")
        print(f"目标 {len(targets)} 条：可重放 {len(replayable)}（{len(by_window)} 个源窗口），"
              f"无溯源 {len(blind)} 条（{'/'.join(str(t['id']) for t in blind)}）")
        if args.dry:
            for anchor, mems in sorted(by_window.items()):
                ids = ", ".join(f"#{m['id']}({m['verdict']}→期望{m['expect']})" for m in mems)
                print(f"  窗口 {anchor} · session {mems[0]['session_id'][:8]}: {ids}")
            return 0

        from app.core.llm import get_llm_client
        client = get_llm_client()

        cache: dict[str, dict] = {}
        if args.cache.exists():
            cache = json.loads(args.cache.read_text(encoding="utf-8"))
            print(f"载入缓存 {args.cache}：{len(cache)} 个窗口已完成，直接复用")

        results, unmatched, parse_failed = [], [], []
        for n, (anchor, mems) in enumerate(sorted(by_window.items()), 1):
            cached = cache.get(str(anchor))
            if cached is None:
                window, ctx = _rebuild_window(anchor, mems[0]["session_id"], mems[0]["source"])
                if window is None:
                    print(f"[{n}/{len(by_window)}] 窗口 {anchor} 重建失败，跳过 "
                          f"{'/'.join(str(m['id']) for m in mems)}")
                    unmatched.extend(mems)
                    continue
                started = time.perf_counter()
                try:
                    result = client.structured(
                        "route_replay",
                        system=route_prompt.system_prompt(),
                        messages=[{"role": "user",
                                   "content": router._user_message(window, ctx)}],
                        tool=route_prompt.TOOL,
                        schema=route_prompt.RouteResult)
                except LLMParseError as exc:
                    # 生产链路里这种窗口落 pending 卡由人兜；重放里记下来继续跑
                    print(f"[{n}/{len(by_window)}] 窗口 {anchor} 输出不合 schema、重试耗尽："
                          f"{str(exc)[:120]}")
                    cache[str(anchor)] = {"error": "parse_failed"}
                    args.cache.write_text(json.dumps(cache, ensure_ascii=False),
                                          encoding="utf-8")
                    parse_failed.extend(mems)
                    continue
                cost = time.perf_counter() - started
                cached = {"error": None,
                          "items": [{"kind": i.kind, "content": i.content}
                                    for i in result.items if i.content]}
                cache[str(anchor)] = cached
                args.cache.write_text(json.dumps(cache, ensure_ascii=False),
                                      encoding="utf-8")
                print(f"[{n}/{len(by_window)}] 窗口 {anchor}（{window.tokens} tok，{cost:.0f}s）"
                      f"→ {len(result.items)} items")
            else:
                print(f"[{n}/{len(by_window)}] 窗口 {anchor}（缓存）")
            if cached.get("error"):
                parse_failed.extend(mems)
                continue
            items = cached["items"]
            for m in mems:
                best, best_sim = None, 0.0
                for item in items:
                    if item["kind"] == "discard":
                        continue
                    s = _sim(m["content"], item["content"])
                    if s > best_sim:
                        best, best_sim = item, s
                if best is None or best_sim < SIM_FLOOR:
                    unmatched.append(m)
                    print(f"    #{m['id']} 期望 {m['expect']} → 无对应 item（重放没抽出这条）")
                    continue
                ok = "✓" if best["kind"] == m["expect"] else "✗"
                results.append({"id": m["id"], "verdict": m["verdict"],
                                "expect": m["expect"], "got": best["kind"], "sim": best_sim})
                print(f"    #{m['id']} 期望 {m['expect']} → 实判 {best['kind']} "
                      f"(sim={best_sim:.2f}) {ok}  {best['content'][:60]}")

        hit = sum(1 for r in results if r["got"] == r["expect"])
        print(f"\n═══ 对照汇总（重放 {len(results)} 条）═══")
        print(f"与人工判定一致: {hit}/{len(results)}"
              + (f" = {hit / len(results):.0%}" if results else ""))
        for want in ("observation", "atomic_fact"):
            sub = [r for r in results if r["expect"] == want]
            good = sum(1 for r in sub if r["got"] == want)
            print(f"  期望 {want}: {good}/{len(sub)} 判对")
        miss_now = [r for r in results if r["expect"] == "observation" and r["got"] != "observation"]
        over_now = [r for r in results if r["expect"] == "atomic_fact" and r["got"] != "atomic_fact"]
        if miss_now:
            print(f"  仍漏判（规矩→非observation）: {'/'.join(str(r['id']) for r in miss_now)}")
        if over_now:
            print(f"  矫枉过正（零碎→observation）: {'/'.join(str(r['id']) for r in over_now)}")
        if unmatched:
            print(f"  无法对照（无溯源/窗口重建失败/重放未抽出）: "
                  f"{'/'.join(str(m['id']) for m in unmatched)}")
        if parse_failed:
            print(f"  窗口输出解析失败（模型不合 schema，生产链路会落 pending 卡）: "
                  f"{'/'.join(str(m['id']) for m in parse_failed)}")
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
