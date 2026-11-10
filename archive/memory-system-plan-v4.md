<!-- ★ 已归档（被 v5 取代，v5 又被 memory-system-plan.md 取代）。仅留作历史。 -->

# AI 个人助理记忆系统 · 完整方案 v4

## 0. 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python + FastAPI |
| 数据库 | PostgreSQL + pgvector（已装） |
| DB 驱动 | psycopg3 + pgvector-python |
| Embedding | bge-m3（本地，1024 维） |
| LLM | 云端 API / 本地，按 org.llm_policy 路由 |
| 前端 | React + Vite |
| 迁移 | Alembic |

## 1. 架构总览

```
                    org 上下文（cwd 自动判定）贯穿全链路
                                   │
conversation_log  （不可变原始素材，唯一真相源）
      │  抽取（增量水位线，可重跑，按 org 选模型）
      ▼
   路由器（LLM，每项带 confidence）
      │
      ├─ conf ≥ 阈值 ──────────────► 直接写入
      │        ├─ atomic_fact → 记忆写入器（召回+四动作）→ memories
      │        └─ observation → 沉淀器 → raw_observations
      │
      └─ conf < 阈值 → pending_items(pending) → 【人裁决】→ 批准后写入
                                   │
   （汇总器，定期）raw_observations → living_doc draft
                                   → doc_proposals(pending) → 【人审 diff】

读取侧：三级作用域隔离召回（user穿透/org隔离/project隔离）→ 组装 prompt
```

四条贯穿全局的铁律：

1. 原始日志是真相源，派生三层可随时删了重建、抽取可重跑。
2. user 级穿透，org/project 级严格隔离，物理上不可能跨组织召回。
3. 拿不准就问，绝不静默猜宽（尤其 scope，防跨 org 泄露）。
4. 全链路是表驱动的持久状态机，任意中断点可恢复，且每步幂等。

