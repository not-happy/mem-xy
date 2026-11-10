"""§5.2 窗口切分：纯函数，不碰数据库。

每条测试对应一个实测结论或一个会静默丢素材的边界，见 docstring。
"""

import pytest

from app.pipeline.windows import TURN_RADIUS, Row, slice_windows


def row(id_, role, content="内容", turn_no=None, tokens=100):
    return Row(id=id_, role=role, content=content,
               turn_no=id_ if turn_no is None else turn_no, tokens_est=tokens)


def test_one_user_turn_gathers_the_assistant_replies_after_it():
    rows = [row(1, "user", "改一下"), row(2, "assistant"), row(3, "assistant")]
    windows = slice_windows(rows)
    assert len(windows) == 1
    assert windows[0].anchor_id == 1
    assert [r.id for r in windows[0].rows] == [1, 2, 3]


def test_a_new_user_turn_starts_a_new_window():
    rows = [row(1, "user"), row(2, "assistant"), row(3, "user"), row(4, "assistant")]
    windows = slice_windows(rows)
    assert [(w.anchor_id, [r.id for r in w.rows]) for w in windows] == [
        (1, [1, 2]), (3, [3, 4])]


def test_thinking_far_from_any_human_input_is_dropped():
    """★ 两级过滤的第 2 级。实测 855 条含信号词的 thinking 只有 255 条落在 ±2 轮内
    （§5.4）——这一刀砍掉 70% 的候选，是成本的关键，不是可选优化。"""
    rows = [row(1, "user")] + [row(i, "thinking") for i in range(2, 8)]
    kept = [r.id for w in slice_windows(rows) for r in w.rows if r.role == "thinking"]
    assert kept == [2, 3]  # 距锚点 1、2 轮留下；3 轮起（id 4~7）全丢


def test_thinking_before_the_next_anchor_survives_via_that_anchor():
    """±2 是双向的：锚点**之后**两轮回看的 thinking 也要留（它往往是对下一句的预判）。"""
    rows = [row(1, "user"), row(2, "assistant"), row(3, "assistant"),
            row(4, "thinking"), row(5, "user")]
    kept = [r.id for w in slice_windows(rows) for r in w.rows if r.role == "thinking"]
    assert kept == [4]


def test_orphan_rows_before_the_first_anchor_are_dropped():
    """没有人的上下文，判了也是瞎判（§3.3「极短指令单条无意义」的另一面）。"""
    rows = [row(1, "assistant"), row(2, "thinking"), row(3, "user"), row(4, "assistant")]
    windows = slice_windows(rows)
    assert len(windows) == 1
    assert [r.id for r in windows[0].rows] == [3, 4]


def test_a_summary_anchors_its_own_window_and_does_not_swallow_what_follows():
    """§3.3：摘要单独处理。它是整段历史的浓缩，混进后续对话会让模型分不清
    哪句是刚发生的、哪句是三天前的。"""
    rows = [row(1, "summary", "This session is being continued…"),
            row(2, "user"), row(3, "assistant")]
    windows = slice_windows(rows)
    assert [(w.anchor_id, w.is_summary) for w in windows] == [(1, True), (2, False)]
    assert [r.id for r in windows[0].rows] == [1]


def test_token_budget_splits_an_oversized_window():
    rows = [row(1, "user", tokens=100)] + [row(i, "assistant", tokens=900) for i in range(2, 12)]
    windows = slice_windows(rows, token_budget=3000)
    assert len(windows) > 1
    assert all(w.tokens <= 3000 for w in windows)
    assert sum(len(w.rows) for w in windows) == len(rows)  # 一行都没丢


def test_a_single_row_over_budget_stays_whole():
    """一行是一个不可分的语义单位，从中间截断会让模型判到半句话。"""
    rows = [row(1, "user", tokens=50000)]
    windows = slice_windows(rows, token_budget=3000)
    assert len(windows) == 1 and windows[0].tokens == 50000


def test_budget_zero_means_do_not_split():
    rows = [row(1, "user", tokens=100)] + [row(i, "assistant", tokens=900) for i in range(2, 12)]
    assert len(slice_windows(rows)) == 1


def test_as_text_labels_who_said_what():
    rows = [row(1, "user", "别用 print"), row(2, "thinking", "用户要求日志走 logging"),
            row(3, "assistant", "好的")]
    text = slice_windows(rows)[0].as_text()
    assert "【用户】别用 print" in text
    assert "【AI 内心独白】用户要求日志走 logging" in text
    assert "【AI】好的" in text


def test_turn_radius_is_the_measured_knob():
    """★ 别随手改这个数：±1→137、±2→255、±3→337、±5→441（§5.4 实测）。
    放宽一档是成本，收窄一档是漏抽，两边都要用 P5 的否决数据说话。"""
    assert TURN_RADIUS == 2


@pytest.mark.parametrize("roles,expected", [
    (["user"], 1),
    (["user", "user"], 2),
    (["summary"], 1),
    ([], 0),
])
def test_window_count_equals_anchor_count(roles, expected):
    rows = [row(i + 1, role) for i, role in enumerate(roles)]
    assert len(slice_windows(rows)) == expected
