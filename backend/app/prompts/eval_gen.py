"""评测用例生成 prompt（EXPERT-REVIEW Phase 1.1）。

给 `python -m app.commands.eval --gen-candidates` 用：把库里的真记忆喂给 LLM，
生成"未来会话里用户可能自然问出的问题"作为评测 query 候选，**产出必须经人工
确认**（改/删/补）再导入 `eval_recall_cases`——评测集是后续一切调优的唯一裁判，
裁判本身不能是模型自说自话（§10 方法论：n≥3、跑不了写待测，同理"生成不等于认定"）。

与 route.py 的差别：这里没有 PROMPT_VER 驱动的游标重跑语义——生成是一次性动作，
产物落文件由人裁决，版本只影响候选质量不影响存量数据，故不建版本常量。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

SYSTEM = """你在为记忆召回系统出评测题。下面给你若干条已入库的记忆（带编号），\
请为每条记忆生成用户在未来会话里可能自然问出的问题。

## 出题规则

1. **像人说话**：问题是用户干活时随口问的，不是从记忆里抄的。
   「改签验价要先调哪个接口」好；「RetrievePnr 与 RetrieveAncillary 的调用顺序」不像人话。
2. **换说法**：至少一半的问题不得照抄记忆原文里的特征词（尤其英文标识符、编号）。
   召回靠语义向量，照抄关键词的题测不出真实水平。
3. **答案唯一指向**：问题必须能被对应的那条记忆回答，且不会合理地指向别的记忆。
   拿不准就在该条少出题，别硬凑。
4. **多样**：同一条记忆的几个问题角度要不同（问做法、问原因、问例外情况……）。
5. 用户以中文提问为主，技术词可中英混用，符合真实习惯。

按输入编号逐条返回；某条记忆实在出不了好题就返回空 queries，不要编。"""

TOOL = {
    "name": "record_eval_queries",
    "description": "按记忆编号返回生成的评测 query 候选。",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "对应输入的记忆编号"},
                        "queries": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["index", "queries"],
                },
            },
        },
        "required": ["items"],
    },
}


class MemoryQueries(BaseModel):
    index: int
    queries: list[str] = Field(default_factory=list)


class GenResult(BaseModel):
    items: list[MemoryQueries] = Field(default_factory=list)


def user_message(batch: list[dict]) -> str:
    """batch 元素：{"index": int, "content": str}。编号在整个生成过程中全局唯一。"""
    lines = [f"[{item['index']}]\n{item['content']}" for item in batch]
    return "记忆列表：\n\n" + "\n\n".join(lines)
