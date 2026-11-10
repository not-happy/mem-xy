"""initial schema (plan §4.1)

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-22
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

-- ══ 组织与归属 ══
CREATE TABLE orgs (
    org_key          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    is_personal      BOOLEAN NOT NULL DEFAULT FALSE,
    retention_days   INT,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE llm_endpoints (
    endpoint_key  TEXT PRIMARY KEY,
    base_url      TEXT NOT NULL,
    model         TEXT NOT NULL,
    api_protocol  TEXT NOT NULL DEFAULT 'anthropic'
                  CHECK (api_protocol IN ('anthropic','openai','ollama')),
    max_input_tokens INT,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    note          TEXT
);

CREATE TABLE org_match_rules (
    id           BIGSERIAL PRIMARY KEY,
    pattern_type TEXT NOT NULL
                 CHECK (pattern_type IN ('cwd_prefix','git_remote_host','git_remote_path')),
    pattern      TEXT NOT NULL,
    org_key      TEXT NOT NULL REFERENCES orgs(org_key),
    project_key  TEXT,
    priority     INT  NOT NULL DEFAULT 100,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (pattern_type, pattern)
);

CREATE TABLE workspace_map (
    cwd           TEXT PRIMARY KEY,
    org_key       TEXT REFERENCES orgs(org_key),
    project_key   TEXT CHECK (project_key IS NULL OR project_key ~ '^[^/]+/[^/]+$'),
    match_rule_id BIGINT REFERENCES org_match_rules(id),
    resolved_by   TEXT NOT NULL DEFAULT 'rule'
                  CHECK (resolved_by IN ('rule','human','default')),
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    resolved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ══ 层0：原始对话 ══
CREATE TABLE conversation_log (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    src_uuid    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    org_key     TEXT,
    project_key TEXT,
    cwd         TEXT,
    git_branch  TEXT,
    role        TEXT NOT NULL
                CHECK (role IN ('user','assistant','thinking','summary')),
    content     TEXT NOT NULL,
    turn_no     INT,
    tokens_est  INT,
    msg_ts      TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, src_uuid)
);
CREATE INDEX ON conversation_log (session_id, id);
CREATE INDEX conversation_log_unattributed
    ON conversation_log (id) WHERE org_key IS NULL;

CREATE TABLE extraction_cursor (
    source       TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    last_log_id  BIGINT  NOT NULL DEFAULT 0,
    prompt_ver   TEXT    NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, session_id)
);

-- ══ 受控词表 ══
CREATE TABLE doc_sections (
    section_key TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    aliases     TEXT[] NOT NULL DEFAULT '{}',
    match_embedding vector(1024),
    match_text  TEXT,
    applies_to  TEXT NOT NULL DEFAULT 'org'
                CHECK (applies_to IN ('org','project','both')),
    org_key     TEXT REFERENCES orgs(org_key),
    sort        INT NOT NULL DEFAULT 100,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE
);

-- ══ 层1：小本子 ══
CREATE TABLE memories (
    id           BIGSERIAL PRIMARY KEY,
    scope        TEXT NOT NULL CHECK (scope IN ('user','org','project')),
    org_key      TEXT,
    project_key  TEXT,
    subject      TEXT NOT NULL CHECK (subject IN ('user','project')),
    role         TEXT NOT NULL
                 CHECK (role IN ('profile','preference','pointer','fact')),
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    keywords     TEXT[] NOT NULL DEFAULT '{}',
    why          TEXT,
    embedding        vector(1024),
    embedding_sparse sparsevec(250002),
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','deleted')),
    source_msg   BIGINT REFERENCES conversation_log(id),
    mention_count  INT NOT NULL DEFAULT 1,
    revision_count INT NOT NULL DEFAULT 0,
    prompt_ver   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT (scope='user' AND subject='project')),
    CHECK (scope <> 'org'     OR org_key IS NOT NULL),
    CHECK (scope <> 'project' OR (org_key IS NOT NULL AND project_key IS NOT NULL)),
    UNIQUE (source_msg, content_hash)
);
CREATE INDEX memories_emb_hnsw ON memories
    USING hnsw (embedding vector_cosine_ops) WHERE status='active';
CREATE INDEX memories_kw_gin ON memories
    USING gin (keywords) WHERE status='active';
CREATE INDEX memories_bucket ON memories
    (scope, org_key, project_key, subject, role) WHERE status='active';

-- ══ 层2：线索池 ══
CREATE TABLE raw_observations (
    id           BIGSERIAL PRIMARY KEY,
    org_key      TEXT NOT NULL,
    project_key  TEXT,
    section_key  TEXT NOT NULL REFERENCES doc_sections(section_key),
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    polarity     TEXT NOT NULL DEFAULT 'positive'
                 CHECK (polarity IN ('positive','negative')),
    keywords     TEXT[] NOT NULL DEFAULT '{}',
    embedding    vector(1024),
    consumed     BOOLEAN NOT NULL DEFAULT FALSE,
    consumed_at  TIMESTAMPTZ,
    source_msg   BIGINT REFERENCES conversation_log(id),
    prompt_ver   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_msg, content_hash)
);
CREATE INDEX obs_hnsw ON raw_observations
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX obs_pending ON raw_observations
    (org_key, section_key, id) WHERE consumed = FALSE;
CREATE INDEX obs_negative_hnsw ON raw_observations
    USING hnsw (embedding vector_cosine_ops)
    WHERE polarity = 'negative';

-- ══ 层3：规矩清单 ══
CREATE TABLE living_doc (
    id          BIGSERIAL PRIMARY KEY,
    org_key     TEXT NOT NULL REFERENCES orgs(org_key),
    project_key TEXT,
    section_key TEXT NOT NULL REFERENCES doc_sections(section_key),
    content     TEXT NOT NULL,
    authority   TEXT NOT NULL DEFAULT 'draft'
                CHECK (authority IN ('draft','confirmed','pinned')),
    origin      TEXT NOT NULL DEFAULT 'summarized'
                CHECK (origin IN ('summarized','human')),
    origin_ref  TEXT,
    source_ids  BIGINT[] NOT NULL DEFAULT '{}',
    version     INT NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX living_doc_uniq
    ON living_doc (org_key, section_key, COALESCE(project_key, ''));
CREATE INDEX living_doc_inject
    ON living_doc (org_key, project_key, authority, section_key);

CREATE TABLE living_doc_history (
    id            BIGSERIAL PRIMARY KEY,
    living_doc_id BIGINT NOT NULL REFERENCES living_doc(id) ON DELETE CASCADE,
    version       INT NOT NULL,
    old_content   TEXT,
    new_content   TEXT NOT NULL,
    old_authority TEXT,
    new_authority TEXT NOT NULL,
    proposal_id   BIGINT,
    changed_by    TEXT NOT NULL CHECK (changed_by IN ('human','summarizer','replay')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON living_doc_history (living_doc_id, version);

CREATE TABLE doc_proposals (
    id          BIGSERIAL PRIMARY KEY,
    org_key     TEXT NOT NULL,
    project_key TEXT,
    section_key TEXT NOT NULL,
    old_content TEXT,
    new_content TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','accepted','rejected','expired')),
    job_id      BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);
CREATE INDEX ON doc_proposals (status, created_at) WHERE status='pending';

-- ══ 待人工裁决 ══
CREATE TABLE pending_items (
    id          BIGSERIAL PRIMARY KEY,
    org_key     TEXT,
    project_key TEXT,
    stage       TEXT NOT NULL CHECK (stage IN ('route','dedup','section')),
    raw_content TEXT NOT NULL,
    source_msg  BIGINT,
    suggestion  JSONB,
    reason      TEXT,
    prompt_ver  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','writing',
                                  'done','rejected','failed','expired')),
    resolution  JSONB,
    job_id      BIGINT,
    last_error  TEXT,
    resolved_by TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ
);
CREATE INDEX ON pending_items (status, created_at)
    WHERE status IN ('pending','writing','failed');

-- ══ 任务队列 ══
CREATE TABLE jobs (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,
    payload      JSONB NOT NULL,
    org_key      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','running','done','failed','cancelled')),
    attempts     INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    lease_until  TIMESTAMPTZ,
    dedup_key    TEXT,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX jobs_dedup ON jobs (dedup_key)
    WHERE status IN ('pending','running');
CREATE INDEX jobs_claim ON jobs (status, id)
    WHERE status IN ('pending','running');

-- ══ 标注资产 ══
CREATE TABLE route_decisions (
    id           BIGSERIAL PRIMARY KEY,
    source_msg   BIGINT,
    raw_snippet  TEXT NOT NULL,
    prompt_ver   TEXT NOT NULL,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('observation','atomic_fact','discard')),
    confidence   NUMERIC(4,3) NOT NULL,
    model_output JSONB NOT NULL,
    human_output JSONB,
    overridden   BOOLEAN NOT NULL DEFAULT FALSE,
    org_key      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON route_decisions (prompt_ver, kind, overridden);

-- ══ 召回审计 + 评测 ══
CREATE TABLE recall_log (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT,
    session_id   TEXT NOT NULL,
    turn_no      INT,
    query_text   TEXT NOT NULL,
    org_key      TEXT,
    project_key  TEXT,
    hits         JSONB NOT NULL,
    injected_ids BIGINT[] NOT NULL DEFAULT '{}',
    latency_ms   INT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON recall_log (session_id, turn_no);
CREATE INDEX ON recall_log (created_at);

CREATE TABLE memory_feedback (
    id            BIGSERIAL PRIMARY KEY,
    memory_id     BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    recall_log_id BIGINT REFERENCES recall_log(id),
    kind          TEXT NOT NULL CHECK (kind IN ('corrected','useless','useful')),
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON memory_feedback (memory_id);

CREATE TABLE eval_recall_cases (
    id           BIGSERIAL PRIMARY KEY,
    label        TEXT,
    query_text   TEXT NOT NULL,
    org_key      TEXT,
    project_key  TEXT,
    expected_ids BIGINT[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE eval_runs (
    id            BIGSERIAL PRIMARY KEY,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    prompt_ver    TEXT,
    config        JSONB,
    hit_at_5      NUMERIC,
    mrr           NUMERIC,
    suspend_rate  NUMERIC,
    override_rate NUMERIC,
    p95_ms        INT,
    notes         TEXT
);

-- ══ shadow 重跑 ══
CREATE TABLE replay_runs (
    id           BIGSERIAL PRIMARY KEY,
    prompt_ver   TEXT NOT NULL,
    scope_filter JSONB,
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running','awaiting_review','applied','discarded')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);

CREATE TABLE replay_actions (
    id               BIGSERIAL PRIMARY KEY,
    run_id           BIGINT NOT NULL REFERENCES replay_runs(id) ON DELETE CASCADE,
    action           TEXT NOT NULL CHECK (action IN ('ADD','UPDATE','DELETE','NOOP')),
    target_memory_id BIGINT,
    proposed_content TEXT,
    proposed_meta    JSONB,
    applied          BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ON replay_actions (run_id, applied);

-- ══ 审计 ══
CREATE TABLE memory_events (
    id          BIGSERIAL PRIMARY KEY,
    memory_id   BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('ADD','UPDATE','DELETE','NOOP','RESTORE')),
    old_content TEXT,
    new_content TEXT,
    source_msg  BIGINT,
    actor       TEXT NOT NULL DEFAULT 'system',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON memory_events (memory_id, id);
"""

TABLES = [
    "memory_events", "replay_actions", "replay_runs", "eval_runs", "eval_recall_cases",
    "memory_feedback", "recall_log", "route_decisions", "jobs", "pending_items",
    "doc_proposals", "living_doc_history", "living_doc", "raw_observations", "memories",
    "doc_sections", "extraction_cursor", "conversation_log", "workspace_map",
    "org_match_rules", "llm_endpoints", "orgs",
]


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP EXTENSION IF EXISTS vector")
