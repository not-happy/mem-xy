"""§3 过滤规则。每个测试都对应一次实测发现（见 decisions-log C.6）。"""

from app.ingest import filters


def user(content, **kw):
    return {"type": "user", "message": {"content": content}, **kw}


def assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def text(t):
    return {"type": "text", "text": t}


def thinking(t):
    return {"type": "thinking", "thinking": t}


def tool_use():
    return {"type": "tool_use", "id": "x", "name": "Read", "input": {}}


def tool_result():
    return {"type": "tool_result", "tool_use_id": "x", "content": "ok"}


# ── user 行的两种形态 ──


def test_str_content_is_accepted():
    assert filters.classify_user(user("改成 snake_case")) == filters.Parsed(
        role="user", content="改成 snake_case")


def test_list_of_text_blocks_is_also_real_human_input():
    """★ C.6 翻车一：旧规则 `isinstance(c, str)` 把这类行全丢了，
    实测 266 条真人输入里有 161 条（61%）走的是这条路。"""
    record = user([{"type": "text", "text": "跑完了，你看下"}])
    assert filters.classify_user(record) == filters.Parsed(role="user", content="跑完了，你看下")


def test_multiple_text_blocks_are_joined():
    record = user([{"type": "text", "text": "先做 A"}, {"type": "text", "text": "再做 B"}])
    assert filters.classify_user(record).content == "先做 A 再做 B"


def test_list_containing_tool_result_is_dropped():
    """tool_result 是 `list[tool_result]`，和 `list[text]` 完全是两回事。"""
    assert filters.classify_user(user([tool_result()])) is None


def test_mixed_block_list_is_dropped():
    """只认纯文本块；混了别的类型就说明不是人直接打的字。"""
    assert filters.classify_user(user([{"type": "text", "text": "hi"}, tool_result()])) is None


def test_is_meta_is_dropped():
    assert filters.classify_user(user("系统元数据", isMeta=True)) is None


def test_non_user_types_never_become_user():
    assert filters.classify_user({"type": "attachment", "message": {"content": "x"}}) is None


# ── 注入标记 ──


def test_every_injected_prefix_is_dropped():
    for prefix in filters.INJECTED_PREFIXES:
        assert filters.classify_user(user(prefix + " 后面的内容")) is None, prefix


def test_leading_whitespace_does_not_smuggle_an_injected_prefix():
    assert filters.classify_user(user("   <task-notification>xx")) is None


def test_a_pasted_log_starting_with_a_bracket_is_kept():
    """★ C.6 翻车二：粗暴的"以 [ 开头就丢"会杀掉这条真素材。
    `[Request interrupted by user]` 是固定系统标记（丢），`[INFO` 是人粘的日志（留）。"""
    line = "[INFO ] 2026-10-22 18:55:10.223 [2225 ] cloudMonitor/cloud_channel.cpp:498: send metric"
    assert filters.classify_user(user(line)).role == "user"


def test_an_injected_marker_in_the_middle_does_not_drop_the_turn():
    """只挡开头。人把 `<system-reminder>` 抄进消息里质问时，那是真输入。"""
    assert filters.classify_user(user("为什么会有 <system-reminder> 这个东西")).role == "user"


def test_whitespace_only_turn_is_dropped():
    assert filters.classify_user(user("   \n ")) is None


# ── 压缩摘要分流 ──


def test_compaction_summary_becomes_its_own_role():
    record = user(filters.SUMMARY_PREFIX + " a previous conversation…")
    assert filters.classify_user(record).role == "summary"


# ── assistant 行 ──


def test_tool_use_only_is_dropped():
    assert filters.parse_record(assistant(tool_use())) is None


def test_assistant_text_is_kept():
    assert filters.parse_record(assistant(text("结论：改成 snake_case"))) == filters.Parsed(
        role="assistant", content="结论：改成 snake_case")


def test_thinking_without_signal_words_is_dropped():
    """§3.2 第一级：5060 → 862 全靠这一条。"""
    assert filters.parse_record(assistant(thinking("Let me read the file first."))) is None


def test_thinking_with_a_signal_word_is_kept():
    parsed = filters.parse_record(assistant(thinking("用户说不要用 Lombok，那就不引")))
    assert parsed.role == "thinking"


def test_thinking_wins_over_text_in_the_same_record():
    """★ EXPERT-REVIEW A2 后行为变化：混块时 role 仍是 thinking（它有信号词），
    但 text 不再被静默丢掉——拼接进同一行（一行约束 UNIQUE(source, src_uuid) 下无损）。"""
    parsed = filters.parse_record(assistant(thinking("必须大写"), text("好的")))
    assert parsed.role == "thinking"
    assert "必须大写" in parsed.content and "好的" in parsed.content


def test_mixed_thinking_text_keeps_the_text_when_thinking_has_no_signal():
    """thinking 本就不入库（无信号词），但 text 单独出现时是入库的——保住它。"""
    parsed = filters.parse_record(
        assistant(thinking("Let me read the file first."), text("改完了，你看下")))
    assert parsed == filters.Parsed(role="assistant", content="改完了，你看下")


def test_mixed_thinking_text_warns_so_the_broken_assumption_stays_visible(caplog):
    """WARN 是哨兵：实测 assistant 从不混块（tool_use/thinking/text 各自独立），
    这条日志计数非零 = 外部假设破了，要回来重审 §3 的过滤规则。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.ingest.filters"):
        filters.parse_record(assistant(thinking("必须大写"), text("好的")))
    assert "混块" in caplog.text


def test_empty_assistant_text_is_dropped():
    assert filters.parse_record(assistant(text("  "))) is None


def test_non_message_records_are_dropped():
    assert filters.parse_record({"type": "attachment"}) is None
    assert filters.parse_record({"type": "user"}) is None


# ── 信号词 ──


def test_signal_pattern_covers_both_languages():
    for word in ("不要", "改为", "必须", "否决", "踩坑", "don't", "prefer", "convention"):
        assert filters.has_signal(word), word
    assert not filters.has_signal("读取文件并返回内容")
