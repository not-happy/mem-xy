"""doc_proposals.source_ids：提案 ↔ 观察池的溯源链

Revision ID: 0002_proposal_sources
Revises: 0001_initial
Create Date: 2026-10-25

★ 为什么非加不可：汇总器一造出提案就把那批 observations 标 `consumed`，
  而 `consumed` 是个布尔——**不记是被哪个提案消费掉的**。这一刀下去，
  "living_doc 这条规矩当初是从哪几条线索汇总来的"就永久查不出来了，
  而 `living_doc.source_ids` 这个列存在的意义正是它（§11 的溯源展示要用）。
  信息在写提案的那一刻还在手上，过了就没了，所以只能现在存。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0002_proposal_sources"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ★ `CREATE INDEX IF NOT EXISTS` **必须带索引名**：省掉名字时解析器会在 `ON` 上
    #   报语法错误（不带 IF NOT EXISTS 才可以省，0001 里那些就是省的）。
    op.execute("""
        ALTER TABLE doc_proposals
            ADD COLUMN IF NOT EXISTS source_ids BIGINT[] NOT NULL DEFAULT '{}';
        CREATE INDEX IF NOT EXISTS doc_proposals_source_ids_gin
            ON doc_proposals USING gin (source_ids);
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE doc_proposals DROP COLUMN IF EXISTS source_ids")
