"""eval_recall_cases 补 content_hash 判定列：期望答案不再钉死 memory id。

Revision ID: 0008_eval_hashes
Revises: 0007_phase_a_hardening

背景（ROADMAP B1 / S8）：expected_ids / must_not_ids 钉的是记忆行 id——
记忆被合并（B4 的 merge_pair 会软删一方）、重写（UPDATE 换行）或清理后，
用例就静默变成假阴性，评测集会一边腐烂一边"看起来还在跑"。改存
content_hash（app.core.hashing，归一化去首尾空白+丢空行后的 sha256）：
内容还在（哪怕换了行/换了 id）就算命中，内容没了才判失效——失效用
--check 显式报出来，而不是悄悄拖低 hit@5。

★ 两对列并存、都判：ids 列保留（存量 96 条用例不动），hashes 列是新增
判定维度。runner 对任一维命中即算命中（详见 evaluation.py 头注释）。
"""

from __future__ import annotations

from alembic import op

revision: str = "0008_eval_hashes"
down_revision: str | None = "0007_phase_a_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE eval_recall_cases"
               " ADD COLUMN IF NOT EXISTS expected_hashes TEXT[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE eval_recall_cases"
               " ADD COLUMN IF NOT EXISTS must_not_hashes TEXT[] NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE eval_recall_cases DROP COLUMN IF EXISTS must_not_hashes")
    op.execute("ALTER TABLE eval_recall_cases DROP COLUMN IF EXISTS expected_hashes")
