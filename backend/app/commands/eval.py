"""召回评测命令（EXPERT-REVIEW Phase 1.1 建评测集 / 1.2 跑批与基线）。

用法：
    python -m app.commands.eval --gen-candidates              # LLM 从真记忆生成 query 候选清单
    python -m app.commands.eval --gen-candidates --per-memory 3
    python -m app.commands.eval --import eval-candidates.md   # 人工确认后导入（幂等判重）
    python -m app.commands.eval --run                         # 跑全量 → hit@5/MRR@10/p50/p95 → 落 eval_runs
    python -m app.commands.eval --show                        # 用例统计 + 最近几次跑批结果

★ --gen-candidates 只调 LLM；--run 要加载 bge-m3（冷启动 15~33s，跑批前会预热）。
★ 候选清单**必须经人工确认**（改/删/补块）再 --import：评测集是后续一切调优的
  唯一裁判，裁判不能是模型自说自话。清单是行协议 markdown，直接文本编辑即可。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from ..core import db
from ..evaluation import (DEFAULT_CANDIDATES_FILE, CaseDraft, import_cases,
                          latest_run, parse_candidate_file,
                          render_candidate_file, run_eval)

_BATCH_SIZE = 8


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="召回评测：建用例、跑批、看基线")
    parser.add_argument("--gen-candidates", action="store_true",
                        help="用 LLM 从 active 记忆生成 query 候选清单（人工确认后再导入）")
    parser.add_argument("--per-memory", type=int, default=2,
                        help="每条记忆生成几个候选 query（默认 2）")
    parser.add_argument("--out", default=str(DEFAULT_CANDIDATES_FILE),
                        help="候选清单输出路径（默认项目根 eval-candidates.md）")
    parser.add_argument("--import", dest="import_file", default=None, metavar="FILE",
                        help="把人工确认过的候选清单导入 eval_recall_cases")
    parser.add_argument("--run", action="store_true",
                        help="跑全量评测并落 eval_runs（加载 bge-m3）")
    parser.add_argument("--k", type=int, default=None, help="覆盖 recall.k（默认用配置值）")
    parser.add_argument("--show", action="store_true", help="用例统计 + 最近跑批结果")
    parser.add_argument("--harvest", type=int, default=None, metavar="N",
                        help="从 recall_log 采 N 条真实 query 出候选清单（待人工标注），"
                             "评测扩容的起点（ROADMAP B1：出题不再全靠模型）")
    parser.add_argument("--dump", action="store_true",
                        help="打印 active 记忆的 #id + hash 前 12 位 + 内容（标注 hash 用例的对照表）")
    parser.add_argument("--check", action="store_true",
                        help="健康检查：期望答案已失效（记忆被删/合并/改写）的用例清单")
    args = parser.parse_args(argv)

    if args.gen_candidates:
        return _gen_candidates(args.per_memory, Path(args.out))
    if args.import_file:
        return _import(Path(args.import_file))
    if args.run:
        return _run(args.k)
    if args.show:
        return _show()
    if args.harvest is not None:
        return _harvest(args.harvest, Path(args.out))
    if args.dump:
        return _dump()
    if args.check:
        return _check()
    parser.print_help()
    return 0


# ─────────────────── --gen-candidates ───────────────────

def _gen_candidates(per_memory: int, out: Path) -> int:
    from ..core.llm import get_llm_client
    from ..prompts import eval_gen

    memories = db.query(
        "SELECT id, scope, org_key, project_key, content FROM memories"
        " WHERE status = 'active' AND embedding IS NOT NULL ORDER BY id")
    if not memories:
        print("没有 active 记忆，无从生成。先跑抽取。")
        return 1

    client = get_llm_client()
    by_index = {no: mem for no, mem in enumerate(memories, start=1)}
    drafts: list[CaseDraft] = []
    queries_by_memory: dict[int, list[str]] = {}

    batches = [list(range(i, min(i + _BATCH_SIZE, len(memories) + 1)))
               for i in range(1, len(memories) + 1, _BATCH_SIZE)]
    for batch_no, indexes in enumerate(batches, start=1):
        payload = [{"index": no, "content": by_index[no]["content"]} for no in indexes]
        print(f"LLM 生成候选 {batch_no}/{len(batches)}（记忆 {indexes[0]}–{indexes[-1]}）...")
        result = client.structured(
            "eval_gen", system=eval_gen.SYSTEM,
            messages=[{"role": "user", "content": eval_gen.user_message(payload)}],
            tool=eval_gen.TOOL, schema=eval_gen.GenResult)
        for item in result.items:
            queries_by_memory[item.index] = list(item.queries)[:per_memory]

    for no, mem in by_index.items():
        for query in queries_by_memory.get(no, []):
            drafts.append(CaseDraft(
                query=query.replace("\n", " "), expect=[mem["id"]], org=mem["org_key"],
                project=mem["project_key"], label=f"记忆#{mem['id']} 换说法",
                note=f"原记忆 #{mem['id']}：{mem['content'][:100]}".replace("\n", " ")))

    drafts += _cross_org_negatives(by_index, queries_by_memory)

    header = [
        "# eval_recall_cases 候选清单",
        f"<!-- 生成：{datetime.now():%Y-%m-%d %H:%M} | python -m app.commands.eval --gen-candidates -->",
        "<!-- 人工确认（改 query / 删块 / 补块）后导入： -->",
        "<!--   python -m app.commands.eval --import eval-candidates.md -->",
        "<!-- 字段：query 必填；expect=期望命中的记忆 id；must_not=绝不该出现的 id（否定例）； -->",
        "<!-- org/project = 召回时的上下文（可空）；HTML 注释行导入时忽略。 -->",
    ]
    out.write_text(render_candidate_file(drafts, header), encoding="utf-8")
    positives = sum(1 for d in drafts if d.expect)
    print(f"\n已写 {out}：{len(drafts)} 条候选（正例 {positives}，否定例 {len(drafts) - positives}）。")
    print("★ 请人工确认/修订后再 --import——评测集是所有调优的裁判，不能模型自说自话。")
    return 0


def _cross_org_negatives(by_index: dict[int, dict],
                         queries_by_memory: dict[int, list[str]]) -> list[CaseDraft]:
    """串台否定例（§6.1）：拿 A org 记忆的 query，放到 B org 的常用上下文里，
    must_not=那条记忆——org/project 过滤器被改坏时这里会红。
    scope='user' 的记忆任何上下文都合法可见，不能当否定素材。"""
    orgs = sorted({m["org_key"] for m in by_index.values() if m["org_key"]})
    if len(orgs) < 2:
        return []
    # 每个 org 取记忆最多的 project 作为它的典型上下文
    top_context: dict[str, str | None] = {}
    for org in orgs:
        row = db.query_one(
            "SELECT project_key FROM memories WHERE status='active' AND org_key=%s"
            " GROUP BY project_key ORDER BY count(*) DESC LIMIT 1", (org,))
        top_context[org] = row["project_key"] if row else None

    drafts: list[CaseDraft] = []
    for org in orgs:
        other = next(o for o in orgs if o != org)
        victim = query = None
        for no in sorted(by_index):
            mem = by_index[no]
            if (mem["org_key"] == org and mem["scope"] != "user"
                    and queries_by_memory.get(no)):
                victim, query = mem, queries_by_memory[no][0]
                break
        if not victim:
            continue
        drafts.append(CaseDraft(
            query=query.replace("\n", " "), must_not=[victim["id"]], org=other,
            project=top_context[other],
            label=f"跨org串台（{org} 记忆不得在 {other} 上下文出现）",
            note=f"原记忆 #{victim['id']}（{org}）：{victim['content'][:80]}".replace("\n", " ")))
    return drafts


# ─────────────────── --import ───────────────────

def _import(path: Path) -> int:
    if not path.is_file():
        print(f"文件不存在：{path}")
        return 1
    drafts, errors = parse_candidate_file(path.read_text(encoding="utf-8"))
    for err in errors:
        print(f"[跳过] {err}")
    if not drafts:
        print("没有可导入的用例。")
        return 1
    result = import_cases(drafts)
    print(f"导入完成：新增 {result['inserted']}，判重跳过 {result['skipped']}，"
          f"格式问题 {len(errors)}。")
    return 0


# ─────────────────── --harvest：从真实提问扩容 ───────────────────

def _harvest(n: int, out: Path) -> int:
    """从 recall_log 采真实 query 出候选清单（ROADMAP B1：出题不再全靠模型）。

    每条附 note=当时注入的记忆 id——标注者从"系统当时带了什么"入手判断
    "该不该带、带对了吗"，比凭空出题快得多。expect/must_not 留空，人工标注。

    ★ B6：用户在 UI 上标过 'useless' 的行优先全采（复查池的本体），
      不足 n 条再用普通采样补齐——负标记 = 用户亲手说"这轮带错了"，
      是最高价值的出题素材，不该和普通行混在一个采样池里排队。
    """
    useless = db.query(
        "SELECT btrim(query_text) AS q, org_key, project_key, injected_ids"
        " FROM recall_log"
        " WHERE feedback = 'useless' AND length(btrim(query_text)) >= 2"
        " ORDER BY created_at DESC LIMIT %s", (n,))
    rest = n - len(useless)
    rows = list(useless)
    if rest > 0:
        rows += db.query(
            "SELECT DISTINCT ON (btrim(query_text))"
            "  btrim(query_text) AS q, org_key, project_key, injected_ids"
            " FROM recall_log"
            " WHERE session_id <> 'eval-runner' AND length(btrim(query_text)) >= 2"
            " ORDER BY btrim(query_text), created_at DESC"
            " LIMIT %s", (rest,))
    if not rows:
        print("recall_log 里还没有真实 query（先用一段时间，或 hook 没接上）。")
        return 1
    # 行协议装不下多行/超长 query：压平空白 + 截断（贴 JSON 的原样 query 不是好评测题）
    def _flat(q: str) -> str:
        flat = " ".join(q.split())
        return flat[:400] + "…" if len(flat) > 400 else flat

    drafts = [CaseDraft(
        query=_flat(row["q"]), org=row["org_key"], project=row["project_key"],
        label="真实提问（待标注）",
        note=("当时注入：" + " ".join(f"#{i}" for i in (row["injected_ids"] or [])[:5])
              or "当时无注入")) for row in rows]
    header = [
        "# eval_recall_cases 候选清单（真实提问采集）",
        f"<!-- 采集：{datetime.now():%Y-%m-%d %H:%M} | python -m app.commands.eval --harvest {n} -->",
        "<!-- 每块的 note 列出了系统当时实际注入的记忆 id（标注起点，不判定对错）。 -->",
        "<!-- 标注方法：对照 --dump 的记忆表，给 query 写 expect（该召回的 id）或 expect_hash； -->",
        "<!-- 口语 query 就保留口语——真实分布正是要考的部分。标完 --import。 -->",
    ]
    out.write_text(render_candidate_file(drafts, header), encoding="utf-8")
    print(f"已写 {out}：{len(drafts)} 条真实 query（全部待标注）。")
    print("★ 标注后 --import；注：--import 不会动已有用例（幂等判重）。")
    return 0


def _dump() -> int:
    rows = db.query(
        "SELECT id, content_hash, scope, org_key, project_key, content FROM memories"
        " WHERE status='active' ORDER BY id")
    for row in rows:
        ctx = row["org_key"] or ""
        if row["project_key"]:
            ctx += f" / {row['project_key']}"
        print(f"#{row['id']} [{row['scope']}] {ctx} {row['content_hash'][:12]}"
              f"  {row['content'][:80].replace(chr(10), ' ')}")
    print(f"\\n共 {len(rows)} 条 active 记忆。hash 列写前 12 位即可（前缀匹配）。")
    return 0


def _check() -> int:
    stale = stale_cases()
    if not stale:
        print("健康：所有用例的期望答案都能对上 active 记忆。")
        return 0
    print(f"★ {len(stale)} 条用例的期望答案已失效（记忆被删/合并/改写），评测前先修订：")
    for item in stale:
        print(f"  case#{item['case_id']} {item['query'][:50]}"
              f"  expected_ids={item['expected_ids'] or '[]'}"
              f"  expected_hashes={[h[:12] for h in item['expected_hashes']] or '[]'}")
    return 0


# ─────────────────── --run ───────────────────

def _run(k: int | None) -> int:
    summary = run_eval(k=k)
    print(f"\n用例 {summary.total}（正例 {summary.positives}，否定例 {summary.negatives}）")
    print(f"hit@5   : {fmt(summary.hit_at_5)}")
    print(f"MRR@10  : {fmt(summary.mrr_at_10)}")
    print(f"否定泄漏: {fmt(summary.leak_rate)}"
          + (f"（{summary.leak_count}/{summary.negatives}）" if summary.negatives else ""))
    print(f"延迟    : p50={summary.p50_ms}ms p95={summary.p95_ms}ms（预算 400ms）")
    if summary.missed:
        print(f"\n未命中/泄漏 {len(summary.missed)} 条：")
        for item in summary.missed:
            print(f"  case#{item['case_id']} {item['query'][:50]} → got {item['got']}")
    stale = stale_cases()
    if stale:
        print(f"★ {len(stale)} 条用例的期望答案已失效（--check 看明细）——"
              f"hit@5 已被它们拖低，先修订再当基线用。")
    print(f"\n已落 eval_runs #{summary.run_id}")
    return 0


def fmt(value: float | None) -> str:
    return "待测（该类用例为 0）" if value is None else f"{value:.3f}"


# ─────────────────── --show ───────────────────

def _show() -> int:
    stat = db.query_one(
        "SELECT count(*) n,"
        " count(*) FILTER (WHERE expected_ids <> '{}') positives,"
        " count(*) FILTER (WHERE must_not_ids <> '{}') negatives"
        " FROM eval_recall_cases")
    print(f"用例：{stat['n']}（正例 {stat['positives']}，否定例 {stat['negatives']}）")
    run = latest_run()
    if not run:
        print("eval_runs：还没跑过（--run）")
        return 0
    print(f"最近一次 #{run['id']} @ {run['run_at']:%Y-%m-%d %H:%M}：")
    print(f"  hit@5={fmt(run['hit_at_5'])}  MRR@10={fmt(run['mrr'])}"
          f"  p95={run['p95_ms']}ms  {run['notes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
