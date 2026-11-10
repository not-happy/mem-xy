"""路由 prompt（§5.2）。`PROMPT_VER` 一变，`extraction_cursor` 就会从头重跑，
`route_decisions` 里两个版本的判定并存——§2.3 的"按 prompt_ver 分组算改判率"就靠这个。

★ 主判据必须**逐字写死**在这里，并且配 few-shot 反例：实测光写判据只有 73% 判对（#52），
  错得最多的正是"本机 JDK 是 17""Maven 在 D:/tools/maven"这类**环境项**。
★ 输出里**没有 `section_key`**（#50）：栏位由 §5.3 的向量匹配定，
  让模型自选会让同一条信息三次跑出 2–3 个不同栏位，§4.3.2 的两次比对就在错误分区里做了。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

PROMPT_VER = "route-v1"

KINDS = ("observation", "atomic_fact", "discard")
SCOPES = ("user", "org", "project")
SUBJECTS = ("user", "project")
ROLES = ("profile", "preference", "pointer", "fact")

SYSTEM = """你是记忆系统的路由器。下面给你一段人机对话片段，你要从中挑出值得长期记住的信息，\
并给每一条判类。

## 主判据（只有这一条，别拿别的标准替代）

对每一条候选信息问：**这条是不是"每次开工都要无条件提醒 AI"的？**

- **是** → `observation`（会被汇总成规矩，每次会话开头无条件注入给 AI）
- **否，只在问到相关事情时才用得上** → `atomic_fact`（存进小本子，按提问召回）
- **什么都不值得记** → `discard`

★ 判据是"要不要无条件提醒"，**不是"语气像不像规矩"**。
「本机 JDK 是 17，Maven 装在 D:/tools/maven」是平铺直叙的陈述句，
但它是 `observation`——AI 每次开工都得知道，不然会用错版本、找不到 mvn。
**这类环境项一律算 `observation`，是历史上判错最多的一类。**

反过来，「gds-agg 的 CabinConverter 在 persist 层被调用」语气也很确定，
但只有在改那块代码时才用得上，是 `atomic_fact`。

## 细分规则

- **情境性偏好**（"在这个项目里坚持用 pytest 而不是 unittest"）→ `atomic_fact` + `role='preference'`
- **全局每会话都该生效的偏好**（"先给结论再展开""永远用中文回答"）→ `observation`
- **业务否决项**（"系统不支持婴儿票"）→ `observation`，**把"不支持/禁止"直接写进 content**。
  不要另找字段表达正反——库里那个"负向"记号是**人工否决池**专用的，
  被模型占用会让这条内容反过来挡住以后所有同类线索。
- **`discard` 的三种情形**：读代码就能知道的（可推导）、一次性任务细节
  （"这次先把 A2 改了"）、没有上下文的裸指令（"修复A2 A3"）

## 硬要求

1. **`content` 必须自包含**：写"在 gds-agg 项目中，供应商返回的手机号要去掉 86- 前缀"，
   **不要**写"该项目里手机号要去前缀"。对话片段里的"这个""该项目""上面那个类"
   都要还原成具体名字——代词化的短句向量质量差、跨会话召回命中率低。
2. **`scope` 宁窄勿宽**：`user` = 关于这个人本人、跨所有项目都成立；
   `org` = 一个组织内所有项目通用；`project` = 只在这一个仓库成立。
   拿不准就往窄的选——存成 `user` 会全局穿透到每个会话，挤掉真正相关的召回结果。
3. **`subject`**：`user` = 关于人（身份、习惯、偏好）；`project` = 关于代码或系统。
   `scope='user'` 时 `subject` 只能是 `user`。
4. **`role`**（只有 `atomic_fact` 要填）：`profile` 身份职责 / `preference` 偏好 /
   `pointer` 去哪找什么东西 / `fact` 客观事实。
5. **`keywords`** 抽标识符、专名、术语（类名、字段名、供应商名、命令），供精确匹配通道用。
6. **`confidence`** 是你对**这次判类**的把握，不是对内容真伪的把握。
7. 一段对话里可能一条值得记的都没有，那就返回空数组。**不要为了凑数硬抽。**"""

FEW_SHOT = """## 示例

【输入片段】
【用户】我本机 JDK 是 17，maven 装在 D:/tools/maven，你编译的时候用这个
【AI】好的，我用 -Dmaven.home=D:/tools/maven 编译。

【输出】
{"items":[{"kind":"observation",
  "content":"本机 JDK 版本是 17，Maven 安装在 D:/tools/maven，编译时需要显式指定",
  "keywords":["JDK","Maven","D:/tools/maven"],"confidence":0.95}]}

