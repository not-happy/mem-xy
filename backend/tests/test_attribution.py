"""§5.1 归属判定：纯规则。这些测试不连数据库（`match_rules` 是纯函数）。"""

import pytest

from app.ingest.attribution import (Attribution, compile_rules, match_rules,
                                    normalize_cwd, project_key_for, remote_host,
                                    remote_path, repo_name)

DIDA_REMOTE = ("https://codeup.aliyun.com/62bc118a3e81781f3ad1d681/Flight/gds/agg/"
               "gds-agg-processor-tiket.git")

RULES = compile_rules([
    {"id": 1, "pattern_type": "git_remote_path", "pattern": "62bc118a3e81781f3ad1d681/%",
     "org_key": "dida", "project_key": None, "priority": 10},
    {"id": 2, "pattern_type": "cwd_prefix", "pattern": "C:/Users/yong.xu/IdeaProjects/dida/",
     "org_key": "dida", "project_key": None, "priority": 50},
    {"id": 3, "pattern_type": "cwd_prefix", "pattern": "C:/Users/yong.xu/own/",
     "org_key": "personal", "project_key": None, "priority": 80},
])


# ── cwd 归一化 ──


@pytest.mark.parametrize("raw,want", [
    (r"c:\Users\yong.xu\own\mem-xy", "C:/Users/yong.xu/own/mem-xy"),
    (r"C:\Users\yong.xu\own\mem-xy", "C:/Users/yong.xu/own/mem-xy"),
    ("C:/Users/yong.xu/own/mem-xy", "C:/Users/yong.xu/own/mem-xy"),
    ("C:/Users/yong.xu/own/mem-xy/", "C:/Users/yong.xu/own/mem-xy"),
    ("", ""),
    (None, ""),
])
def test_normalize_cwd(raw, want):
    """★ 实测 44 种写法归一化后只有 36 个真实目录，差值全是盘符大小写。"""
    assert normalize_cwd(raw) == want


def test_normalize_cwd_collapses_the_two_drive_spellings():
    assert normalize_cwd(r"d:\ai\mog") == normalize_cwd("D:/ai/mog")


# ── 远端地址解析 ──


def test_remote_path_strips_scheme_and_host():
    """★ 必须去 host：codeup.aliyun.com 是公共托管，只匹配 host 会误判他司仓库。"""
    assert remote_path(DIDA_REMOTE) == "62bc118a3e81781f3ad1d681/Flight/gds/agg/gds-agg-processor-tiket.git"


def test_remote_path_handles_scp_style():
    assert remote_path("git@codeup.aliyun.com:62bc118a3e81781f3ad1d681/Flight/x.git") == \
        "62bc118a3e81781f3ad1d681/Flight/x.git"


def test_remote_host():
    assert remote_host(DIDA_REMOTE) == "codeup.aliyun.com"
    assert remote_host("git@codeup.aliyun.com:a/b.git") == "codeup.aliyun.com"
    assert remote_host("") == ""


def test_repo_name_prefers_the_remote():
    assert repo_name("C:/somewhere/else", DIDA_REMOTE) == "gds-agg-processor-tiket"


def test_repo_name_falls_back_to_the_cwd_tail():
    assert repo_name("C:/Users/yong.xu/own/mem-xy", None) == "mem-xy"


def test_project_key_is_forced_into_org_slash_repo():
    """`project_key` 上有 CHECK `^[^/]+/[^/]+$`，多段的仓库路径必须只取末段。"""
    assert project_key_for("dida", "C:/x", DIDA_REMOTE) == "dida/gds-agg-processor-tiket"


def test_project_key_takes_only_the_last_path_segment():
    """远端路径可以很深（`…/Flight/gds/agg/x.git`），但 project_key 只允许 `org/repo` 两段。"""
    assert project_key_for("personal", "C:/x", "https://h/a/b/we/ird.git") == "personal/ird"


def test_project_key_is_none_when_nothing_to_derive():
    assert project_key_for("personal", "/", None) is None


# ── 规则匹配 ──


def test_git_remote_wins_over_cwd_prefix():
    """★ `~/own/` 是混合目录：真 dida 仓库要先被 remote 规则命中，
    所以 git_remote_path 的 priority(10) 必须高于 cwd_prefix(80)。"""
    got = match_rules(RULES, "C:/Users/yong.xu/own/gds-agg-processor-tiket", DIDA_REMOTE)
    assert got.org_key == "dida"
    assert got.match_rule_id == 1
    assert got.resolved_by == "rule"


def test_a_fork_keeps_the_upstream_remote_and_is_still_dida():
    """§5.1 记的边界情形：这类误判靠 workspace_map.resolved_by='human' 人工覆盖。"""
    got = match_rules(RULES, "D:/ai/forks/tiket", DIDA_REMOTE)
    assert got.org_key == "dida"


def test_cwd_prefix_matches_a_dida_repo_without_a_remote():
    got = match_rules(RULES, "C:/Users/yong.xu/IdeaProjects/dida/gds-sabre", None)
    assert (got.org_key, got.project_key) == ("dida", "dida/gds-sabre")


def test_cwd_prefix_matches_personal():
    got = match_rules(RULES, "C:/Users/yong.xu/own/mem-xy", None)
    assert (got.org_key, got.project_key, got.match_rule_id) == ("personal", "personal/mem-xy", 3)


def test_no_rule_matches_returns_none_so_the_caller_can_default():
    assert match_rules(RULES, "C:/tmp/smoke/starforge", None) is None


def test_another_companys_repo_on_the_same_host_does_not_match():
    """★ 这条是 `git_remote_path` 存在的理由。"""
    other = "https://codeup.aliyun.com/ffffffffffffffff/Flight/gds/agg/gds-agg.git"
    assert match_rules(RULES, "C:/tmp/other", other) is None


def test_matching_follows_list_order_so_the_caller_must_sort_by_priority():
    """`match_rules` 不排序（排序是 SQL 的事：`ORDER BY priority, id`）。
    这条测试把那个契约钉住：列表顺序若和 priority 不一致，结果就跟着列表走。"""
    unsorted = compile_rules([
        {"id": 3, "pattern_type": "cwd_prefix", "pattern": "C:/Users/yong.xu/own/",
         "org_key": "personal", "project_key": None, "priority": 80},
        {"id": 1, "pattern_type": "git_remote_path", "pattern": "62bc118a3e81781f3ad1d681/%",
         "org_key": "dida", "project_key": None, "priority": 10},
    ])
    got = match_rules(unsorted, "C:/Users/yong.xu/own/x", DIDA_REMOTE)
    assert got.org_key == "personal"
    assert match_rules(RULES, "C:/Users/yong.xu/own/x", DIDA_REMOTE).org_key == "dida"


def test_an_explicit_project_key_on_the_rule_is_not_overridden():
    rules = compile_rules([
        {"id": 9, "pattern_type": "cwd_prefix", "pattern": "C:/special/",
         "org_key": "dida", "project_key": "dida/pinned", "priority": 10},
    ])
    assert match_rules(rules, "C:/special/whatever", None).project_key == "dida/pinned"


def test_attribution_is_frozen():
    with pytest.raises(Exception):
        Attribution("a", "b", "rule").org_key = "c"
