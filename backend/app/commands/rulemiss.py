"""规矩漏判率人工抽样（plan §10 / EXPERT-REVIEW Roadmap 1.4——Phase 1 最后一项）。

用法：
    python -m app.commands.rulemiss --gen      # 全候选池 → 项目根 rule-miss-sample.md
    python -m app.commands.rulemiss --score    # 人工判定完成后计分出漏判率

背景：memories 表只存**自动入库**的零碎记忆；规矩类本该路由成 observation 走
人审、批准后进 living_doc（100% 注入）。被误判成 fact/preference 落进 memories
的规矩 = **静默漏判**——不进 pending、无人改判、任何自动指标都看不见，只能靠
本抽样：筛 content 含规范性措辞（与 metrics._rule_miss_candidates 同一口径），
人工逐条判"到底是不是规矩"。

判定值（改每条的「判定: 待定」行）：
    规矩     = 长期规范性要求 → 净漏判
    已成文   = 是规矩但 living_doc 已覆盖 → 曾漏判、已被补救，单列
    零碎     = 一次性事实/偏好陈述 → 非漏判
    剔除     = 该条本身不该存在（错误记忆等）→ 不计入分母

漏判率口径：净漏判率 = 规矩/(总数-剔除)；宽口径（曾漏判）= (规矩+已成文)/(总数-剔除)。
★ --gen 遇到已存在的清单文件会拒绝覆盖（里面可能有人工判定结果），--force 才覆盖。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from ..core import db
from ..metrics import _NORMATIVE_WORDS

DEFAULT_SAMPLE_FILE = Path(__file__).resolve().parents[3] / "rule-miss-sample.md"
VERDICTS = ("待定", "规矩", "已成文", "零碎", "剔除")

_HEADER = """# 规矩漏判率抽样清单

> 生成：{ts} · 候选池 {n} 条全量（plan §10 设计样本量 30，池接近样本量时全查）
> 口径：active 记忆正文含规范性措辞（{words}），与 Metrics 页候选池同源。
> memories 表只存自动入库的零碎记忆；规矩本该走人审进 living_doc（100% 注入），
> 落进这里 = 静默漏判。

## 怎么判（改每条末尾的「判定: 待定」）
- `规矩` = 长期规范性要求 → 净漏判
- `已成文` = 是规矩但 living_doc 已覆盖 → 曾漏判已补救，单列
- `零碎` = 一次性事实/偏好陈述 → 非漏判
- `剔除` = 该条本身不该存在 → 不计入分母

