"""§5.2 窗口切分：以真人输入为锚点，把 `conversation_log` 切成一段段待判定的素材。

★ 纯函数，不碰数据库：取数是 router 的事，切分在这里，这样才能用假数据测边界。

两级 thinking 过滤的第 2 级在这里做（第 1 级"信号词"在 `ingest/filters.py` 入库时就做了）：
实测 855 条含信号词的 thinking，只有 **255 条（30%）** 落在真人输入 ±2 轮内（§5.4）。
"""

from __future__ import annotations

from dataclasses import dataclass

# §5.4 实测的窗口宽度可调参数：±1→137、±2→255、±3→337、±5→441。
# 先按 ±2 跑，等 P5 攒够否决数据再决定要不要放宽。
TURN_RADIUS = 2

# 锚点角色。★ `summary` 也算锚点，但它**只跟自己成一个窗口**：
# 压缩摘要是整段历史的浓缩，把它和后续对话混在一个窗口里，
# 模型分不清哪句是刚发生的、哪句是三天前的（§3.3「路由时单独处理」）。
ANCHOR_ROLES = ("user", "summary")

ROW_PREFIX = {
    "user": "用户",
    "assistant": "AI",
    "thinking": "AI 内心独白",
    "summary": "上下文压缩摘要",
}


@dataclass(frozen=True)
class Row:
    id: int
    role: str
    content: str
    turn_no: int
    tokens_est: int


@dataclass(frozen=True)
class Window:
    """一段待路由的素材。`anchor_id` 就是写库时的 `source_msg`。"""

    anchor_id: int
    rows: tuple[Row, ...]
    is_summary: bool = False

    @property
    def tokens(self) -> int:
        return sum(r.tokens_est for r in self.rows)

    def as_text(self) -> str:
        return "\n\n".join(f"【{ROW_PREFIX.get(r.role, r.role)}】{r.content}" for r in self.rows)


def _anchor_turns(rows: list[Row]) -> list[int]:
    return [r.turn_no for r in rows if r.role in ANCHOR_ROLES]


def _near_anchor(row: Row, anchors: list[int]) -> bool:
    return any(abs(row.turn_no - a) <= TURN_RADIUS for a in anchors)


def slice_windows(rows: list[Row], *, token_budget: int = 0) -> list[Window]:
    """把一个会话的行切成窗口。`rows` 必须按 `id` 升序且同属一个会话。

    ★ 传进来的应该是**整个会话**的行，不是游标之后的增量：
      thinking 的 ±2 轮判定要看它前后有没有真人输入，而那个锚点可能已经在上一次
      抽取里消费过了。切完再由 router 按 `anchor_id > last_log_id` 丢掉旧窗口，
      比给切分函数补一堆"回看几行"的参数简单，也不会漏。

    `token_budget` > 0 时把超大窗口再切小（单个会话里一轮对话可能拖着上百条 AI 回复）。
    """
    anchors = _anchor_turns(rows)
    out: list[Window] = []
    bucket: list[Row] = []

    def flush() -> None:
        if not bucket:
            return
        anchor = bucket[0]
        for chunk in _chunks(list(bucket), token_budget):
            out.append(Window(anchor_id=chunk[0].id, rows=tuple(chunk),
                              is_summary=anchor.role == "summary"))
        bucket.clear()

    for row in rows:
        if row.role in ANCHOR_ROLES:
            flush()
            bucket.append(row)
            if row.role == "summary":
                flush()  # 摘要单独成窗，后面接着攒下一个窗口
            continue
        if row.role == "thinking" and not _near_anchor(row, anchors):
            continue  # 第 2 级筛：离真人输入太远的内心独白不要
        if not bucket:
            continue  # 会话开头的孤儿行（第一个锚点之前），没有人的上下文，不判
        bucket.append(row)
    flush()
    return out


def _chunks(rows: list[Row], budget: int) -> list[list[Row]]:
    if budget <= 0:
        return [rows]
    out: list[list[Row]] = []
    current: list[Row] = []
    total = 0
    for row in rows:
        # ★ 单行就超预算也只能整行放进去——一行是一个不可分的语义单位，
        #   从中间截断会让模型判到半句话。
        if current and total + row.tokens_est > budget:
            out.append(current)
            current, total = [], 0
        current.append(row)
        total += row.tokens_est
    if current:
        out.append(current)
    return out
