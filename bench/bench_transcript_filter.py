"""按方案 §3 的过滤规则量一遍全量 transcript，把基线数字量出来。

★ 这里不自己写一份规则，而是 import 生产模块 `app.ingest.filters`——
量出来的基线和跑在生产里的判据必须是同一份代码，否则会漂：
上一版就是脚本里自己实现了一遍，结果 §3.1 只认 `str`，静默丢掉了
237 条以 `list[text]` 形态到达的真人输入。

用法：python bench/bench_transcript_filter.py
"""

import collections
import glob
import json
import os
import re
import sys
import statistics

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, BACKEND)

from app.ingest import filters  # noqa: E402

PROJECTS = os.path.expanduser("~/.claude/projects")


def normalize_cwd(cwd: str) -> str:
    """实测同一个目录会以 `C:\\...` 和 `c:\\...` 两种写法出现（43 个 cwd 里有一批是这种重复）。
    不归一化，workspace_map 会给同一个目录建两条缓存，归属还可能判得不一致。"""
    if not cwd:
        return ""
    out = cwd.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", out):
        out = out[0].upper() + out[1:]
    return out


def is_subagent(path: str) -> bool:
    return "/subagents/" in path.replace(os.sep, "/")


def pct(values: list[int], q: float) -> int:
    """简单分位数，不引 numpy。values 可为空。"""
    if not values:
        return 0
    return int(statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1])


def main() -> int:
    files = sorted(glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True))
    if not files:
        print(f"没找到 transcript：{PROJECTS}")
        return 1

    groups = (("主会话", [f for f in files if not is_subagent(f)]),
              ("子agent", [f for f in files if is_subagent(f)]))

    stats = {}
    cwds = collections.Counter()
    cwds_raw = collections.Counter()
    real_user_per_session = collections.Counter()
    sessions: set[str] = set()

    for label, group in groups:
        s = stats[label] = {
            "files": len(group), "total": 0, "kept": 0, "bytes": 0, "parse_err": 0,
            "roles": collections.Counter(),
            "dropped_user_prefix": collections.Counter(),
            "len": collections.defaultdict(list),
        }
        for path in group:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    s["total"] += 1
                    s["bytes"] += len(line.encode("utf-8", "replace"))
                    try:
                        d = json.loads(line)
                    except Exception:
                        s["parse_err"] += 1
                        continue
                    if not isinstance(d, dict):
                        continue

                    if label == "主会话":
                        # ★ 不要写 `d.get("sessionId") or basename` 这种兜底：
                        #   `file-history-*` 那类行没有 sessionId，兜底会把**文件名**
                        #   当成一个会话加进去，29 个文件就虚报成 59 个会话。
                        sid = d.get("sessionId")
                        if sid:
                            sessions.add(sid)
                        if d.get("cwd"):
                            cwds_raw[d["cwd"]] += 1
                            cwds[normalize_cwd(d["cwd"])] += 1

                    parsed = filters.parse_record(d)
                    if parsed is None:
                        # 统计"被挡掉的 user 行开头"，用来发现新的注入标记
                        if d.get("type") == "user" and not d.get("isMeta"):
                            head = filters.user_text(d)
                            if head:
                                s["dropped_user_prefix"][head.lstrip()[:24]] += 1
                        continue

                    s["kept"] += 1
                    s["roles"][parsed.role] += 1
                    s["len"][parsed.role].append(len(parsed.content))
                    if parsed.role == "user" and label == "主会话":
                        real_user_per_session[d["sessionId"]] += 1

    print(f"文件总数 {len(files)}：主会话 {len(groups[0][1])} + 子agent {len(groups[1][1])}")
    print("★ 方案里原先记的『72 个历史会话』把子 agent 文件也算进去了；")
    print("  而且这个语料是活的——文件会新增也会消失，绝对数字每次都不一样。")
    print()

    for label, s in stats.items():
        ratio = s["kept"] / s["total"] if s["total"] else 0
        print(f"── {label}：{s['files']} 文件 / {s['bytes'] / 1048576:.0f}MB / {s['total']} 行"
              f"（解析失败 {s['parse_err']}）")
        print(f"   保留 {s['kept']} 行 → 信噪比 {ratio:.1%}")
        for role in filters.ROLES:
            n = s["roles"][role]
            if not n:
                continue
            lens = s["len"][role]
            print(f"      {role:<10} {n:>6}   长度 p50={pct(lens, .5)} p95={pct(lens, .95)} max={max(lens)}")
        if s["dropped_user_prefix"]:
            top = s["dropped_user_prefix"].most_common(6)
            print(f"   被挡掉的 user 行开头（前 6）：{top}")
        print()

    m = stats["主会话"]
    user_lens = m["len"]["user"]
    print(f"★ 主会话真实用户输入 {m['roles']['user']} 条 / {len(sessions)} 会话"
          f"   （方案 §13 旧基线写的是 146 条 / 72 会话）")
    print(f"★ 主会话信噪比 {m['kept'] / max(m['total'], 1):.1%}   （方案 §13 旧基线写的是 14%）")
    print(f"★ 回归判据：真人输入长度 p50={pct(user_lens, .5)} 字符。")
    print("  这个数应该停在几十字符（人打字就是这么长）。一旦涨到几千，")
    print("  说明有新版本的注入包装漏进来了——filters.INJECTED_PREFIXES 要补。")
    zero = len(sessions) - len(real_user_per_session)
    print(f"  一条真实用户输入都没有的主会话：{zero} / {len(sessions)}")
    print(f"  真实用户输入最多的会话：{[(s[:8], n) for s, n in real_user_per_session.most_common(5)]}")
    print()
    print(f"★ cwd 归一化：原始 {len(cwds_raw)} 种写法 → 归一化后 {len(cwds)} 个真实目录"
          f"（差值就是盘符大小写造成的重复）")
    for cwd, n in cwds.most_common(14):
        print(f"   {n:>6}  {cwd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