## 2. 数据模型（完整 schema）

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- ══ 组织与作用域 ══
CREATE TABLE orgs (
    org_key     TEXT PRIMARY KEY,          -- 'company-a'/'company-b'/'personal'
    name        TEXT NOT NULL,
    is_personal BOOLEAN DEFAULT FALSE,
    llm_policy  TEXT DEFAULT 'cloud',      -- cloud / local-only
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE workspace_map (               -- cwd → 归属，自动打标
    cwd         TEXT PRIMARY KEY,
    org_key     TEXT NOT NULL REFERENCES orgs(org_key),
    project_key TEXT NOT NULL
);

-- ══ 层0：原始对话日志（真相源）══
CREATE TABLE conversation_log (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,             -- 'claude'/'qoder'
    session_id  TEXT NOT NULL,
    org_key     TEXT, project_key TEXT,
    role        TEXT NOT NULL,             -- user/assistant
    content     TEXT NOT NULL,             -- 仅文本，不存工具大段输出
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON conversation_log (source, session_id, id);

CREATE TABLE extraction_cursor (           -- 中断点1：增量抽取水位线
    session_id   TEXT PRIMARY KEY,
    last_msg_id  BIGINT NOT NULL,
    prompt_ver   TEXT,                     -- 变了对历史重跑
    extracted_at TIMESTAMPTZ DEFAULT now()
);

-- ══ 层1：原子事实 ══
CREATE TABLE memories (
    id           BIGSERIAL PRIMARY KEY,
    scope        TEXT NOT NULL,            -- user/org/project
    org_key      TEXT, project_key TEXT,
    subject      TEXT NOT NULL,            -- user/project（关于谁）
    role         TEXT NOT NULL,            -- profile/rule/pointer/fact（作用）
    content      TEXT NOT NULL,
    why          TEXT,
    embedding    vector(1024),
    deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    occurrence   INT NOT NULL DEFAULT 1,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now(),
    last_seen_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON memories (scope, org_key, subject, role) WHERE deleted = FALSE;

-- ══ 层2：规范线索沉淀池 ══
CREATE TABLE raw_observations (
    id           BIGSERIAL PRIMARY KEY,
    org_key      TEXT NOT NULL, project_key TEXT,
    hint_section TEXT,
    content      TEXT NOT NULL,
    polarity     TEXT NOT NULL DEFAULT 'positive',  -- positive/negative(否决)
    embedding    vector(1024),
    consumed     BOOLEAN NOT NULL DEFAULT FALSE,
    source_msg   BIGINT,
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON raw_observations USING hnsw (embedding vector_cosine_ops);

-- ══ 层3：AI起草+人审活文档（按 org 各一份）══
CREATE TABLE living_doc (
    id         BIGSERIAL PRIMARY KEY,
    org_key    TEXT NOT NULL,
    doc_key    TEXT NOT NULL,              -- "tech_standards"
    section    TEXT NOT NULL,
    content    TEXT NOT NULL,
    authority  TEXT NOT NULL DEFAULT 'draft',  -- draft/confirmed/pinned
    source_ids BIGINT[],
    version    INT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(org_key, doc_key, section)
);
CREATE TABLE doc_proposals (               -- 中断点3：待审 diff
    id          BIGSERIAL PRIMARY KEY,
    org_key     TEXT NOT NULL, doc_key TEXT NOT NULL, section TEXT NOT NULL,
    old_content TEXT, new_content TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected/expired
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- ══ 中断点2：低置信度待人工裁决 ══
CREATE TABLE pending_items (
    id           BIGSERIAL PRIMARY KEY,
    org_key      TEXT, project_key TEXT,
    stage        TEXT NOT NULL,            -- 'route'(分类)/'dedup'(四动作)
    raw_content  TEXT NOT NULL,
    suggestion   JSONB,                    -- 模型候选+confidence，UI预填
    reason       TEXT,                     -- 为什么拿不准
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/failed/expired
    resolved_by  TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    resolved_at  TIMESTAMPTZ
);

-- ══ 审计 ══
CREATE TABLE memory_events (
    id          BIGSERIAL PRIMARY KEY,
    memory_id   BIGINT,
    action      TEXT NOT NULL,             -- ADD/UPDATE/DELETE/NOOP
    old_content TEXT, new_content TEXT, source_msg BIGINT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
```

### 分类体系

三级作用域 scope：

- `user`：全局，跨所有 org，穿透一切。
- `org`：某组织内共享，仅本 org 可见。
- `project`：单仓库，仅本 project 可见。

memories 内容分类（正交两字段）：

| 字段 | 取值 | 回答 |
|---|---|---|
| subject | user / project | 关于谁 |
| role | profile / rule / pointer / fact | 起什么作用（rule 类应改走 observations） |

## 3. 写入链路（持久状态机 + 幂等）

① 归属判定 — 入库时按 cwd 查 workspace_map 打 org/project；缺失则 LLM 判 + UI 确认记住。

② 路由器（1 次 LLM，按 org.llm_policy 选模型，带版本号）— 从水位线拉未处理消息，拆分+分类，每项输出 confidence：

```json
[{"kind":"atomic_fact","scope":"project","subject":"project","role":"fact",
  "content":"...","why":null,"confidence":0.93},
 {"kind":"observation","hint_section":"命名约定","content":"...","confidence":0.41}]
```

硬规则：is（是什么/在哪）→ atomic_fact；should（应该怎么做）→ observation；含两者拆两条；scope 宁窄勿宽；丢弃项（derivable/ephemeral）带 reason。
幂等：靠 extraction_cursor.last_msg_id，只处理水位线后消息，重跑不重抽。

③ 置信度门控 + 分流：

```
confidence ≥ 阈值：
   atomic_fact → 记忆写入器
   observation → 沉淀器
   derivable/ephemeral → 丢弃+日志
confidence < 阈值（或 kind/scope/四动作模糊）：
   → pending_items(status=pending)  ← 中断，等人裁决
```

④ 记忆写入器（每条 +1 次 LLM）— 四动作去重更新：

```
新事实 → embedding → 按 scope/org/subject/role 分桶向量召回 top5
      → LLM 判定 [{action, memory_id, merged_content}]
      → ADD/UPDATE/DELETE/NOOP + memory_events
```

NOOP 只更新 occurrence/last_seen_at；矛盾走软删除。
幂等：同一事实重来命中 NOOP，不重复插。

⑤ 汇总器（定期，每 org×section 1 次 LLM）：

```
拉未consumed的相关 observations + 当前 living_doc
  confirmed/pinned → 不改，新线索存待审
  draft → 合成新 draft（排除 negative observations）
  → doc_proposals(pending)，标记 observations consumed
```

⑥ 人工裁决（两个闸口，全部条件 UPDATE 保幂等）：

```sql
-- pending_items：批准分类
UPDATE pending_items SET status='approved', resolved_by='human', resolved_at=now()
 WHERE id=$1 AND status='pending';        -- 影响行数=1 才算抢到
-- doc_proposals：采纳 diff（升 confirmed）
UPDATE doc_proposals SET status='accepted' WHERE id=$1 AND status='pending';
```

批准后按人定的类型走 ④/⑤ 写入。裁决结果作为 few-shot 反哺路由 prompt，挂起率随时间下降。

## 4. 读取链路（三级隔离召回）

```sql
WHERE deleted = FALSE
  AND ( scope='user'
     OR (scope='org'     AND org_key=$cur_org)
     OR (scope='project' AND project_key=$cur_project) )
```

组装 prompt（token 预算内，越具体越优先 project > org > user）：

```
[系统规则: living_doc confirmed/pinned]
[已知事实: memories top-K，向量+全文 hybrid，
          加权 = 相似度 × 时间衰减(last_seen) × log(occurrence)]
[当前对话]
```

## 5. 三个中断点（全链路可恢复）

| 中断点 | 表 | 恢复依据 |
|---|---|---|
| 1 抽取 | extraction_cursor | last_msg_id 水位线续跑 |
| 2 分类裁决 | pending_items | status=pending 待处理 |
| 3 文档审核 | doc_proposals | status=pending 待处理 |

任一中断（崩溃/关机/暂停）后重启，从表的中间态继续，不重跑已完成、不丢未完成。配套幂等：水位线 + NOOP + 条件 UPDATE。

## 6. Web UI 页面

| 页面 | 功能 | 优先级 |
|---|---|---|
| 待办中心 | 汇总 pending_items + doc_proposals 待办数，统一入口红点 | 高 |
| 待确认 | 逐条卡片：原文+模型建议+为什么拿不准，选 scope/subject/role/kind → 批准/否决，支持批量 | 高 |
| 待审 diff | 活文档 draft 逐段 采纳/否决/手改 | 高 |
| 记忆库 | 按 org/scope/subject/role 浏览、编辑、删除、看 why 与历史 | 中 |
| 活文档 | 分 org 展示规范 + 权威级；draft 升 confirmed | 中 |
| 观察池 | 规范线索与否决项，手动触发汇总 | 中 |
| 组织管理 | orgs 增删、llm_policy、workspace_map 维护 | 中 |
| 设置 | embedding/LLM、汇总触发、置信度阈值、日志保留期 | 中 |

置信度阈值可调：初期调高（多确认、少误记），稳定后调低（少打扰）。

## 7. 目录结构

```
memory-assistant/
├─ backend/app/
│  ├─ api/         # FastAPI 路由
│  ├─ core/        # 配置·DB·embedding·LLM客户端(按org分流)
│  ├─ ingest/      # 归属判定·路由·置信度门控·写入器·汇总器·抽取游标·裁决
│  ├─ retrieve/    # 三级作用域召回 + 上下文组装
│  └─ prompts/     # 路由/四动作/汇总（带版本号）
├─ backend/alembic/
├─ backend/tests/
└─ frontend/       # React + Vite
```

## 8. 分阶段路线

- P0 地基：全 schema + Alembic + embedding/LLM 客户端 + orgs/workspace_map
- P1 原始日志：conversation_log 写入（对接 Claude/Qoder 导出）+ 抽取游标 + 归属判定
- P2 写入闭环：路由器 + 置信度门控 + 四动作写入器（先 ADD，再补 UPDATE/DELETE/NOOP）+ pending_items
- P3 读取闭环：三级作用域 hybrid 召回 + 上下文组装（此时已是可用记忆系统）
- P4 活文档：观察池 + 汇总器 + doc_proposals
- P5 Web UI：待办中心 + 待确认 + 待审 diff（三闸口优先），再补其余
- P6 增强：时间衰减/遗忘、occurrence 调权、prompt 迭代+历史重跑、按 org local-only LLM、裁决反哺、日志保留期归档、expired 僵尸项清理

## 9. 关键设计原则

1. 一源三派生 — 原始日志真相源，派生层可重建、抽取可重跑。
2. 组织硬隔离 — user 穿透，org/project 隔离，绝不跨 org 召回（合规底线）。
3. 分类正交 — subject×role 消归属歧义；is/should 消路由歧义；宁窄勿宽防泄露。
4. 拿不准就问 — 低置信度进 pending_items 由人裁决，绝不静默猜宽；裁决反哺降打扰。
5. 文档 AI 起草、人锁定 — authority 分级 + diff 审核 + 否决记忆。
6. 持久状态机 + 幂等 — 三中断点全落表，条件 UPDATE + 水位线 + NOOP 保可恢复不重复。
7. 分桶检索提精度。

## 10. 隐私说明

写入不做脱敏，原始对话明文入库、抽取时原文送 LLM。保密完全依赖按 org 选 LLM：

- personal / 无敏感数据的 org：用 cloud，原文出境到云端 API。
- 涉及公司机密的 org：设 local-only，抽取走本地 LLM，原文不出本机。

配套：日志保留期（过期归档/删除原始日志，派生记忆保留）。

## 11. 开工前需拍板的 3 项

1. 汇总触发：定时／定量／手动？（建议"手动 + 阈值提醒"起步）
2. LLM 供应商：cloud 用哪家？local-only 用什么（Ollama？）？
3. 首批 org：先建哪几个、各自 llm_policy？
