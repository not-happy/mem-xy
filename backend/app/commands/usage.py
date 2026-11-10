"""token 消耗监控：`python -m app.commands.usage`

    python -m app.commands.usage            # 汇总 + 分任务明细
    python -m app.commands.usage --today    # 只看今天
    python -m app.commands.usage --tail 20  # 最近 20 次调用逐条

数据源是 `llm_usage` 表（migration 0003），每次 LLM 调用一行。
只读、不调 LLM、不加载 bge-m3，秒回。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM token 消耗监控")
    parser.add_argument("--today", action="store_true", help="只统计今天")
    parser.add_argument("--tail", type=int, default=0, metavar="N",
                        help="列出最近 N 次调用")
    args = parser.parse_args(argv)

    from app.core import db

    db.init_pool(min_size=1, max_size=1)
    try:
        return _report(db, args)
    finally:
        db.close_pool()


def _report(db, args) -> int:
    where = "WHERE created_at >= date_trunc('day', now())" if args.today else ""

    total = db.query_one(f"""
        SELECT count(*)                                        AS calls,
               count(*) FILTER (WHERE NOT ok)                  AS failed,
               coalesce(sum(input_tokens), 0)                  AS in_tok,
               coalesce(sum(output_tokens), 0)                 AS out_tok,
               coalesce(sum(input_tokens + output_tokens), 0)  AS all_tok,
               coalesce(round(avg(latency_ms) FILTER (WHERE ok)), 0) AS avg_ms
          FROM llm_usage {where}
    """)
    if not total or not total["calls"]:
        print("llm_usage 还没有记录" + ("（今天）" if args.today else ""))
        return 0

    print(f"── LLM 消耗{'（今天）' if args.today else '（累计）'} ──")
    print(f"  调用 {total['calls']} 次（失败 {total['failed']}）  "
          f"输入 {total['in_tok']:,} tok + 输出 {total['out_tok']:,} tok "
          f"= 合计 {total['all_tok']:,} tok  平均延迟 {int(total['avg_ms'])}ms")

    print("\n── 分任务 ──")
    for row in db.query(f"""
            SELECT task, count(*) AS calls,
                   sum(input_tokens) AS in_tok, sum(output_tokens) AS out_tok,
                   sum(input_tokens + output_tokens) AS all_tok,
                   round(avg(latency_ms) FILTER (WHERE ok)) AS avg_ms,
                   count(*) FILTER (WHERE NOT ok) AS failed
              FROM llm_usage {where}
             GROUP BY task ORDER BY all_tok DESC
    """):
        print(f"  {row['task']:<14} {row['calls']:>4} 次  "
              f"{row['all_tok']:>10,} tok（in {row['in_tok']:,} / out {row['out_tok']:,}）"
              f"  avg {int(row['avg_ms'] or 0):>6}ms  失败 {row['failed']}")

    if args.tail:
        print(f"\n── 最近 {args.tail} 次调用 ──")
        for row in db.query("""
                SELECT created_at, task, ok, input_tokens, output_tokens, latency_ms,
                       left(coalesce(error, ''), 80) AS error
                  FROM llm_usage ORDER BY id DESC LIMIT %s
        """, (args.tail,)):
            flag = "" if row["ok"] else f"  ✗ {row['error']}"
            print(f"  {row['created_at']:%m-%d %H:%M:%S} {row['task']:<12} "
                  f"in {row['input_tokens']:>6} / out {row['output_tokens']:>5} "
                  f"{row['latency_ms']:>6}ms{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
