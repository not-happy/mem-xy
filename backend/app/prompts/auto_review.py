"""★ 双判共识制（2026-11-27，decisions-log #116）：第二遍 LLM 复判的 prompt。

背景：用户问"入池审核能否全自动、每条都通过大模型判定"。定案是**双判共识制**：
route 的第一遍判定照旧，每张待审卡再过一遍独立 LLM 复判——
- 两遍一致（observation↔observation、below_threshold 的 atomic_fact↔atomic_fact）
  → 自动落库（resolved_by='llm_auto'），人不再看；
- 分歧（observation/atomic_fact 互换、第二遍判 discard、栏位给不出）
  → 卡留 pending，第二遍意见写进 suggestion.llm_review 给人当参考；
- merge 卡（第一遍是向量近邻，不是 LLM）→ LLM 读两条原文做 verdict：
  merge=确认同一件事（自动合并）、keep_both=确是两件事（自动关卡）。

词表与判据必须与第一遍（prompts/route.py）同源——复判不是另立标准，
而是"第二双眼睛用同一套标准独立看一遍"。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from .route import KINDS, ROLES, SCOPES, SUBJECTS

PROMPT_VER = "auto-review-v1"

SYSTEM = """\
你是个人记忆系统的第二遍审核员。第一遍（路由模型）已经把一段对话判过类，
现在由你**独立**复核这张卡，两遍一致的内容才自动入库，你与第一遍不一致的
会退回人工。所以：不要试图猜第一遍怎么判，按你自己的判断来，但用同一套标准。

【判类标准（与第一遍完全相同）】
第一问：这条信息是否需要**每次开工都无条件提醒**？（行为规矩、产品取向、
明确的"要/不要"指令）→ 是，记 observation（观察池，后续汇总进成文规范）。
环境类条目历史上判错最多——"每次开工都要提醒的环境事实"也算规矩。
第二问：不需要每次提醒、但**值得长期记住**？→ atomic_fact（锚点/偏好/事实）。
第三问：都可推导、是一次性执行细节、或纯指令？→ discard（不留）。

【输出要求】
- kind 取 observation / atomic_fact / discard。
- atomic_fact 必须给 scope/subject/role（受控词表，见 tool schema）。
- content 是自包含的一句话（不用代词）；与卡上原文分歧时可给修订建议。
- confidence 是你对**判类**的把握（不是对内容真伪）。
- why 一句话说明依据，特别是不一致时要让人一眼看懂差在哪。
"""

MERGE_SYSTEM = """\
你是个人记忆系统的合并复核员。系统用向量相似度（余弦 ≥0.90）挑出两条可能
在说同一件事的记忆，但余弦分不开"重复"和"互补的超集"——所以由你读两条
原文做最终裁决：

- verdict=merge：两条确实是**同一件事**的重复表述（或一条是另一条的子集），
  可以合并成一条。content 字段给出合并后的自包含表述（建议保留信息更全的
  一条的原文，仅去掉重复部分）。
- verdict=keep_both：两条虽然字面相似，但记录的是**两件不同的事**
  （不同对象、不同结论、互补的独立事实）→ 不合并。
- 不确定时选 keep_both——错误合并会丢失一条真实记忆，分开放着最多多占一行。
"""

TOOL = {
    "name": "record_review",
    "description": "记录对这张待审卡的独立复核结论。",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "content": {"type": "string", "description": "修订建议（与原判一致时可不填）"},
            "scope": {"type": "string", "enum": list(SCOPES)},
            "subject": {"type": "string", "enum": list(SUBJECTS)},
            "role": {"type": "string", "enum": list(ROLES)},
            "section_key": {"type": "string", "description": "栏位建议（卡上缺栏位时给）"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "why": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["kind", "confidence"],
    },
}

MERGE_TOOL = {
    "name": "record_merge_review",
    "description": "记录对这对近邻记忆的合并裁决。",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["merge", "keep_both"]},
            "content": {"type": "string", "description": "verdict=merge 时给合并后的表述"},
            "why": {"type": "string"},
        },
        "required": ["verdict"],
    },
}


class JudgeItem(BaseModel):
    """第二遍对 route 卡的复判结果。校验规则与 RouteItem 同源但更宽松：
    这里只要求词表合法；content/scope 等缺失交给共识判定处理（不一致本来就留人）。"""

    kind: str
    confidence: float = Field(ge=0, le=1)
    content: str | None = None
    scope: str | None = None
    subject: str | None = None
    role: str | None = None
    section_key: str | None = None
    keywords: list[str] = Field(default_factory=list)
    why: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "JudgeItem":
        if self.kind not in KINDS:
            raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {self.kind!r}")
        return self


class JudgeResult(BaseModel):
    kind: JudgeItem


class MergeJudge(BaseModel):
    verdict: str
    content: str | None = None
    why: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "MergeJudge":
        if self.verdict not in ("merge", "keep_both"):
            raise ValueError(f"verdict 必须是 merge/keep_both，收到 {self.verdict!r}")
        return self


def system_prompt() -> str:
    return SYSTEM


def merge_system_prompt() -> str:
    return MERGE_SYSTEM