改完跑：`python -m app.commands.rulemiss --score`
"""


def _fetch_pool() -> list[dict]:
    """全候选池 + source_msg 溯源（会话与时间，帮人工回忆上下文）。"""
    p: dict = {}
    # ★ m. 前缀必须有：JOIN conversation_log 后两边都有 content 列，裸写会歧义
    cond = " OR ".join([f"m.content LIKE %(nw{i})s" for i in range(len(_NORMATIVE_WORDS))])
    p.update({f"nw{i}": f"%{w}%" for i, w in enumerate(_NORMATIVE_WORDS)})
    return db.query(f"""
        SELECT m.id, m.scope, m.org_key, m.project_key, m.subject, m.role,
               m.content, m.why, m.created_at,
               c.session_id, coalesce(c.msg_ts, c.ingested_at) AS msg_at
          FROM memories m
          LEFT JOIN conversation_log c ON c.id = m.source_msg
         WHERE m.status='active' AND ({cond})
         ORDER BY m.id
    """, p)


def render_sample(rows: list[dict]) -> str:
    parts = [_HEADER.format(ts=datetime.now().strftime("%Y-%m-%d %H:%M"),
                            n=len(rows), words="/".join(_NORMATIVE_WORDS))]
    for r in rows:
        if r.get("session_id"):
            ts = f" · {r['msg_at']:%Y-%m-%d}" if r.get("msg_at") else ""
            src = f"session {r['session_id']}{ts}"
        else:
            src = "（无溯源）"
        where = "/".join(x for x in (r["org_key"], r["project_key"]) if x) or "全库"
        why = f"\n- why: {r['why']}" if r.get("why") else ""
        parts.append(
            f"## #{r['id']} · {r['role']}/{r['subject']} · {where}"
            f" · {r['created_at']:%Y-%m-%d}\n"
            f"- content: {_oneline(r['content'])}{why}\n"
            f"- 来源: {src}\n"
            f"- 判定: 待定\n")
    return "\n".join(parts)


def _oneline(text: str) -> str:
    return text.replace("\n", " ").strip()


_BLOCK_RE = re.compile(r"^## #(\d+)", re.MULTILINE)
_VERDICT_RE = re.compile(r"^- 判定:\s*(\S+)\s*$", re.MULTILINE)


def parse_verdicts(text: str) -> tuple[dict[int, str], list[int]]:
    """解析清单 → {memory_id: 判定}；非法判定值的块 id 进 errors。"""
    verdicts: dict[int, str] = {}
    errors: list[int] = []
    blocks = _BLOCK_RE.split(text)
    # split 结果: [前言, id1, 块1, id2, 块2, ...]
    for i in range(1, len(blocks) - 1, 2):
        mem_id, body = int(blocks[i]), blocks[i + 1]
        m = _VERDICT_RE.search(body)
        verdict = m.group(1) if m else "待定"
        if verdict not in VERDICTS:
            errors.append(mem_id)
            continue
        verdicts[mem_id] = verdict
    return verdicts, errors


def score(verdicts: dict[int, str]) -> dict:
    total = len(verdicts)
    counts = {v: sum(1 for x in verdicts.values() if x == v) for v in VERDICTS}
    denom = total - counts["剔除"]
    return {
        "total": total, **counts, "denominator": denom,
        "net_miss_rate": (counts["规矩"] / denom) if denom else None,
        "broad_miss_rate": ((counts["规矩"] + counts["已成文"]) / denom) if denom else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规矩漏判率人工抽样（plan §10）")
    parser.add_argument("--gen", action="store_true", help="生成抽样清单（全候选池）")
    parser.add_argument("--out", default=str(DEFAULT_SAMPLE_FILE),
                        help="清单路径（默认项目根 rule-miss-sample.md）")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的清单")
    parser.add_argument("--score", action="store_true", help="解析人工判定并计算漏判率")
    args = parser.parse_args(argv)
    out = Path(args.out)

    if args.gen:
        if out.exists() and not args.force:
            print(f"★ {out} 已存在（可能含人工判定），拒绝覆盖；确要重生成加 --force",
                  file=sys.stderr)
            return 1
        db.init_pool()
        try:
            rows = _fetch_pool()
        finally:
            db.close_pool()
        if not rows:
            print("候选池为空——没有含规范性措辞的 active 记忆，无需抽样")
            return 0
        out.write_text(render_sample(rows), encoding="utf-8")
        print(f"已生成 {out}：{len(rows)} 条待人工判定（改「判定: 待定」行，然后 --score）")
        return 0

    if args.score:
        if not out.exists():
            print(f"清单不存在: {out}（先 --gen）", file=sys.stderr)
            return 1
        verdicts, errors = parse_verdicts(out.read_text(encoding="utf-8"))
        for mem_id in errors:
            print(f"★ #{mem_id} 判定值非法（可选：{'/'.join(VERDICTS)}），未计入",
                  file=sys.stderr)
        s = score(verdicts)
        pending = s["待定"]
        print(f"总数 {s['total']}（剔除 {s['剔除']}，分母 {s['denominator']}）")
        print(f"  规矩 {s['规矩']} · 已成文 {s['已成文']} · 零碎 {s['零碎']}")
        if s["net_miss_rate"] is None:
            print("★ 分母为 0，漏判率无意义")
        else:
            print(f"净漏判率 = {s['net_miss_rate']:.1%}（规矩/分母）")
            print(f"宽口径（曾漏判）= {s['broad_miss_rate']:.1%}（(规矩+已成文)/分母）")
        if pending:
            print(f"\n★ 还有 {pending} 条「待定」未判——比率只反映已判定部分")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
