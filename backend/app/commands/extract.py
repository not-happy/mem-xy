"""P2 写入闭环入口：路由 → 待办 → 人工裁决 → 四动作写入。

    python -m app.commands.extract --stats                  # 只看待办与改判率，不调 LLM
    python -m app.commands.extract --limit-sessions 2 --limit-windows 3   # 小规模试跑
    python -m app.commands.extract --session <session_id>    # 只跑一个会话
    python -m app.commands.extract --queue-all               # ★ 全量回填：只排队，交给 worker
    python -m app.commands.extract --list-pending            # 列出待办卡片
    python -m app.commands.extract --approve 12 --kind atomic_fact        # 人工裁决
    python -m app.commands.extract --run-jobs                # 把队列里的写入任务跑掉

★ 与 `app.commands.ingest` 不同，**这个命令会加载 bge-m3**（冷启动约 33s）：
  栏位匹配（§5.3）要给每条 observation 算向量，躲不开。`--queue-all` 例外，它只写队列。
★ 也会真的调 LLM 花钱。先用 `--limit-windows` 试跑，别一上来就全量。
★ 全量（259 窗口）实测单次 route ≈53s ⇒ 串行 ≈3.8 小时，**别用同步模式跑**，
  用 `--queue-all` + `python -m app.worker`（decisions-log C.9）。
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2 写入闭环")
    parser.add_argument("--session", default=None, help="只跑这个 session_id")
    parser.add_argument("--limit-sessions", type=int, default=None)
    parser.add_argument("--limit-windows", type=int, default=None,
                        help="每个会话最多调这么多次 LLM（游标不推过没跑完的窗口）")
    parser.add_argument("--run-jobs", action="store_true",
                        help="跑完路由后，把队列里的 write_facts / apply_decision 也执行掉")
    parser.add_argument("--queue-all", action="store_true",
                        help="★ 只排队不执行：每会话排一个 route job，交给后台 worker 慢慢抽。"
                             "全量回填必须走这条（单次 ≈53s，259 窗口串行 ≈3.8 小时）")
    parser.add_argument("--stats", action="store_true", help="只看统计，不调 LLM")
    parser.add_argument("--list-pending", action="store_true")
    parser.add_argument("--approve", type=int, default=None, metavar="PENDING_ID")
    parser.add_argument("--reject", type=int, default=None, metavar="PENDING_ID")
    parser.add_argument("--kind", default=None,
                        choices=["observation", "atomic_fact", "discard"],
                        help="--approve 时改判到哪个 kind（默认用卡片上预选的）")
    parser.add_argument("--content", default=None, help="--approve 时覆盖 content")
    parser.add_argument("--section", default=None, help="--approve 时指定 section_key")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from app.core import db

    db.init_pool(min_size=1, max_size=2)
    try:
        if args.stats:
            return _print_stats(db)
        if args.list_pending:
            return _list_pending(db)
        if args.approve is not None:
            return _adjudicate(db, args, approved=True)
        if args.reject is not None:
            return _adjudicate(db, args, approved=False)
        if args.queue_all:
            # ★ 故意不加载 bge-m3、不调 LLM：这一步只是往队列里塞活儿，秒回。
            from app.pipeline import router

            result = router.queue_all_sessions(limit_sessions=args.limit_sessions)
            print(f"排了 {result['queued']} 个 route job（会话共 {result['sessions']} 个，"
                  f"队列里已有 {result['already_queued']} 个）")
            print("接着跑 `python -m app.worker` 让后台慢慢抽；"
                  "抽完再用 --list-pending / --approve / --run-jobs 落库")
            return 0
        return _extract(db, args)
    finally:
        db.close_pool()


def _extract(db, args) -> int:
    from app.core.embedding import get_embedding_service
    from app.pipeline import router

    get_embedding_service().load()   # 栏位匹配要用（§5.3），先加载免得第一次调用超时
    stats = router.route_all(limit_sessions=args.limit_sessions,
                             limit_windows=args.limit_windows, only_session=args.session)
    report = stats.as_dict()
    print(f"\n会话 {report['sessions']} 个 / 窗口 {report['windows']} 个"
          f"（摘要跳过 {report['windows_skipped']}）/ LLM {report['calls']} 次"
          f" / {report['seconds']}s")
    print(f"判定 {report['items']} 条：{report['kinds']}")
    print(f"  → 进人工待办 {report['pending']} 条（其中栏位匹配不出 {report['section_unmatched']}）")
    print(f"  → 进四动作写入 {report['facts']} 条")
    print(f"  → 判定为丢弃 {report['discarded']} 条")
    if report["cross_org"]:
        print(f"★ 会话归属与栏位归属不一致 {report['cross_org']} 条"
              f"（多半是本机环境的事落到了 personal 的栏位）——§14 待审词表的观测信号")
    if report["parse_failed"]:
        print(f"⚠ LLM 输出不合 schema {report['parse_failed']} 次，素材已落待办，没丢")
    _print_stats(db, quiet=True)

    if args.run_jobs:
        _run_jobs()
    return 0


def _run_jobs() -> None:
    """★ 走的是生产用的那个 worker 循环（`jobs.run_worker`），不是另写一套执行器——
    否则 CLI 跑通了、真 worker 跑不通，等于没测。"""
    from app.queue import jobs
    from app.worker import HANDLERS

    print("\n── 执行队列 ──")

    def drained() -> bool:
        live = jobs.stats()
        return not (live.get("pending") or live.get("running"))

    if drained():
        print("  队列是空的")
        return
    jobs.run_worker(HANDLERS, idle_sleep=0.0, should_stop=drained)
    print(f"  队列剩余：{jobs.stats()}")


def _print_stats(db, *, quiet: bool = False) -> int:
    from app.pipeline import review

    if not quiet:
        print("── 待办队列 ──")
    for key, count in review.queue_stats().items():
        print(f"  {key:<22} {count}")
    if not quiet:
        print("\n── 改判率（★ 必须按 kind 分开看，§2.3）──")
    rows = review.override_stats()
    if not rows:
        print("  （还没有任何判定）")
    for row in rows:
        reviewed = row["reviewed"]
        rate = f"{row['overridden'] / reviewed:.0%}" if reviewed else "—"
        print(f"  {row['prompt_ver']:<14} {row['kind']:<12} 判了 {row['judged']:>4} 条 / "
              f"已审 {reviewed:>3} / 改判 {row['overridden']:>3}（{rate}） "
              f"平均置信 {row['avg_confidence']} 判错时 {row['avg_conf_when_wrong']}")
    return 0


def _list_pending(db) -> int:
    rows = db.query(
        "SELECT id, stage, status, reason, org_key, project_key, raw_content, suggestion"
        " FROM pending_items WHERE status IN ('pending','failed')"
        " ORDER BY id LIMIT 50")
    print(f"── 待办 {len(rows)} 条（最多列 50）──")
    for row in rows:
        suggestion = row["suggestion"] if isinstance(row["suggestion"], dict) \
            else json.loads(row["suggestion"] or "{}")
        print(f"#{row['id']} [{row['status']}/{row['stage']}] {row['reason']}")
        print(f"    归属：{row['org_key']} / {row['project_key'] or '—'}")
        print(f"    模型判：{suggestion.get('model_kind')}  预选：{suggestion.get('kind')}"
              f"  栏位：{suggestion.get('section_key') or '（没匹配出来）'}"
              f"  分数：{suggestion.get('section_score')}")
        print(f"    内容：{(suggestion.get('content') or row['raw_content'])[:160]}")
    return 0


def _adjudicate(db, args, *, approved: bool) -> int:
    from app.pipeline import review

    if not approved:
        ok = review.reject(args.reject, note=args.content)
        print(f"否决 #{args.reject}：{'成功' if ok else '失败（可能已被处理过）'}")
        return 0 if ok else 1

    resolution: dict = {}
    if args.kind:
        resolution["kind"] = args.kind
    if args.content:
        resolution["content"] = args.content
    if args.section:
        resolution["section_key"] = args.section
    job_id = review.approve(args.approve, resolution or None)
    if job_id is None:
        # 抢不到多半是"已经批准过了"。这时 `--run-jobs` 仍然要跑——
        # 上一批批准排出去的 job 可能还在队列里等着（我就在这上面白等了一轮）。
        print(f"批准 #{args.approve} 失败：已被处理过，或不存在")
        if args.run_jobs:
            _run_jobs()
        return 1
    print(f"批准 #{args.approve} → job {job_id}（跑 --run-jobs 落库）")
    if args.run_jobs:
        _run_jobs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
