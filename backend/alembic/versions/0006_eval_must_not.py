"""eval_recall_cases 补 must_not_ids：否定用例的显式语义。

Revision ID: 0006_eval_must_not
Revises: 0005_pending_dedup

背景（EXPERT-REVIEW Phase 1.1 / #77、§6.1）：评测集除了"该召回的召回了吗"
（expected_ids），还要钉住"不该出现的别出现"——串台（org/project 过滤器被改坏）
与已删/被否决记忆复活，都只能靠否定用例发现。原 schema 只有 expected_ids
（NOT NULL），拿空数组或 label 字符串当否定开关都太脆，补一列显式表达。

★ DEFAULT '{}'：存量行（如有）自动是否定集为空的正例，不回填、不改语义。
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_eval_must_not"
down_revision: str | None = "0005_pending_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE eval_recall_cases"
               " ADD COLUMN IF NOT EXISTS must_not_ids BIGINT[] NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE eval_recall_cases DROP COLUMN IF EXISTS must_not_ids")
