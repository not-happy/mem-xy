"""P1 摄取入口：扫 Claude Code transcript，过滤后落 `conversation_log`。

    python -m app.commands.ingest              # 全量扫（幂等，重复跑不产生新行）
    python -m app.commands.ingest --dry-run    # 只量不写，用来对基线
    python -m app.commands.ingest --subagents  # 连子 agent 一起扫（默认不扫，见 §3.1）

★ 不加载 embedding 模型：P1 全程零向量计算，所以启动是秒级，不受 33s 冷启动影响。
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import time

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫 transcript 落 conversation_log")
    parser.add_argument("--dry-run", action="store_true", help="只量不写")
    parser.add_argument("--subagents", action="store_true",
                        help="连子 agent transcript 一起扫（默认只扫主会话，§3.1）")
    parser.add_argument("--root", default=None, help="覆盖 ~/.claude/projects")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.core import db
    from app.ingest.scan import scan_once
    from app.ingest.transcripts import DEFAULT_ROOT

    db.init_pool(min_size=1, max_size=2)
    try:
        root = pathlib.Path(args.root) if args.root else DEFAULT_ROOT
        started = time.perf_counter()
        stats = scan_once(root, include_subagents=args.subagents, write=not args.dry_run)
        elapsed = time.perf_counter() - started

        report = stats.as_dict()
        print(f"\n扫描 {report['files']} 个文件"
              f"（跳过未改动 {report['files_skipped']}）/ {report['records']} 行 / {elapsed:.1f}s")
        print(f"留存 {report['candidates']} 行 ⇒ 信噪比 {report['ratio']:.1%}"
              f"   （§13 P1 基线 7.9%）")
        if not args.dry_run:
            print(f"新增 {report['inserted']} 行，被幂等键挡掉 {report['conflicts']} 行")
        if report["no_key"]:
            print(f"⚠ {report['no_key']} 行缺 sessionId/uuid，已跳过——transcript 格式可能变了")
        print(f"role 分布：{report['roles']}")
        print(f"归属分布：{report['orgs']}   会话 {report['sessions']} 个 / 目录 {report['cwds']} 个")
        if report["default_cwds"]:
            print(f"★ 落到 personal 默认桶的目录（{len(report['default_cwds'])} 个，"
                  f"UI「归属管理」页要列出来供一次性确认）：")
            for cwd in report["default_cwds"]:
                print(f"    {cwd}")
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
