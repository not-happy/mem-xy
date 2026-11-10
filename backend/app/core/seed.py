"""Idempotent seed data: orgs, endpoints, attribution rules, the 22 doc_sections (plan §4.4, §5.1)."""

from __future__ import annotations

import logging

from psycopg import Connection

from . import db
from .embedding import get_embedding_service

log = logging.getLogger(__name__)

ORGS = [
    ("personal", "个人", True),
    ("dida", "dida 机票", False),
]

ENDPOINTS = [
    # max_input_tokens is a conservative placeholder: the real ceiling is untested (§14)
    ("dida-gateway", "http://dragon-open-api.didainternal.com",
     "qwen3.8-max-anthropic", "anthropic", 32000,
     "公司内网网关，Anthropic 协议兼容，后端 Qwen。token 不在此表（§2.1）"),
]

# priority: git_remote_path 10 (authoritative) < cwd_prefix 50 < mixed dirs 80
MATCH_RULES = [
    ("git_remote_path", "62bc118a3e81781f3ad1d681/%", "dida", None, 10),
    ("cwd_prefix", "C:/Users/yong.xu/IdeaProjects/dida/", "dida", None, 50),
    ("cwd_prefix", "C:/Users/yong.xu/dida/", "dida", None, 50),
    ("cwd_prefix", "C:/Users/yong.xu/own/", "personal", None, 80),
    ("cwd_prefix", "D:/ai/", "personal", None, 80),
]

# (section_key, label, applies_to, org_key, sort, aliases)
SECTIONS: list[tuple[str, str, str, str | None, int, list[str]]] = [
    # ── ① personal / org ──
    ("local_env", "本机环境与工具", "org", "personal", 10,
     ["JDK", "Java 版本", "Maven", "mvn", "Python", "Node", "环境变量", "PATH",
      "本机", "我电脑上", "SASE", "装在", "路径", "在 D 盘", "版本是"]),
    ("env_pitfalls", "环境已知坑与规避", "org", "personal", 20,
     ["已知坑", "踩坑", "超时", "老是失败", "写操作失败", "分类器超时", "绕一下",
      "workaround", "auto 模式", "四步应对"]),
    ("answer_style", "回答与表达风格", "org", "personal", 30,
     ["中文", "先给结论", "结论前置", "别说太多", "简洁", "表达方式", "小函数",
      "函数别太长", "接地气", "术语"]),
    ("ai_working_rules", "给 AI 的工作纪律", "org", "personal", 40,
     ["日志不要全读", "别一直重试", "重试次数", "写完要校验", "落盘检查", "工作纪律",
      "大文件", "只读相关段"]),
    ("external_communication", "对外沟通", "org", "personal", 50,
     ["写信", "邮件", "英文", "给对方", "供应商技术支持", "语气",
      "别让人觉得是我们的问题"]),

    # ── ② dida / org（全系适用）──
    ("dependency", "依赖与第三方库", "org", "dida", 110,
     ["依赖", "pom", "三方库", "第三方库", "Lombok", "引包", "能不能用这个库"]),
    ("naming_docs", "命名与注释", "org", "dida", 120,
     ["命名", "类名", "方法名", "常量名", "注释", "Javadoc", "@author", "@since",
      "必须大写吗"]),
    ("code_style", "代码风格与格式", "org", "dida", 130,
     ["代码风格", "格式", "行宽", "缩进", "4空格", "K&R", "import", "全路径",
      "函数长度", "try-with-resources", "feign.Response", "资源没关", "接口签名",
      "方法参数", "包装类型", "null 安全", "转换器传参", "P3C"]),
    ("enum_const", "枚举与常量", "org", "dida", 140,
     ["枚举", "enum", "魔法值", "硬编码", "常量", "双向查找", "枚举映射"]),
    ("error_handling", "异常与失败处理", "org", "dida", 150,
     ["异常", "报错", "fail-fast", "fallback", "兜底", "别把内部异常返回出去",
      "吞异常"]),
    ("layering", "分层与模块依赖", "org", "dida", 160,
     ["分层", "模块依赖", "persist", "util", "Converter", "单向", "循环依赖",
      "能不能引用"]),
    ("framework", "框架约定", "org", "dida", 170,
     ["框架", "ServiceImpl", "Abstract 基类", "继承", "@Override", "钩子",
      "logger 声明", "公司框架"]),
    ("domain_model", "领域模型与映射", "org", "dida", 180,
     ["领域模型", "舱位", "cabin", "cabinGrade", "flightClass", "航段", "leg",
      "journeys", "PNR", "PCC", "FFN", "改签", "change", "验价", "verify",
      "婴儿票", "infant", "couponStatus", "字段语义", "这两个别搞混"]),
    ("supplier_protocol", "供应商对接通用协议", "org", "dida", 190,
     ["供应商协议", "GdsStatus", "code", "BaseResponse", "响应码", "状态码", "透传",
      "别伪造 code", "返回码"]),
    ("logging", "日志与可观测", "org", "dida", 200,
     ["日志", "log", "DEBUG", "INFO", "isDebugEnabled", "报文", "成对报文", "飞书",
      "通知", "可观测", "打日志"]),
    ("build_env", "构建与联调", "org", "dida", 210,
     ["构建", "编译", "打包", "mvn", "maven", "-pl", "-am", "离线", "-o", "groupId",
      "com.didatravel", "install", "版本属性", "gds-agg", "联调"]),
    ("git_workflow", "分支与提交", "org", "dida", 220,
     ["分支", "branch", "dev", "提交", "commit", "push", "先不提交", "review",
      "合并", "从 dev 拉", "新分支"]),
    ("process", "协作流程与判断方法", "org", "dida", 230,
     ["流程", "协作", "plan", "以 plan 为准", "偏离要说明", "调研", "字段冗余",
      "框架层有没有用", "Nacos", "运行时配置", "最终看配置", "别只看代码"]),

    # ── ③ dida / project（一供应商一份）──
    ("field_format", "字段格式与取值约束", "project", "dida", 310,
     ["字段格式", "取值", "长度", "前缀", "去前缀", "86-", "手机号", "mobile",
      "不能用中文", "英文名", "firstNameEn", "lastNameEn", "contact", "联系人",
      "枚举值", "大小写", "必填字段", "传空"]),
    ("business_boundary", "业务边界与否决项", "project", "dida", 320,
     ["不支持", "不做", "业务层面", "婴儿", "infant", "儿童", "宠物", "团体",
      "免费行李", "baggage", "id=0", "prices=0", "也要带上", "必须携带", "特殊值",
      "边界", "不覆盖"]),
    ("flow_gotcha", "流程与时序陷阱", "project", "dida", 330,
     ["改签", "change", "executeChange", "退票", "refund", "验价", "verify",
      "verifyChangeDoneByRetrievePnr", "时序", "先后顺序", "易错", "坑", "状态码",
      "0000", "GdsStatus", "判成功", "要校验行程", "RetrievePnr"]),
    ("contact_ops", "对接与运维事项", "project", "dida", 340,
     ["对接", "联调", "对方", "技术支持", "供应商说", "账号", "密码", "PCC", "pcc",
      "office", "couponStatus", "测试环境", "生产环境", "白名单", "开通", "配额",
      "限流", "联系人邮箱"]),
]