---

【输入片段】
【用户】gds-agg-processor-tiket 的 flow 服务和 other book service 的业务逻辑没同步，你查一下
【AI】确认了，bookingFlowService 少了 otherBook 那边的婴儿票校验分支。

【输出】
{"items":[
 {"kind":"atomic_fact","scope":"project","subject":"project","role":"fact",
  "content":"gds-agg-processor-tiket 的 bookingFlowService 与 other book service 存在业务逻辑未同步，缺少婴儿票校验分支",
  "keywords":["gds-agg-processor-tiket","bookingFlowService","otherBook"],
  "why":"用户主动指出并要求排查","confidence":0.93},
 {"kind":"discard","reason":"derivable","confidence":0.9}]}

---

【输入片段】
【用户】修复A2 A3
【AI】已修复 A2 和 A3 两处。

【输出】
{"items":[{"kind":"discard","reason":"no_context","confidence":0.97}]}

---

【输入片段】
【用户】回答别啰嗦，先给结论再展开，用中文
【AI内心独白】用户明确要求结论前置，之后所有回答都要遵守，不要先铺陈背景。

【输出】
{"items":[{"kind":"observation",
  "content":"回答风格要求：永远用中文，先给结论再展开，不要啰嗦铺陈",
  "keywords":["中文","结论前置","简洁"],"confidence":0.98}]}

---

【输入片段】
【用户】Travelport 那边技术支持的邮箱是 support@travelport.com，测试环境账号他们还没给开
【AI】我记一下，等开通后再联调。

【输出】
{"items":[{"kind":"atomic_fact","scope":"project","subject":"project","role":"pointer",
  "content":"Travelport 对接：技术支持联系人邮箱是 support@travelport.com；测试环境账号尚未开通，联调需等对方开通",
  "keywords":["Travelport","support@travelport.com","测试环境","联调"],"confidence":0.88}]}
"""

TOOL = {
    "name": "record_route",
    "description": "记录这段对话里值得长期记住的信息及其判类结果。没有就返回空 items。",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/definitions/item"}},
        },
        "required": ["items"],
        "definitions": {
            "item": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "content": {"type": "string", "description": "自包含的一句话，不用代词"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "scope": {"type": "string", "enum": list(SCOPES)},
                    "subject": {"type": "string", "enum": list(SUBJECTS)},
                    "role": {"type": "string", "enum": list(ROLES)},
                    "why": {"type": "string"},
                    "reason": {"type": "string",
                               "enum": ["derivable", "no_context", "one_off",
                                        "rejected_before", "duplicate"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["kind", "confidence"],
            }
        },
    },
}


class RouteItem(BaseModel):
    kind: str
    confidence: float = Field(ge=0, le=1)
    content: str | None = None
    keywords: list[str] = Field(default_factory=list)
    scope: str | None = None
    subject: str | None = None
    role: str | None = None
    # ★ 这里**故意没有 `polarity`**：`raw_observations.polarity='negative'` 是人工否决记录的
    #   专用记号（§4.3.2 比对② 就查它）。让模型也能写 negative，等于一条"系统不支持婴儿票"
    #   的观察会反过来把以后所有婴儿票线索挡成"你以前否决过"——2026-10-23 真跑时踩到了。
    #   pydantic 默认忽略多余字段，所以旧 prompt_ver 重放时模型多吐一个 polarity 也不会炸。
    why: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "RouteItem":
        if self.kind not in KINDS:
            raise ValueError(f"kind 必须是 {KINDS} 之一，收到 {self.kind!r}")
        if self.kind == "discard":
            return self
        if not (self.content or "").strip():
            raise ValueError(f"{self.kind} 必须有 content")
        if self.kind == "observation":
            return self
        for field_name in ("scope", "subject", "role"):
            value = getattr(self, field_name)
            if not value:
                raise ValueError(f"atomic_fact 必须填 {field_name}")
        if self.scope not in SCOPES or self.subject not in SUBJECTS or self.role not in ROLES:
            raise ValueError("scope/subject/role 取值不在受控词表内")
        # memories 表的 CHECK 约束，提前拦住免得整批写入回滚
        if self.scope == "user" and self.subject == "project":
            raise ValueError("scope='user' 不能配 subject='project'")
        return self


class RouteResult(BaseModel):
    items: list[RouteItem] = Field(default_factory=list)


def system_prompt() -> str:
    return f"{SYSTEM}\n\n{FEW_SHOT}"
