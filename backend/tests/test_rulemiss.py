"""规矩漏判率抽样：清单解析、计分口径、渲染、覆盖保护（纯函数为主，不触库）。"""

from datetime import datetime

import pytest

from app.commands.rulemiss import (VERDICTS, main, parse_verdicts,
                                   render_sample, score)


def _rows():
    base = {"scope": "project", "org_key": "personal", "project_key": "mem-xy",
            "subject": "project", "why": None, "session_id": "sess-1",
            "msg_at": datetime(2026, 10, 28, 12, 0), "created_at": datetime(2026, 10, 28)}
    return [
        {**base, "id": 101, "role": "fact",
         "content": "feign Response 必须 try-with-resources 包裹"},
        {**base, "id": 102, "role": "preference", "why": "用户纠正过一次",
         "content": "提交信息\n不要用英文"},
    ]


SAMPLE = """# 规矩漏判率抽样清单

## #101 · fact/project · personal/mem-xy · 2026-10-28
- content: feign Response 必须 try-with-resources 包裹
- 判定: 规矩

## #102 · preference/project · personal/mem-xy · 2026-10-28
- content: 提交信息 不要用英文
- why: 用户纠正过一次
- 判定: 零碎

## #103 · fact/project · personal/mem-xy · 2026-10-29
- content: 一律用枚举
- 判定: 已成文
"""


class TestParseVerdicts:
    def test_parses_each_block(self):
        verdicts, errors = parse_verdicts(SAMPLE)
        assert verdicts == {101: "规矩", 102: "零碎", 103: "已成文"}
        assert errors == []

    def test_missing_verdict_line_defaults_pending(self):
        verdicts, errors = parse_verdicts("## #7 · x\n- content: 一律禁止\n")
        assert verdicts == {7: "待定"}
        assert errors == []

    def test_illegal_verdict_goes_to_errors(self):
        verdicts, errors = parse_verdicts("## #7 · x\n- 判定: 也许是规矩\n")
        assert verdicts == {}
        assert errors == [7]

    def test_content_colon_and_verdict_not_confused(self):
        """content 里出现「判定:」字样不得被误抓（正则锚定行首 - 判定:）。"""
        text = "## #9 · x\n- content: 这条讲「判定: 规矩」的写法\n- 判定: 待定\n"
        verdicts, _ = parse_verdicts(text)
        assert verdicts == {9: "待定"}


class TestScore:
    def test_rates(self):
        s = score({1: "规矩", 2: "规矩", 3: "已成文", 4: "零碎", 5: "零碎",
                   6: "零碎", 7: "零碎", 8: "零碎", 9: "零碎", 10: "剔除"})
        assert s["total"] == 10 and s["denominator"] == 9
        assert s["net_miss_rate"] == pytest.approx(2 / 9)
        assert s["broad_miss_rate"] == pytest.approx(3 / 9)

    def test_empty_denominator_is_none(self):
        s = score({1: "剔除"})
        assert s["net_miss_rate"] is None and s["broad_miss_rate"] is None

    def test_all_verdict_values_are_scoreable(self):
        s = score({i: v for i, v in enumerate(VERDICTS)})
        assert s["total"] == len(VERDICTS)


class TestRender:
    def test_roundtrip_through_parser(self):
        text = render_sample(_rows())
        verdicts, errors = parse_verdicts(text)
        assert errors == []
        assert verdicts == {101: "待定", 102: "待定"}

    def test_newlines_in_content_are_flattened(self):
        assert "提交信息 不要用英文" in render_sample(_rows())

    def test_why_and_source_included(self):
        text = render_sample(_rows())
        assert "用户纠正过一次" in text
        assert "session sess-1" in text

    def test_no_source_marked(self):
        rows = _rows()
        rows[0]["session_id"] = None
        assert "（无溯源）" in render_sample(rows)


class TestGenGuard:
    def test_refuses_to_overwrite_existing_file(self, tmp_path, capsys):
        out = tmp_path / "sample.md"
        out.write_text("人工判定结果", encoding="utf-8")
        assert main(["--gen", "--out", str(out)]) == 1
        assert out.read_text(encoding="utf-8") == "人工判定结果"
        assert "--force" in capsys.readouterr().err
