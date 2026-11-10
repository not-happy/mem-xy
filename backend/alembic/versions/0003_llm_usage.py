"""llm_usage：每次 LLM 调用一行，token 消耗监控的唯一数据源

Revision ID: 0003_llm_usage
Revises: 0002_proposal_sources
Create Date: 2026-10-27

★ 为什么加：全量回填（§13 P2 / §14 第 3 项）要烧 ~259 次 route + 数十次
  write_facts/summarize 调用，`LLMResponse` 里的 usage 此前只回显在返回值上、
  用完即丢——没有它，"回填花了多少 token"只能拍脑袋。
  这张表只追加不更新，量级 = LLM 调用次数（千级），不是审计大表。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_llm_usage"
down_revision: Union[str, None] = "0002_proposal_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id            BIGSERIAL PRIMARY KEY,
            task          TEXT NOT NULL,              -- route/write_facts/summarize/...
            endpoint_key  TEXT NOT NULL,
            model         TEXT NOT NULL,              -- 网关回显的（可能缺 -anthropic 后缀）
            thinking      BOOLEAN NOT NULL DEFAULT FALSE,
            input_tokens  INT NOT NULL DEFAULT 0,
            output_tokens INT NOT NULL DEFAULT 0,
            latency_ms    INT NOT NULL DEFAULT 0,
            ok            BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = HTTP/传输失败（无 usage）
            error         TEXT,                       -- 已 redact（§2.1）
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS llm_usage_task_time ON llm_usage (task, created_at);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS llm_usage")
