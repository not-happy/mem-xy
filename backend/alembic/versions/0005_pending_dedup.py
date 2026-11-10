"""pending_items 幂等键：dedup_hash + 部分唯一索引。

Revision ID: 0005_pending_dedup
Revises: 0004_job_progress

背景（EXPERT-REVIEW A3 / decisions-log #70）：route job 租约被回收后可能被两个
worker 先后执行，`pending_items` 没有幂等约束 ⇒ 同一批素材排出两套待办卡，
直接膨胀人审队列。补一列 `dedup_hash`（= sha256(prompt_ver|source_msg|kind|content)，
见 router._dedup_hash），插入一律 `ON CONFLICT (dedup_hash) DO NOTHING`。

★ 键里含 prompt_ver：换版全量重判（§2.3）时同一素材要能出新卡，
  挡掉的只是同一版判定下的重复消费。
★ 部分索引 `WHERE dedup_hash IS NOT NULL`：存量行（本迁移之前落的卡）没有 hash，
  NULL 也天然互不相等，不回填、不撞键。
"""

from __future__ import annotations

from alembic import op

revision: str = "0005_pending_dedup"
down_revision: str | None = "0004_job_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pending_items ADD COLUMN IF NOT EXISTS dedup_hash TEXT")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS pending_items_dedup"
               " ON pending_items (dedup_hash) WHERE dedup_hash IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS pending_items_dedup")
    op.execute("ALTER TABLE pending_items DROP COLUMN IF EXISTS dedup_hash")