def match_text(label: str, aliases: list[str]) -> str:
    """The exact string that `match_embedding` encodes — stored so staleness is checkable."""
    return f"{label}。{'、'.join(aliases)}"


def seed_all(*, with_embeddings: bool = True) -> dict:
    stats = {"orgs": 0, "endpoints": 0, "match_rules": 0, "sections": 0, "embeddings": 0}
    with db.transaction() as conn:
        for org_key, name, is_personal in ORGS:
            stats["orgs"] += conn.execute(
                "INSERT INTO orgs (org_key, name, is_personal) VALUES (%s,%s,%s) "
                "ON CONFLICT (org_key) DO NOTHING",
                (org_key, name, is_personal),
            ).rowcount
        for row in ENDPOINTS:
            stats["endpoints"] += conn.execute(
                "INSERT INTO llm_endpoints (endpoint_key, base_url, model, api_protocol,"
                "                            max_input_tokens, note)"
                " VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (endpoint_key) DO UPDATE"
                "   SET base_url=EXCLUDED.base_url, model=EXCLUDED.model,"
                "       api_protocol=EXCLUDED.api_protocol,"
                "       max_input_tokens=EXCLUDED.max_input_tokens",
                row,
            ).rowcount
        for rule in MATCH_RULES:
            stats["match_rules"] += conn.execute(
                "INSERT INTO org_match_rules (pattern_type, pattern, org_key, project_key, priority)"
                " VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (pattern_type, pattern) DO UPDATE"
                "   SET org_key=EXCLUDED.org_key, priority=EXCLUDED.priority, enabled=TRUE",
                rule,
            ).rowcount
        for section_key, label, applies_to, org_key, sort, aliases in SECTIONS:
            stats["sections"] += conn.execute(
                "INSERT INTO doc_sections (section_key, label, aliases, applies_to,"
                "                          org_key, sort, match_text)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (section_key) DO UPDATE"
                "   SET label=EXCLUDED.label, aliases=EXCLUDED.aliases,"
                "       applies_to=EXCLUDED.applies_to, org_key=EXCLUDED.org_key,"
                "       sort=EXCLUDED.sort, match_text=EXCLUDED.match_text",
                (section_key, label, aliases, applies_to, org_key, sort,
                 match_text(label, aliases)),
            ).rowcount

    if with_embeddings:
        stats["embeddings"] = recompute_section_embeddings()
    log.info("seed done: %s", stats)
    return stats


def recompute_section_embeddings() -> int:
    """§5.3: only 22 short strings — synchronous, never a job.

    Must be re-run whenever label/aliases change, otherwise matching goes stale.
    """
    rows = db.query(
        "SELECT section_key, label, aliases, match_text, match_embedding FROM doc_sections"
    )
    service = get_embedding_service()
    updated = 0
    for row in rows:
        text = match_text(row["label"], row["aliases"])
        if row["match_text"] == text and row["match_embedding"] is not None:
            continue
        vector = service.encode_one(text)
        db.execute(
            "UPDATE doc_sections SET match_embedding=%s, match_text=%s WHERE section_key=%s",
            (vector, text, row["section_key"]),
        )
        updated += 1
    log.info("section match embeddings recomputed: %d/%d", updated, len(rows))
    return updated


def verify() -> dict:
    checks = {
        "orgs": db.query_one("SELECT count(*)::int AS n FROM orgs")["n"],
        "llm_endpoints": db.query_one("SELECT count(*)::int AS n FROM llm_endpoints")["n"],
        "org_match_rules": db.query_one("SELECT count(*)::int AS n FROM org_match_rules")["n"],
        "doc_sections": db.query_one("SELECT count(*)::int AS n FROM doc_sections")["n"],
        "sections_with_embedding": db.query_one(
            "SELECT count(*)::int AS n FROM doc_sections WHERE match_embedding IS NOT NULL"
        )["n"],
    }
    checks["ok"] = (
        checks["doc_sections"] == len(SECTIONS)
        and checks["sections_with_embedding"] == len(SECTIONS)
        and checks["orgs"] >= len(ORGS)
        and checks["llm_endpoints"] >= len(ENDPOINTS)
    )
    return checks


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(seed_all())
    print(verify())
