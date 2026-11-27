"""★ B4-②（S5 余波）：周期近重复合并扫描——把"同一件事记成两条"捞出来给人裁决。

B4-① 放宽了候选分桶（role/subject 不再挡住去重比对），但那只能拦住**未来**
的写入；已经躺在库里的跨标签孪生（#2441/#2506 型：一条算"偏好"、一条算
"事实"）谁也不碰谁，还互相检索不到。本模块每天扫一遍 memories：

- 自连接：同 (scope, org_key, project_key) 分区内、双方 active、向量齐全、
  cosine ≥ `dedup_scan.similarity`（pgvector `<=>` 是余弦距离，阈值 0.90
  ⇔ 距离 ≤ 0.10）。
- 每对生成一张 stage='dedup' 的裁决卡，dedup_hash = "merge:<a>:<b>"——
  同一对天然幂等（部分唯一索引 ON CONFLICT DO NOTHING，重扫不重卡）。
  现存卡若是 failed（系统失败），重扫把它翻回 pending 重新送审——
  "失败要能重试"靠复活旧卡，不是再插一张。
  裁决走 apply_decision 的 merge_pair 分支（app/pipeline/review.py）：
  默认保留 id 较小的一条（老记忆带得动 mention 历史），软删另一条；
  人可以在卡片上改合并文案（resolution.content → 重算向量落库）。
- **不调用任何 LLM**：阈值只负责"拉出来看一眼"，判"是不是同一件事"的是人。
  config 里 `thresholds.dedup_prefilter` 的注释说得更狠：余弦分不开"重复"
  和"超集"——所以这卡永远进人工，不自动合并。

已知取舍：
- 被人否决（rejected）过的对不再出卡（NOT EXISTS 过滤含 rejected）——内容
  后续真变了要等它和别的行撞出新对；省掉"每天骚扰一次"的体验代价。
- 自连接是 O(n²)，个人库规模（几千行）下每天跑一次可接受；库大到分钟级
  再考虑按 scope 分块（C 阶段再议）。

调度：`maybe_enqueue_daily()` 仿 cleanup（24h 节流 + UTC 日期 dedup_key），
挂 worker 启动处，不新增人工环节。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging

from .core.config import get_settings

log = logging.getLogger(__name__)

JOB_KIND = "dedup_scan"


def scan(conn) -> dict:
    """扫一轮近邻对、发合并裁决卡。返回 {"pairs": 扫到的对数, "cards": 新卡数}。

    调用方给连接（自管事务模式，与 cleanup 同款）：扫描和发卡必须同事务——
    半途崩掉就整批回滚，不留下"扫了一半"的中间态。
    """
    settings = get_settings()
    distance = 1.0 - settings.dedup_scan_similarity
    max_cards = settings.dedup_scan_max_cards

    pairs = conn.execute(
        """
        SELECT * FROM (
            SELECT a.id AS a_id, b.id AS b_id,
                   a.content AS a_content, b.content AS b_content,
                   a.scope AS scope, a.org_key AS org_key,
                   a.project_key AS project_key,
                   a.source_msg AS a_source_msg,
                   a.embedding <=> b.embedding AS dist
            FROM memories a
            JOIN memories b ON a.id < b.id
                 AND a.scope = b.scope
                 AND a.org_key IS NOT DISTINCT FROM b.org_key
                 AND a.project_key IS NOT DISTINCT FROM b.project_key
            WHERE a.status = 'active' AND b.status = 'active'
              AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
        ) cand
        WHERE cand.dist <= %s
          -- 这一对已经有过一张卡（pending/被否/已完成）就不再发；failed 的除外，
          -- 系统失败要能重试。
          AND NOT EXISTS (
                SELECT 1 FROM pending_items pi
                WHERE pi.dedup_hash = 'merge:' || cand.a_id || ':' || cand.b_id
                  AND pi.status <> 'failed')
        ORDER BY cand.dist ASC
        LIMIT %s
        """,
        (distance, max_cards),
    ).fetchall()

    created = 0
    for p in pairs:
        raw = f"【A · #{p['a_id']}】{p['a_content']}\n【B · #{p['b_id']}】{p['b_content']}"
        suggestion = {
            "kind": "merge_pair",
            "memory_ids": [p["a_id"], p["b_id"]],
            "content_a": p["a_content"],
            "content_b": p["b_content"],
            # 默认保留 A（id 小 = 更老）的原文；人在卡片上改文案会覆盖它。
            "content": p["a_content"],
            "scope": p["scope"],
            "similarity": round(1.0 - p["dist"], 4),
        }
        card = conn.execute(
            """
            INSERT INTO pending_items (org_key, project_key, stage, raw_content,
                                       source_msg, suggestion, reason, status,
                                       dedup_hash)
            VALUES (%s, %s, 'dedup', %s, %s, %s, 'near_duplicate', 'pending', %s)
            ON CONFLICT (dedup_hash) WHERE dedup_hash IS NOT NULL DO NOTHING
            RETURNING id
            """,
            (p["org_key"], p["project_key"], raw, p["a_source_msg"],
             json.dumps(suggestion, ensure_ascii=False),
             f"merge:{p['a_id']}:{p['b_id']}"),
        ).fetchone()
        if card is not None:
            created += 1
        else:
            # 唯一索引挡住了重插：若现存卡是 failed（系统失败），把它翻回 pending
            # 重新送到人面前——"失败要能重试"靠的是复活旧卡，不是再插一张。
            revived = conn.execute(
                "UPDATE pending_items SET status='pending', resolved_at=NULL"
                " WHERE dedup_hash=%s AND status='failed'",
                (f"merge:{p['a_id']}:{p['b_id']}",),
            ).rowcount
            if revived:
                created += 1

    log.info("dedup_scan 完成：近邻对 %d，新卡 %d（阈值 %.2f）",
             len(pairs), created, settings.dedup_scan_similarity)
    return {"pairs": len(pairs), "cards": created}


def maybe_enqueue_daily(conn) -> int | None:
    """距离上次成功 dedup_scan 超过 24h 才补一个 job；返回 job id（None=不需要）。

    dedup_key 按 UTC 日期——同一天重复调用天然幂等（jobs_dedup 部分唯一索引）。
    """
    today = _dt.datetime.now(_dt.timezone.utc).strftime("dedup_scan:%Y-%m-%d")
    row = conn.execute(
        "SELECT max(finished_at) AS last_ok FROM jobs"
        " WHERE kind = %s AND status = 'done'", (JOB_KIND,)).fetchone()
    last = row["last_ok"] if row else None
    if last is not None and _dt.datetime.now(_dt.timezone.utc) - last < _dt.timedelta(hours=24):
        return None
    from .queue import jobs as jobs_mod

    return jobs_mod.enqueue(JOB_KIND, {}, dedup_key=today, conn=conn)
