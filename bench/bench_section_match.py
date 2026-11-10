"""§5.3 栏位匹配的校准台：12 条真实素材 + 人工认定的正确栏位。

改词表（`seed.py` 的 `SECTIONS` aliases）之后必须重跑：
    python -m app.core.seed        # 重算 match_embedding
    python bench/bench_section_match.py --repeat 3

`--repeat 3` 顺带跑掉 §13 P2 的验收项"同一条 observation 连跑 3 次，section_key 三次一致"。

★ 2026-10-23 首跑的结论（别丢了，这是阈值的唯一依据）：
  * `section_match_min_score=0.35` **一次都没触发**——top-1 分数最低也有 0.527。
    bge-m3 在这套词表上根本给不出低分，真正起作用的是 margin。
  * `section_match_min_margin=0.02` 拦下 2/12（17%），**而且两条的 top-1 其实都判对了**
    （contact_ops 0.579 vs external_communication 0.567；enum_const 0.589 vs field_format 0.586）。
    也就是说这个阈值是在拿"多送 17% 进人工"换"绝不飘"。
    #50 的教训是飘一次就毁掉 §4.3.2 的两次比对，所以这笔交易划算，但要知道代价。
  * 想少送人工，正确的做法不是调低 margin，而是**把打架的栏位词表拉开**
    （contact_ops vs external_communication、enum_const vs field_format/naming_docs）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "backend"))

from app.core import db  # noqa: E402
from app.core.embedding import get_embedding_service  # noqa: E402
from app.pipeline import sections  # noqa: E402

# (素材, 人工认定的正确栏位)
CASES: list[tuple[str, str]] = [
    ("在 gds-agg 项目中禁止引入 Lombok，一律写显式 getter/setter", "dependency"),
    ("本机 JDK 版本是 17，Maven 安装在 D:/tools/maven", "local_env"),
    ("回答风格要求：永远用中文，先给结论再展开", "answer_style"),
    ("当前系统业务层面不支持婴儿票", "business_boundary"),
    ("改签必须先调 RetrievePnr 校验行程，再调 executeChange", "flow_gotcha"),
    ("联系人邮箱是 support@travelport.com，测试环境账号尚未开通", "contact_ops"),
    ("日志不要全量读，只读相关段", "ai_working_rules"),
    ("枚举名必须全大写，禁止魔法值", "enum_const"),
    ("提交代码前先从 dev 拉新分支，不要直接推 dev", "git_workflow"),
    ("mvn 打包要用 -pl -am，离线加 -o", "build_env"),
    ("手机号字段要去掉 86- 前缀再传给供应商", "field_format"),
    ("异常不要吞掉，也不要将内部异常直接返回给调用方", "error_handling"),
]


def main() -> int:
    repeat = _repeat_arg()
    db.init_pool(min_size=1, max_size=2)
    try:
        get_embedding_service().load()
        pool = sections.load_sections(has_project=True)
        print(f"候选栏位 {len(pool)} 个\n")
        hit = auto = review = 0
        runs: list[list[str]] = [[] for _ in CASES]
        for index, (content, expected) in enumerate(CASES):
            vector = get_embedding_service().encode_one(content)
            scored = sorted(
                ((sum(a * b for a, b in zip(vector, s.embedding)), s) for s in pool),
                key=lambda pair: pair[0], reverse=True)
            top_score, top = scored[0]
            second_score, second = scored[1]
            matched = sections.match(content, pool, embedding=vector)
            if matched is None:
                review += 1
                mark = "→人工"
                got = f"{top.section_key}(被 margin 拦下)"
            else:
                auto += 1
                got = matched.section_key
                hit += got == expected
                mark = "✓" if got == expected else f"✗ 应为 {expected}"
            print(f"  {mark:<22} {got:<26} top1={top_score:.3f} "
                  f"margin={top_score - second_score:.3f} 2nd={second.section_key:<20} "
                  f"{content[:26]}")
            # ★ 稳定性比的是**归一化后的结果**：`got` 带了给人看的注解，
            #   直接拿它比会把"同样送人工"误报成飘移（首跑就踩过这个坑）。
            runs[index].append(matched.section_key if matched else "(→人工)")
            for _ in range(repeat - 1):
                # ★ 重跑要**重新 encode**：稳定性要验的是"同一条素材反复过一遍匹配器"，
                #   把向量缓存下来只验了排序的确定性，验不出编码器本身的抖动。
                again = sections.match(content, pool,
                                       embedding=get_embedding_service().encode_one(content))
                runs[index].append(again.section_key if again else "(→人工)")
        print(f"\n自动判 {auto} 条（其中判对 {hit}，准确率 {hit / max(auto, 1):.0%}），"
              f"送人工 {review} 条（{review / len(CASES):.0%}）")
        print("★ 送人工比例涨了 → 先去看是哪两个栏位在打架，别急着调低 margin。")
        return _report_stability(runs, repeat)
    finally:
        db.close_pool()


def _repeat_arg() -> int:
    """`--repeat 3` 就是 §13 P2 的验收项"同一条 observation 连跑 3 次，section_key 三次一致"。"""
    if "--repeat" in sys.argv:
        return int(sys.argv[sys.argv.index("--repeat") + 1])
    return 1


def _report_stability(runs: list[list[str]], repeat: int) -> int:
    if repeat < 2:
        print("\n（没验稳定性：加 `--repeat 3` 才是 §13 P2 的验收跑法）")
        return 0
    drifted = [(CASES[i][0], got) for i, got in enumerate(runs) if len(set(got)) > 1]
    print(f"\n── 栏位稳定性（每条连跑 {repeat} 次）──")
    for content, got in drifted:
        print(f"  ✗ 飘了：{got}  ← {content[:30]}")
    if not drifted:
        print(f"  ✓ {len(runs)} 条素材 × {repeat} 次，section_key 全部一致")
        print("  ★ #50 要治的就是这个：LLM 自选栏位时实测同一条会飘 2–3 个栏位，"
              "飘一次就毁掉 §4.3.2 的两次比对。向量匹配 + 固定词表 ⇒ 确定性。")
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
