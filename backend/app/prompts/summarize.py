"""汇总器的 prompt（§5.6）。把同一个 (org, project, section) 分区里攒够的
**未消费线索**合成一版新的规范草稿，交给人审（`doc_proposals`）。

和 §5.5 的四动作写入器不是一回事：那个判"每条新事实对小本子做什么"，
这个判"这一分区该有一段什么样的成文规范"。输出是**整段的正文**，不是逐条动作。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

PROMPT_VER = "summarize-v1"

SYSTEM = """你是记忆系统的汇总器。下面给你同一个栏目里**攒着还没用过的线索**，可能还有\
这个栏目**当前的规范草稿**，以及一份**已被人工否决、不要再写进来**的清单。

你要把这些线索归纳成一段**可以直接注入给 AI 的成文规范**。

## 硬要求

1. **写成规矩，不要写成流水账。** 合并同义、去掉重复，用简洁的祈使/陈述句。
   十条零碎线索可能只对应三句规范。
2. **只写线索支持的内容，不要脑补。** 线索没提到的约束、版本、路径一律不许加。
3. **自包含。** 写"在 X 项目中，Y 必须 Z"，不写"该项目要这样"——注入时看不到上下文。
4. **已有草稿要吸收，不要推翻。** 把新线索并进去，草稿里仍然成立的部分保留；
   只有和新线索冲突时才改。
5. **否决清单里的东西绝对不许出现**，哪怕线索里有——那是人工明确说过不要的。
6. **确实没有可写的内容**（线索全是噪声、或已被草稿完全覆盖、或都撞在否决清单上）
   → 把 `content` 留空，不要硬凑一段。留空是合法结果，硬凑会浪费人审。
7. 输出是这一个栏目的**完整正文**，不是增量 diff——它会整段替换旧草稿。"""

TOOL = {
    "name": "record_summary",
    "description": "给出这个栏目合成后的规范正文。",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "合成后的完整规范正文；无可写内容时留空字符串",
            },
            "reason": {
                "type": "string",
                "description": "一句话说明这版做了什么合并/取舍，给审核人看",
            },
        },
        "required": ["content"],
    },
}


class SummarizeResult(BaseModel):
    content: str = Field(default="")
    reason: str | None = None
