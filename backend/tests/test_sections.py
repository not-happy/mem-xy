"""§5.3 栏位匹配：用手工向量测判定逻辑，不加载模型、不碰数据库。"""

from app.pipeline.sections import Section, match


def sec(key, vec, org="dida", applies_to="org"):
    return Section(section_key=key, label=key, applies_to=applies_to, org_key=org,
                   sort=100, embedding=vec)


POOL = [sec("code_style", [1.0, 0.0, 0.0]), sec("local_env", [0.0, 1.0, 0.0]),
        sec("git_workflow", [0.0, 0.0, 1.0])]


def test_picks_the_closest_section():
    got = match("缩进用 4 空格", POOL, embedding=[0.99, 0.14, 0.0],
                min_score=0.35, min_margin=0.02)
    assert got.section_key == "code_style"
    assert got.runner_up == "local_env"


def test_returns_none_when_nothing_is_close_enough():
    """分数低于阈值 ⇒ 调用方必须把它送进 `pending_items(stage='section')`。"""
    assert match("完全不相干的东西", POOL, embedding=[0.2, 0.2, 0.2],
                 min_score=0.35, min_margin=0.02) is None


def test_returns_none_when_the_top_two_are_indistinguishable():
    """两个栏位一样像 = 词表分不开，人来定。

    ★ 这条阈值存在的理由：#50 里模型三次跑把 JDK17 判到 local_env/build_env/build_env，
    栏位一飘，§4.3.2 的两次向量比对就在错误分区里做了。宁可交人工也不要飘。"""
    got = match("JDK 17 还是 Maven", POOL, embedding=[0.7071, 0.7071, 0.0],
                min_score=0.35, min_margin=0.02)
    assert got is None


def test_margin_threshold_is_configurable():
    vector = [0.7071, 0.7071, 0.0]
    assert match("x", POOL, embedding=vector, min_score=0.35, min_margin=0.0).section_key \
        in ("code_style", "local_env")


def test_empty_pool_never_matches():
    assert match("任何东西", [], embedding=[1.0, 0.0, 0.0]) is None


def test_carries_the_sections_org_so_the_caller_can_spot_cross_org_matches():
    """会话归属 dida 却匹配到 personal 的栏位（本机环境的事）是合法的，
    但要能被统计出来——那是 §14 待审词表最直接的观测信号。"""
    pool = POOL + [sec("answer_style", [0.0, 0.9, 0.44], org="personal")]
    got = match("回答用中文", pool, embedding=[0.0, 0.95, 0.3],
                min_score=0.35, min_margin=0.02)
    assert got.section_key == "answer_style"
    assert got.org_key == "personal"
