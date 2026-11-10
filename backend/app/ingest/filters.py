"""§3 的入库过滤规则——纯函数，不碰数据库。

★ 这里的清单不是拍脑袋写的，是把全量 72 个 transcript 里 user 行的开头标记
穷举出来后定的（`bench/bench_transcript_filter.py`）。新版本 Claude Code
可能加新标记；**发现漏网的判据是"真人输入长度 p50 突然涨到几千字符"**。

★ 不能用"以 < 或 [ 开头就丢"这种粗暴规则：实测有人粘贴的日志正好以
`[INFO ]` 开头，那是真素材。所以只挡下面这些确切的注入标记。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Claude Code / IDE / 后台任务注入的包装，不是人打的
INJECTED_PREFIXES: tuple[str, ...] = (
    "<command-name>",
    "<command-message>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<system-reminder>",
    "<task-notification>",
    "<ide_opened_file>",
    "<ide_selection>",
    "[SYSTEM NOTIFICATION",              # 任务通知在别的版本里的外层包装
    "[Request interrupted by user]",     # 用户按了中断，是信号不是内容
)

# §3.3 上下文压缩摘要：信号极密，单独存 role='summary'
SUMMARY_PREFIX = "This session is being continued from"

# §3.2 第一级：thinking 规则预筛的信号词
SIGNAL_PATTERN = re.compile(
    r"不要|不用|改为|应该|必须|否决|放弃|偏好|约定|踩坑"
    r"|don't|instead|prefer|convention|reject",
    re.I,
)

ROLES = ("user", "assistant", "thinking", "summary")


@dataclass(frozen=True)
class Parsed:
    role: str
    content: str


def user_text(record: dict) -> str | None:
    """把 user 行的文本取出来；取不到（tool_result 之类）返回 None。

    ★ content 有两种形态，实测 str 267 条、list[text] 241 条。
    只认 str 会静默丢掉 237 条真人输入（list 那批 isMeta=False 的几乎全是人话）。
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        kinds = {b.get("type") for b in blocks}
        if blocks and kinds == {"text"}:
            return " ".join(b.get("text", "") for b in blocks)
    return None


def _blocks(record: dict, kind: str) -> list[str]:
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return []
    return [
        b.get(kind, "")
        for b in message["content"]
        if isinstance(b, dict) and b.get("type") == kind
    ]


def classify_user(record: dict) -> Parsed | None:
    if record.get("type") != "user" or record.get("isMeta"):
        return None
    text = user_text(record)
    if text is None:
        return None
    stripped = text.lstrip()
    if not stripped:
        return None
    if any(stripped.startswith(prefix) for prefix in INJECTED_PREFIXES):
        return None
    role = "summary" if stripped.startswith(SUMMARY_PREFIX) else "user"
    return Parsed(role=role, content=stripped)


def has_signal(text: str) -> bool:
    """§3.2 第一级筛。第二级（窗口邻近）在抽取阶段做，见 §5.2。"""
    return bool(SIGNAL_PATTERN.search(text))


def parse_record(record: dict) -> Parsed | None:
    """一条 transcript 记录 → 该存什么，None 表示丢弃。

    ★ 实测 assistant 记录从不混块类型（tool_use 6476 / thinking 5052 / text 1803
    各自独立），所以一条记录最多产出一行，`UNIQUE(source, src_uuid)` 不会撞。
    ★ 但那是**外部假设**，Claude Code 升个版本就可能变——混块不再静默丢 text
    （EXPERT-REVIEW A2）：能拼的拼成一行，拼不了的至少保住 text，并打 WARN。
    日志里这条 WARN 的计数非零时，要回来重审 §3 的过滤规则。
    """
    kind = record.get("type")
    if kind == "user":
        return classify_user(record)
    if kind != "assistant":
        return None

    thinking = _blocks(record, "thinking")
    text_blocks = _blocks(record, "text")

    if thinking and text_blocks:
        log.warning("assistant 记录 thinking+text 混块（uuid=%s），不再静默丢 text",
                    record.get("uuid"))
        think_text = "\n".join(t for t in thinking if t).strip()
        say_text = "\n".join(t for t in text_blocks if t).strip()
        if think_text and has_signal(think_text):
            # 一行只能存一种（UNIQUE(source, src_uuid)），拼接两块都不丢
            combined = f"{think_text}\n\n{say_text}" if say_text else think_text
            return Parsed(role="thinking", content=combined)
        # thinking 没信号词（本就不入库），但 text 单独出现时是入库的——保住它
        return Parsed(role="assistant", content=say_text) if say_text else None

    if thinking:
        text = "\n".join(t for t in thinking if t).strip()
        # §3.2 第一级：没信号词的 thinking 不入库（5037 → 857）
        return Parsed(role="thinking", content=text) if text and has_signal(text) else None

    if text_blocks:
        text = "\n".join(t for t in text_blocks if t).strip()
        return Parsed(role="assistant", content=text) if text else None

    return None  # 只有 tool_use ⇒ 工具调用参数，丢
