"""recall_log 加 feedback 列：B6 一键"有用/没用"标记 + 负标记进复查池。

Revision ID: 0009_recall_feedback
Revises: 0008_eval_hashes

背景（ROADMAP B6）：hook 侧每轮真实注入已有审计行（recall_log），但用户
对"这轮带得对不对"没有表达通道——整轮标记不该挂在 memory_feedback 上
（那张表要求 memory_id NOT NULL，一行召回要打 N 条记忆才能表达"这轮
没用"）。给 recall_log 直接加一列：

- feedback TEXT NULL，CHECK 只容 'useful' / 'useless'（NULL = 未标记）。
- POST /v1/recall-log/{id}/feedback 写它；"useless" 的行被
  eval --harvest 优先采进候选清单（复查池），标注后 --import。
- 部分索引只盖已标记行：标记是稀疏事件，全表索引纯浪费。
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_recall_feedback"
down_revision: str | None = "0008_eval_hashes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE recall_log"
               " ADD COLUMN IF NOT EXISTS feedback TEXT"
               " CHECK (feedback IN ('useful','useless'))")
    op.execute("CREATE INDEX IF NOT EXISTS recall_log_useless_idx"
               " ON recall_log (created_at) WHERE feedback = 'useless'")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS recall_log_useless_idx")
    op.execute("ALTER TABLE recall_log DROP COLUMN IF EXISTS feedback")
