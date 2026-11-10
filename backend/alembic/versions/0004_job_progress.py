"""任务执行过程可视化：jobs.progress + llm_usage.job_id。

Revision ID: 0004_job_progress
Revises: 0003_llm_usage

背景：route job 跑一个大会话动辄几十分钟（游标只在整会话跑完后推进），
期间 UI/日志完全看不出它在动。补两列：
  * `jobs.progress`：worker 每个窗口开头写 {window, windows_total}，UI 画进度条；
  * `llm_usage.job_id`：每次 LLM 调用归属到 job，任务页能下钻看调用时间线。
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_job_progress"
down_revision: str | None = "0003_llm_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS progress JSONB")
    op.execute("ALTER TABLE llm_usage ADD COLUMN IF NOT EXISTS job_id BIGINT")
    op.execute("CREATE INDEX IF NOT EXISTS llm_usage_job ON llm_usage (job_id)"
               " WHERE job_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS llm_usage_job")
    op.execute("ALTER TABLE llm_usage DROP COLUMN IF EXISTS job_id")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS progress")
