"""四动作写入器的 prompt（§5.5）。判"整桶"而不是"每条"，所以一次调用要同时看
新事实和已有记忆，输出的是**每条新事实对已有记忆做什么**。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

PROMPT_VER = "write_facts-v1"

ACTIONS = ("ADD", "UPDATE", "DELETE", "NOOP")

SYSTEM = """你是记忆系统的写入器。下面给你一批**新事实**，以及同一个分区里**已有的记忆**\
（按相关度排过序；已有记忆的 subject/role 标签可能与新事实不同——分类判错很常见，
判"是不是同一件事"只看语义，不看标签）。你要逐条判断每个新事实该怎么落库。

## 四个动作

- `ADD`     已有记忆里没有任何一条覆盖它 → 新增一条
- `NOOP`    已有记忆 `memory_id` 说的就是同一件事，且没有新信息 → 不新增，只累加它的提及次数
- `UPDATE`  已有记忆 `memory_id` 说的是同一件事，但新事实**补充或纠正**了它
            → 用 `merged_content` 覆盖旧内容（必须是合并后的完整表述）
- `DELETE`  已有记忆 `memory_id` 与新事实**矛盾** → 软删除旧的，同时把新事实 ADD 进去

## 硬要求

1. **判"同一件事"看语义，不看字面。**
   「gds-agg 禁止引入 Lombok」和「gds-agg 里不要用 Lombok，一律写显式 getter」
   是同一件事（NOOP 或 UPDATE），不是两条记忆。
2. **只有确实矛盾才用 DELETE。** 补充细节用 UPDATE。删掉的东西只能从 `memory_events`
   里翻，代价远高于多留一条。
3. **`merged_content` 不能丢旧内容里仍然成立的部分**，也必须自包含
   （写"在 X 项目中，Y 使用 Z"，不写"该项目使用 Z"）。
4. **拿不准就 ADD。** 多一条重复记忆，下一批还有机会被合并；漏一条就永远没了。
5. 已有记忆列表可能是空的（冷启动），那就全部 ADD。
6. 每个新事实的 `index` 都必须给出判定，一个都不能漏。"""

TOOL = {
    "name": "record_write_decisions",
    "description": "逐条给出每个新事实的落库动作。",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0,
                                  "description": "对应【新事实】里的编号"},
                        "action": {"type": "string", "enum": list(ACTIONS)},
                        "memory_id": {"type": "integer",
                                      "description": "UPDATE/DELETE/NOOP 时必填，指已有记忆"},
                        "merged_content": {"type": "string",
                                           "description": "UPDATE 时必填，合并后的完整表述"},
                        "mention_delta": {"type": "integer", "minimum": 1, "default": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "action"],
                },
            }
        },
        "required": ["decisions"],
    },
}


class WriteDecision(BaseModel):
    index: int = Field(ge=0)
    action: str
    memory_id: int | None = None
    merged_content: str | None = None
    mention_delta: int = Field(default=1, ge=1)
    reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "WriteDecision":
        if self.action not in ACTIONS:
            raise ValueError(f"action 必须是 {ACTIONS} 之一，收到 {self.action!r}")
        if self.action == "ADD":
            return self
        if self.memory_id is None:
            raise ValueError(f"{self.action} 必须给 memory_id")
        if self.action == "UPDATE" and not (self.merged_content or "").strip():
            raise ValueError("UPDATE 必须给 merged_content")
        return self


class WriteResult(BaseModel):
    decisions: list[WriteDecision] = Field(default_factory=list)
