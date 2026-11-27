"""★ 双判共识制（2026-11-27，decisions-log #116）：入池审核的第二遍 LLM 复判。

第一遍（router 的 route LLM）决定什么进待办；本模块给每张待办卡再过一遍
**独立**的 LLM 复判：

- **一致 → 自动落库**（resolved_by='llm_auto'）：
  - observation 卡 = 第二遍也判 observation（栏位缺失时第二遍补上也算一致）；
  - below_threshold 卡 = 第一遍 kind=atomic_fact（置信度不够才进待办），
    第二遍也判 atomic_fact → 第二遍独立判断即担保，直接写 memories。
- **分歧 → 留人**：第二遍意见写进 suggestion.llm_review（前端当参考展示），
  卡退回 pending，人照常批准/否决——人的裁决语义（含否决）原封不动。
- **merge 卡（stage='dedup'）**：第一遍是向量近邻不是 LLM，没有"共识"可言，
  由 LLM 读两条原文裁决：verdict=merge → 自动执行合并；keep_both → 自动关卡
  （status='rejected'，不写人工否决记录——reject() 从不写 raw_observations）。
  这是"永不自动合并"旧决策的修订：余弦分不开重复/超集，LLM 读全文分得开，
  且有 keep_both 保守出口。
- **discard 永不自动**：route 判 discard 的条目根本不进待办（router._decide），
  待办里出现"第二遍判 discard"必然与第一遍分歧 → 留人。人工否决语义
  （polarity='negative'）零风险。
- LLM 调用失败 → 异常抛出，job 走 A8 指数退避重试；卡保持 pending 原状。

LLM 调用全部在事务外（§5.5）；开关 settings.auto_review_enabled，
False 时触发点直接不排 job（零开销退回纯人工模式）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..core.config import get_settings
from ..core.llm import get_llm_client
from ..prompts import auto_review as auto_review_prompt
from . import review

log = logging.getLogger(__name__)

JOB_KIND = "auto_review"
RESOLVED_BY = "llm_auto"


def handle(payload: dict) -> dict:
    """worker 入口：复核一张待办卡。返回 {"action": ...} 供日志观测。"""
    settings = get_settings()
    if not settings.auto_review_enabled:
        # 开关关了但队列里还残留 job（例如刚关掉开关）——直接跳过不判。
        return {"action": "disabled"}

    pending_id = payload["pending_id"]
    card = _load(pending_id)
    if card is None or card["status"] != "pending":
        # 已被人审掉 / 已失效 / 已是终态——重放与竞态都归 skipped。
        return {"action": "skipped"}

    if card["stage"] == "dedup":
        return _judge_merge(card)
    return _judge_route_card(card)


# --------------------------------------------------------------------------
# route 卡：observation / below_threshold
# --------------------------------------------------------------------------

def _judge_route_card(card: dict) -> dict:
    client = get_llm_client()
    judge = _call_review_llm(client, card)

    first = card["suggestion"] or {}
    model_kind = first.get("model_kind") or first.get("kind") or "observation"
    section_key = first.get("section_key")

    # 共识①：第二遍判 discard 必然分歧（第一遍不会把 discard 送来）。
    if judge.kind == "discard":
        return _leave_for_human(card, judge, reason="second_opinion_discard")

    # 共识②：kind 必须一致。atomic_fact 卡的第二遍 observation（或反过来）
    # 是实质分歧——一个是"每次提醒"，一个是"长期事实"，方向完全不同。
    if judge.kind != model_kind:
        return _leave_for_human(card, judge, reason="kind_disagreement")

    # 共识③：atomic_fact 需要 scope/subject/role 齐全才算可自动落库。
    # （第一遍是 below_threshold 才进来的，字段可能不全。）
    if judge.kind == "atomic_fact" and not all(
            (judge.scope, judge.subject, judge.role)):
        return _leave_for_human(card, judge, reason="missing_facets")

    # 共识④：栏位。section_unmatched 卡（第一遍没匹配上）由第二遍补齐；
    # 第二遍也给不出 → 留人。
    if not section_key and not (judge.section_key or "").strip():
        return _leave_for_human(card, judge, reason="section_still_unmatched")

    # —— 全部共识达成：按两遍一致的判定自动落库 ——
    resolution: dict[str, Any] = {
        "kind": judge.kind,
        "resolved_by": RESOLVED_BY,
    }
    if judge.scope:
        resolution["scope"] = judge.scope
    if judge.subject:
        resolution["subject"] = judge.subject
    if judge.role:
        resolution["role"] = judge.role
    if not section_key and judge.section_key:
        resolution["section_key"] = judge.section_key
    if judge.keywords:
        resolution["keywords"] = judge.keywords
    # content 修订建议只在确有实质差异时才采纳（LLM 常原样复述）。
    original = (first.get("content") or card["raw_content"] or "").strip()
    if judge.content and judge.content.strip() and judge.content.strip() != original:
        resolution["content"] = judge.content.strip()

    job_id = review.approve(card["id"], resolution, resolved_by=RESOLVED_BY)
    log.info("auto_review 卡 %d 双判一致（%s）→ 自动落库 job=%s",
             card["id"], judge.kind, job_id)
    return {"action": "auto_approved", "pending_id": card["id"], "job_id": job_id}


def _leave_for_human(card: dict, judge: auto_review_prompt.JudgeItem,
                     *, reason: str) -> dict:
    """分歧：第二遍意见存进 suggestion.llm_review，卡保持 pending 等人。"""
    from ..core import db

    opinion = {
        "kind": judge.kind,
        "confidence": judge.confidence,
        "why": judge.why,
        "reason": reason,
    }
    if judge.content:
        opinion["content"] = judge.content
    if judge.section_key:
        opinion["section_key"] = judge.section_key

    with db.transaction() as conn:
        conn.execute(
            "UPDATE pending_items"
            "   SET suggestion = suggestion || %s::jsonb,"
            "       reason = reason || %s"
            " WHERE id = %s AND status = 'pending'",
            (json.dumps({"llm_review": opinion}, ensure_ascii=False),
             f"+llm_disagreed:{reason}", card["id"]),
        )
    log.info("auto_review 卡 %d 分歧（%s，第二遍 %s）→ 留人", card["id"], reason, judge.kind)
    return {"action": "left_for_human", "pending_id": card["id"], "reason": reason}


def _call_review_llm(client, card: dict) -> auto_review_prompt.JudgeItem:
    """组装第二遍的输入上下文并调 LLM（事务外）。"""
    first = card["suggestion"] or {}
    facets = "\n".join(
        f"- {key}: {first[key]}" for key in
        ("model_kind", "scope", "subject", "role", "section_key",
         "section_score", "confidence", "why") if first.get(key) is not None)
    user = (
        f"【待复核线索】\n{card['raw_content']}\n\n"
        f"【第一遍判定（仅供参考，请独立判断）】\n{facets or '（无）'}\n\n"
        "请按判类标准给出你的独立复核结论。"
    )
    result = client.structured(
        task="auto_review",
        system=auto_review_prompt.system_prompt(),
        messages=[{"role": "user", "content": user}],
        tool=auto_review_prompt.TOOL,
        schema=auto_review_prompt.JudgeResult,
        max_tokens=1024,
    )
    return result.kind


# --------------------------------------------------------------------------
# merge 卡（stage='dedup'）
# --------------------------------------------------------------------------

def _judge_merge(card: dict) -> dict:
    client = get_llm_client()
    first = card["suggestion"] or {}
    memory_ids = first.get("memory_ids") or []
    user = (
        f"【记忆 A · #{memory_ids[0] if memory_ids else '?'}】\n"
        f"{first.get('content_a') or ''}\n\n"
        f"【记忆 B · #{memory_ids[1] if len(memory_ids) > 1 else '?'}】\n"
        f"{first.get('content_b') or ''}\n\n"
        f"（向量相似度 {first.get('similarity')}）\n\n"
        "请裁决这两条记忆是否为同一件事的重复表述。"
    )
    judge: auto_review_prompt.MergeJudge = client.structured(
        task="auto_review",
        system=auto_review_prompt.merge_system_prompt(),
        messages=[{"role": "user", "content": user}],
        tool=auto_review_prompt.MERGE_TOOL,
        schema=auto_review_prompt.MergeJudge,
        max_tokens=1024,
    )
    if judge.verdict == "merge":
        # 自动合并：content 用 LLM 给的合并文案（缺省退回 A 原文）。
        resolution = {
            "kind": "merge_pair",
            "content": (judge.content or "").strip() or first.get("content") or "",
            "resolved_by": RESOLVED_BY,
        }
        job_id = review.approve(card["id"], resolution, resolved_by=RESOLVED_BY)
        log.info("auto_review merge 卡 %d verdict=merge → 自动合并 job=%s", card["id"], job_id)
        return {"action": "auto_merged", "pending_id": card["id"], "job_id": job_id}

    # keep_both：向量拉错人了，自动关卡（reject 不写人工否决记录，语义干净）。
    note = judge.why or "第二遍复判：两条是不同的记忆（keep_both）"
    ok = review.reject(card["id"], note=note, resolved_by=RESOLVED_BY)
    if not ok:
        # 竞态：刚被人处理了——不算失败。
        return {"action": "skipped", "pending_id": card["id"]}
    log.info("auto_review merge 卡 %d verdict=keep_both → 自动关卡", card["id"])
    return {"action": "auto_rejected", "pending_id": card["id"]}


def _load(pending_id):
    from ..core import db

    with db.transaction() as conn:
        row = conn.execute(
            "SELECT id, stage, status, org_key, project_key, raw_content,"
            "       suggestion, prompt_ver"
            "  FROM pending_items WHERE id = %s", (pending_id,),
        ).fetchone()
        return dict(row) if row else None
