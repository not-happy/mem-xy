"""量窗口切分的真实形状：窗口数、每窗 token 分布、thinking 存活数。

这三个数决定 P2 的成本，不能靠推理：
  * 窗口数 = route 的 LLM 调用次数（1 次/窗口，§5.2）
  * token 分布 = 要不要设 `token_budget`、设多少
  * thinking 存活数 = 复核 §5.4 里那个 255（那是 bench 侧算的，这里从库里算）

    python bench/bench_windows.py            # 全量
    python bench/bench_windows.py --budget 6000
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "backend"))

from app.core import db  # noqa: E402
from app.pipeline import windows  # noqa: E402


def pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    return int(statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=0, help="试算某个 token_budget 的效果")
    args = parser.parse_args()

    db.init_pool(min_size=1, max_size=2)
    try:
        sessions = db.query(
            "SELECT DISTINCT session_id FROM conversation_log WHERE source='claude-code'"
        )
        all_windows = []
        thinking_kept = thinking_total = 0
        for row in sessions:
            rows = db.query(
                "SELECT id, role, content, turn_no, coalesce(tokens_est,0) AS tokens_est "
                "FROM conversation_log WHERE source='claude-code' AND session_id=%s ORDER BY id",
                (row["session_id"],),
            )
            parsed = [windows.Row(id=r["id"], role=r["role"], content=r["content"],
                                  turn_no=r["turn_no"], tokens_est=r["tokens_est"])
                      for r in rows]
            thinking_total += sum(1 for r in parsed if r.role == "thinking")
            wins = windows.slice_windows(parsed, token_budget=args.budget)
            thinking_kept += sum(1 for w in wins for r in w.rows if r.role == "thinking")
            all_windows.extend((row["session_id"], w) for w in wins)
    finally:
        db.close_pool()

    tokens = [w.tokens for _, w in all_windows]
    summary_wins = [w for _, w in all_windows if w.is_summary]
    rows_per = [len(w.rows) for _, w in all_windows]
    print(f"会话 {len(sessions)} 个 → 窗口 {len(all_windows)} 个"
          f"（= route 的 LLM 调用次数），其中摘要单独成窗 {len(summary_wins)} 个")
    print(f"每窗行数 p50={pct(rows_per, .5)} p95={pct(rows_per, .95)} max={max(rows_per)}")
    print(f"每窗 token p50={pct(tokens, .5)} p90={pct(tokens, .9)} p95={pct(tokens, .95)} "
          f"p99={pct(tokens, .99)} max={max(tokens)}  合计={sum(tokens)}")
    over = [t for t in tokens if t > 32000]
    print(f"超过端点 max_input_tokens(32000) 的窗口：{len(over)} 个"
          f"{' → 必须设 token_budget' if over else ''}")
    print(f"thinking：入库 {thinking_total} 条 → 进窗口 {thinking_kept} 条"
          f"（{thinking_kept / max(thinking_total, 1):.0%}）")
    if not args.budget:
        print("★ 拿 --budget 6000 / 8000 再跑一遍，看窗口数涨多少、max 压到多少。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
