"""Claude Code transcript 的发现与解析（§3.5）。

★ 布局实测：`~/.claude/projects` 下 73 个 jsonl = **30 个主会话 + 43 个子 agent**。
子 agent 文件在深度 4 的 `<会话>/subagents/agent-*.jsonl`，**只扫一层 glob 会全部漏掉**。

★ P1 只扫主会话：子 agent 的 `user` 行长度 p50 = 1191 字符，全是父 agent 写的任务书，
当成人类意图会污染素材（§3.1）。它们的 thinking 是否值得纳入，等有评测集再说（§13 P7）。

★ fork 影子副本也不扫（decisions-log #110）：Claude Code fork/rewind 会话时生成的新文件
首行带 `forkedFrom`，且**全部 uuid 被重写**——实测 92250661（fork 自 5813b12c）2590 个
uuid 与库中 src_uuid 零重叠，`UNIQUE(source, src_uuid)` 幂等挡不住；而内容与父会话 98%
相同，入库会让 router 当新素材重抽一遍、排出成批重复待审卡。副本的少量独有内容多半是
父会话还没扫到的最后几条消息，父会话下次扫描自然覆盖。
"""

from __future__ import annotations

import glob
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SOURCE = "claude-code"

DEFAULT_ROOT = Path(os.path.expanduser("~/.claude/projects"))


@dataclass(frozen=True)
class Transcript:
    path: str
    is_subagent: bool
    project_dir: str  # ~/.claude/projects 下的第一层目录名（转义过的 cwd）
    is_fork: bool = False  # 首行带 forkedFrom 的影子副本，见模块 docstring


def _is_forked(path: str) -> bool:
    """首行是否带 `forkedFrom` 键。fork 元数据在文件创建时写入，只看首行即可；
    空文件/首行坏 JSON 一律按"不是 fork"处理（宁可入库重复，不可静默漏扫真会话）。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
        record = json.loads(first)
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(record, dict) and "forkedFrom" in record


def discover(root: Path | str = DEFAULT_ROOT) -> list[Transcript]:
    root = Path(root)
    out = []
    for path in sorted(glob.glob(str(root / "**" / "*.jsonl"), recursive=True)):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        parts = rel.split("/")
        is_subagent = "subagents" in parts[:-1]
        out.append(
            Transcript(
                path=path,
                is_subagent=is_subagent,
                project_dir=parts[0] if len(parts) > 1 else "",
                is_fork=not is_subagent and _is_forked(path),
            )
        )
    return out


def iter_records(path: str | Path) -> Iterator[dict]:
    """逐行读一个 jsonl，只 yield 解析成功的 dict。

    ★ **末尾的半行必须跳过而不是报错**：语料是活的，活跃会话的文件正在被追加，
    两次扫描隔几分钟就差几十行（C.6）。半行 JSON 解析必然失败，属正常情况。
    ★ 但尾行**不以 `\n` 结束 ≠ 半行**（EXPERT-REVIEW A2）：也可能是写完了
    整个文件、写入端就是没补结尾换行——直接 return 会漏采这条完整记录，
    直到下次追加才入库。所以先试着解析：JSON 对象的合法前缀不可能恰好是
    完整对象，解析成功即说明该行完整；即便写入端正在补换行，下次重扫也有
    `UNIQUE(source, src_uuid)` 挡重复。
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.endswith("\n"):
                line = line.strip()
                if line:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        return  # 未写完的尾行，下次扫描再取
                    if isinstance(record, dict):
                        yield record
                return
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def parse_ts(record: dict) -> datetime | None:
    """transcript 的 `timestamp` 实测每行都有，形如 `2026-10-08T03:30:11.939Z`。"""
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


_CJK = ((0x3000, 0x303F), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF))


def estimate_tokens(text: str) -> int:
    """粗估 token 数，用于成本统计（不参与任何判断，所以不需要精确）。

    中日韩字符按 1 token/字算，其余按 4 字符/token。
    ★ 不用 `message.usage.output_tokens`：那个数覆盖 thinking+text+tool_use 全部输出，
    而我们入库的只是其中一种块，拿它当这一行的 token 数会系统性高估。
    """
    cjk = sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _CJK))
    return cjk + (len(text) - cjk + 3) // 4
