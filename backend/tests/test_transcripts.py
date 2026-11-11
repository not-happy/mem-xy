"""§3.5 transcript 发现与解析。"""

import json

from app.ingest.transcripts import (discover, estimate_tokens, iter_records,
                                    parse_ts)


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
                    encoding="utf-8")


def test_discover_finds_subagent_files_four_levels_down(tmp_path):
    """★ 只扫一层 glob 会漏掉全部 43 个子 agent 文件（C.6）。"""
    _write(tmp_path / "C--x-own-mem-xy" / "sess-1.jsonl", [{"type": "user"}])
    _write(tmp_path / "C--x-own-mem-xy" / "sess-1" / "subagents" / "agent-a.jsonl",
           [{"type": "user"}])

    found = {t.path.replace("\\", "/").rsplit("/", 1)[-1]: t for t in discover(tmp_path)}
    assert found["sess-1.jsonl"].is_subagent is False
    assert found["agent-a.jsonl"].is_subagent is True
    assert found["agent-a.jsonl"].project_dir == "C--x-own-mem-xy"


def test_discover_ignores_other_extensions(tmp_path):
    _write(tmp_path / "p" / "a.jsonl", [{"type": "user"}])
    (tmp_path / "p" / "notes.md").write_text("x", encoding="utf-8")
    assert len(discover(tmp_path)) == 1


def test_discover_marks_forked_copies(tmp_path):
    """★ decisions-log #110：fork 影子副本首行带 forkedFrom、uuid 全部被重写，
    `UNIQUE(source, src_uuid)` 幂等挡不住它，必须由 discover 标记出来给 scan 排除。
    首行读不出来（空文件/坏 JSON）按"不是 fork"处理——宁可入库重复，不可漏扫真会话。"""
    _write(tmp_path / "p" / "sess-1.jsonl", [{"type": "user"}])
    _write(tmp_path / "p" / "sess-2.jsonl",
           [{"type": "attachment", "forkedFrom": {"sessionId": "sess-1", "messageUuid": "u1"}},
            {"type": "user"}])
    (tmp_path / "p" / "empty.jsonl").write_text("", encoding="utf-8")

    found = {t.path.replace("\\", "/").rsplit("/", 1)[-1]: t for t in discover(tmp_path)}
    assert found["sess-1.jsonl"].is_fork is False
    assert found["sess-2.jsonl"].is_fork is True
    assert found["empty.jsonl"].is_fork is False


def test_discover_on_an_empty_root(tmp_path):
    assert discover(tmp_path) == []


def test_iter_records_stops_at_an_unfinished_tail(tmp_path):
    """★ 语料是活的：活跃会话的文件正在被追加，末尾半行解析必然失败，
    必须跳过而不是让整个扫描报错（C.6）。"""
    path = tmp_path / "a.jsonl"
    path.write_text(
        '{"type":"user","uuid":"1"}\n{"type":"user","uuid":"2"}\n{"type":"user","uu',
        encoding="utf-8",
    )
    assert [r["uuid"] for r in iter_records(path)] == ["1", "2"]


def test_iter_records_takes_a_complete_tail_without_a_newline(tmp_path):
    """★ EXPERT-REVIEW A2：尾行不以 `\\n` 结束 ≠ 半行——写入端可能就是没补结尾
    换行，直接 return 会漏采这条完整记录，直到下次追加才入库。"""
    path = tmp_path / "a.jsonl"
    path.write_text('{"uuid":"1"}\n{"uuid":"2"}', encoding="utf-8")
    assert [r["uuid"] for r in iter_records(path)] == ["1", "2"]


def test_iter_records_ignores_a_whitespace_only_tail(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text('{"uuid":"1"}\n   ', encoding="utf-8")
    assert [r["uuid"] for r in iter_records(path)] == ["1"]


def test_iter_records_skips_blank_and_malformed_lines(tmp_path):
    path = tmp_path / "a.jsonl"
    path.write_text('{"uuid":"1"}\n\nnot json at all\n[1,2]\n{"uuid":"2"}\n', encoding="utf-8")
    assert [r["uuid"] for r in iter_records(path)] == ["1", "2"]


def test_parse_ts_reads_the_iso_timestamp_every_record_carries():
    ts = parse_ts({"timestamp": "2026-10-08T03:30:11.939Z"})
    assert (ts.year, ts.month, ts.day, ts.hour) == (2026, 10, 8, 3)
    assert parse_ts({}) is None
    assert parse_ts({"timestamp": "昨天"}) is None


def test_estimate_tokens_charges_cjk_per_character():
    assert estimate_tokens("中文字符") == 4
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("中文abcd") == 2 + 1
    assert estimate_tokens("") == 0
