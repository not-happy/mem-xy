"""Phase A 止血（ROADMAP 2026-11-26）：job 退避 / route 逐窗 checkpoint / 事件与提案幂等。

四件事：

1. A8：jobs.next_retry_at——fail() 写指数退避（60s×2^(attempts-1) 封顶 1h），
   claim() 未到点的行不认领。网关故障分钟级烧光重试预算的问题（评审 S3）。
2. A5：route_checkpoints——route 三段式中段② 每窗 LLM 成功即落一行 checkpoint，
   重试从断点续，不再整会话重付（评审 S4）。_finish 推游标后清已消费行。
3. A7：memory_events(memory_id, action, source_msg) 唯一索引——write_facts 租约过期
   双执行时事件表不再重复记账（评审 W3）。先删存量重复（保留最小 id），再建唯一索引。
4. A10：living_doc_history.proposal_id 部分唯一索引——双 worker 并发 accept 同一提案
   时 TOCTOU 兜底（评审 W5）。同样先删存量重复。

全部用 IF NOT EXISTS / 防御性去重，重复执行安全。downgrade 只删结构不改数据。
"""

from alembic import op

revision = "0007_phase_a_hardening"
down_revision = "0006_eval_must_not"
branch_labels = None
depends_on = None

UP_SQL = """
-- ── A8：job 指数退避 ─────────────────────────────────────────────
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;

-- ── A5：route 逐窗 checkpoint ──────────────────────────────────
CREATE TABLE IF NOT EXISTS route_checkpoints (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    first_msg_id BIGINT NOT NULL,
    prompt_ver   TEXT NOT NULL,
    model_output JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS route_checkpoints_uniq
    ON route_checkpoints (source, session_id, first_msg_id, prompt_ver);

-- ── A7：memory_events 幂等（防 write_facts 双执行重复记账）────────
-- 事件同一性 = (memory_id, action, source_msg)：回填/重放产生的重复行内容可能有
-- 出入（old/new_content 因记忆后续又被更新而不同），但记账语义是同一个，保留最小 id。
-- 实测生产库 15 组重复（最大一组 3 行），全部是回填双写产物。
-- NULL source_msg 默认 NULLS DISTINCT，天然不受唯一索引约束（人工录入的事件本就
-- 不需要防重）。
DELETE FROM memory_events a
 USING memory_events b
  WHERE a.id > b.id
    AND a.memory_id = b.memory_id
    AND a.action = b.action
    AND a.source_msg = b.source_msg;
CREATE UNIQUE INDEX IF NOT EXISTS memory_events_action_uniq
    ON memory_events (memory_id, action, source_msg);

-- ── A10：living_doc_history.proposal_id 部分唯一（TOCTOU 兜底）────
DELETE FROM living_doc_history a
 USING living_doc_history b
  WHERE a.id > b.id
    AND a.proposal_id IS NOT NULL
    AND a.proposal_id = b.proposal_id;
CREATE UNIQUE INDEX IF NOT EXISTS living_doc_history_proposal_uniq
    ON living_doc_history (proposal_id) WHERE proposal_id IS NOT NULL;
"""

DOWN_SQL = """
DROP INDEX IF EXISTS living_doc_history_proposal_uniq;
DROP INDEX IF EXISTS memory_events_action_uniq;
DROP TABLE IF EXISTS route_checkpoints;
ALTER TABLE jobs DROP COLUMN IF EXISTS next_retry_at;
"""


def upgrade() -> None:
    op.execute(UP_SQL)


def downgrade() -> None:
    op.execute(DOWN_SQL)
