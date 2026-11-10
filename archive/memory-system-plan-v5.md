<!-- ══════════════════════════════════════════════════════════════
     ★ 已归档，不要按本文件实现。
     现行规范：../memory-system-plan.md
     决策历史（含本文件里所有删除线与"已作废"内容）：../decisions-log.md
     ══════════════════════════════════════════════════════════════ -->

# AI 个人助理记忆系统 · 完整方案 v5

> v4 → v5 变更摘要见 §12。本版基于对本机环境的实测（CPU-only、PG 5432 在跑、
> Claude transcript JSONL 实测 4528 行/14MB 单会话）落成，可直接开工。
>
> **配图**（`diagrams/`）：`memory-system-architecture.html` 分层架构图 ·
> `memory-system-sequence.html` 数据流时序图（改 `gen-sequence-diagram.py` 可重新生成）。

## 0. 技术栈与实测约束

| 层 | 选型 | 依据 |
|---|---|---|
| 后端 | Python 3.12 + FastAPI | 本机已装 3.12.10 |
| 数据库 | PostgreSQL + pgvector | 5432 已在监听 |
| pgvector 版本 | **已确认 ≥ 0.8.0** ✅ | 可用 `hnsw.iterative_scan`，见 §5.3 |
| DB 驱动 | psycopg3 + pgvector-python | |
| Embedding | bge-m3（本地，dense 1024；★ sparse 当前取不到，见 §0.1.3 ⑤） | CPU 可跑，中文强。实测 p95 128ms@8线程 |
| Reranker | bge-reranker-v2-m3，**仅写路径** | 无 GPU，读路径跑不动（§5.4） |
| LLM | 命名端点，按**任务**选（首批一个 `dida-gateway`） | 见 §0.1、§7 |
| 队列 | **PG 表 + `FOR UPDATE SKIP LOCKED`** | 不引 Redis/Celery |
| 前端 | React + Vite | |
| 迁移 | Alembic | |

**硬约束：无 GPU（`nvidia-smi` 不存在）。** 一切读路径设计以 CPU 延迟预算为准。

## 0.1 LLM 端点配置

已确定的端点（Anthropic 协议兼容网关，后端是 Qwen）：

| 字段 | 值 |
|---|---|
| `endpoint_key` | `dida-gateway` |
| `base_url` | `http://dragon-open-api.didainternal.com` |
| `model` | `qwen3.8-max-anthropic` |
| `api_protocol` | `anthropic` |

★ token 只走 repo 外的配置，不进代码库、不进 PG、不进本文档：
`~/.mem-assistant/secrets.toml`（chmod 600，全局 gitignore）或环境变量
`MEM_LLM_TOKEN__DIDA_GATEWAY`。`llm_endpoints` 表只存非敏感字段。
落库/打日志前必须 redact——`httpx` 的异常字符串常带请求头，
而 `jobs.last_error` 与 `recall_log` 会在 UI 里显示。
该 token 已在本次对话中出现过，建议轮换一次。

### 0.1.1 协议适配要点（★ 2026-10-22 已实测，非推测）

对 `dida-gateway` 发了两次最小探测请求（单次抽取任务，5 条候选）。结论：

**① tool use 可用，走正道。** 返回 `stop_reason='tool_use'`，`input` 是合法 JSON。
**数组形状的 schema 也没问题**——`{items: [{kind, content, section_key, scope,
keywords, confidence}]}` 一次返回 5 条，`enum` 约束全部遵守。
所以围栏 JSON 回退**降为兜底**（网关升级/换模型时用），不是主路径。

**② thinking 默认开启。★ 结论已翻案：route 任务保持 thinking ON。**

先记一条教训：**单次采样得出的判断被 3+3 采样完全推翻了**。
第一次探测（各 1 次）我看到"关掉 thinking 抽得更准、只是少抽 1 条"，
据此写了"默认关掉 thinking"。跑了 3+3 之后事实是反的——**召回两种模式都是满分，
分类是开 thinking 明显更准**。下面只保留 3+3 的数据。

同一段素材（5 条待抽取信息，ground truth 全是 `observation`），各跑 3 次：

| | thinking ON（默认） | thinking OFF |
|---|---|---|
| 单次延迟 | 25.6s / 24.4s / 23.8s（均 **24.6s**） | 5.3s / 4.7s / 5.5s（均 **5.1s**） |
| output tokens | 963 / 950 / 884（均 **932**） | 340 / 329 / 341（均 **337**） |
| 召回（抽到几条，3 次共 15 条槽） | **15 / 15** | **15 / 15** |
| kind 判对（15 条槽） | **11 / 15 = 73%** | **7 / 15 = 47%** |
| 自报 confidence 区间 | 0.93 – 0.98 | **全部 1.0** |
| `section_key` 三次是否稳定 | **不稳定**（同一条飘 2–3 个栏位） | 稳定 |

分条看 kind（√ = 判成 `observation`）：

| 待抽取的那条信息 | ON | OFF |
|---|---|---|
| 该项目禁止使用 Lombok | 3/3 √ | 3/3 √ |
| 本机 JDK 是 17 | 2/3 | **0/3** |
| Maven 装在 `D:/tools/maven` | 2/3 | **0/3** |
| flightClass 是单词 / cabinGrade 是单字母 | 1/3 | 1/3 |
| 回答用中文、先给结论 | 3/3 √ | 3/3 √ |

**⇒ 定论：`route` 任务用 thinking ON。** 理由：写入链路是全异步的
（PG 原生 job 队列 + lease），24.6s 的延迟落在后台，用户完全无感；
而分类准确率 73% vs 47% 直接决定"规矩会不会被静默丢掉"——这是不可逆的损失。
**读链路（recall）绝不用 thinking**，那边有 400ms 预算。
所以 `thinking` 必须是**端点级 + 任务级可配开关**，不能全局一刀切。

**顺带两个坏消息**（都比延迟严重）：

- **OFF 的 confidence 全是 1.0**，等于没有信息量；ON 至少还有 0.93–0.98 的分布。
  这直接导致 §4.3 的置信度门控失效，见 ③。
- **ON 的 `section_key` 三次跑飘**：Lombok → `dependency`/`code_style`/`dependency`；
  JDK17 → `local_env`/`build_env`/`build_env`；Maven → `local_env`/`local_env`/`build_env`。
  栏位飘 = §3.2.2 的两次向量比对（同栏位查重 + 否决记忆）会在错误的分区里比，
  查不到重复也拦不住已否决项。**⇒ `section_key` 不让 LLM 自由选，
  改成拿候选文本去和 `doc_sections.label + aliases` 做向量匹配后由规则定。**
  （这也是 §3.3 那份 `aliases` 初稿要填好的原因——它是匹配的目标。）

**③ ★ 实测直接印证了 #47/#48：光靠 confidence 门控救不了误判。**
3+3 采样里，15 条 `kind` 判错的样本中，**confidence 全在 0.93–1.0**，
典型几条：

- "该项目禁止使用 Lombok" → 判成 `atomic_fact`, `section_key=dependency`, **confidence 1.0**
- "flightClass 是单词 / cabinGrade 是单字母" → `atomic_fact`, `domain_model`, **confidence 1.0**
- "JDK 是 17" / "Maven 在 D:/tools/maven" → `atomic_fact`, **`section_key=local_env`**, confidence 1.0
- thinking OFF 时更极端：**15 条全部自报 1.0**，其中包括 8 条判错的

这些全是"每次开工都要提醒 AI"的规矩，按 #47 的新判据都该是 `observation`。
**而它们的 confidence 全是满分** —— 0.95 的阈值对高置信错判**完全无效**，
既不会进人工待办，也不会被 §8 的改判率指标抓到（改判率只统计进了待办的）。
这正是"静默污染"的最坏情况，而且**不是偶发，是 3 次里 2 次**。

⇒ **三条硬性推论**（比原来多一条）：
- 路由 prompt 里必须把主判据**逐字写死**（"这条是不是每次开工都要无条件提醒 AI？"）
  并配 few-shot 反例，**不能指望模型自己领会**。`is`/`should` 不能出现在 prompt 里当判据。
  ★ 现有 prompt 已经写死了判据，73% 说明**光写判据不够，必须给例子**——
  尤其是"本机环境类"（JDK / Maven 路径）这种最容易被判成零碎事实的。
- ★ **`observation` 这一类直接取消置信度门控，一律进人工待办。**
  门控是装饰品；而量小到可以承受：72 个会话扫下来只有 24 条规矩信号。
  这与"系统不确定的一律人工审核，尤其是 rule 类"完全一致。
  `atomic_fact` 仍保留 0.85 门控（量大，且判错的代价可被后续检索兜住）。
- §8 的「**规矩漏判率**」人工抽样**不是可选指标，是 P2 验收项**——
  它是唯一能发现 confidence=1.0 错判的手段。

**④ 延迟量级彻底否死同步写路径，并让 §4.4 的按桶批量从"优化"变成"必需"。**
thinking off 单次 4s、on 单次 20s。每条一次 LLM 调用在这个延迟下不可行。

**⑤ 解析器的两个坑**：
- `usage` 里同时有 Anthropic 的 `input_tokens` 和 OpenAI 风格的 `prompt_tokens_details`，
  取前者即可，别因为多字段报错。
- 响应里 `model` 回显为 **`qwen3.8-max`**（去掉了请求里的 `-anthropic` 后缀），
  **不要硬校验 model 名**。
- `content` 数组里 `thinking` 块可能出现在 `tool_use` 块之前，按 `type` 过滤，别取 `[0]`。

**⑥ 仍未测**：上下文窗口上限、并发限流、长 prompt 下的延迟曲线。
`max_input_tokens` 先保守填 32000，客户端按它分片，超限拆批而非截断（截断丢素材）。

**⑦ 解析失败绝不静默丢弃**：Pydantic 校验不过 → 最多 2 次带错误信息重试；
重试耗尽 → 落 `pending_items(stage='route')`，`reason='llm_parse_failed'`，
`suggestion` 存原始响应片段。宁可多打扰，不可丢素材。

**⑧ 安全**：token 走 `Authorization: Bearer`，网关是 `http://` 明文，
token 在内网裸奔。响应体不回显 token，但 `httpx` 异常字符串会带请求头 →
`jobs.last_error` / `recall_log` 落库前必须 redact（§0.1）。

### 0.1.2 confidence 不是概率，必须校准

LLM 自报的 confidence 与实际正确率没有稳定对应关系，**不能直接当阈值用**。
做法：积累一段 `route_decisions` 后，按 confidence 分桶统计改判率，
取"改判率 < 5%"的分位点作为阈值。

```sql
SELECT kind,
       width_bucket(confidence, 0, 1, 10)                     AS bucket,
       count(*)                                               AS n,
       round(avg(overridden::int)::numeric, 3)                AS override_rate
FROM route_decisions
GROUP BY 1, 2 ORDER BY 1, 2;
```

在此之前，阈值先保守设定，且**按 kind 分级**（§4.3）：
`atomic_fact`（零碎事实）0.85、`discard` 0.95。

★ **`observation` 不参与校准——它没有门控**（#51）。3+3 实测显示规矩类的
confidence 对错判毫无区分度（错判也报 0.93–1.0，thinking OFF 时 15 条全报 1.0），
所以这一类 100% 进人工待办，由 §8 的「规矩类人审吞吐」监控积压，
而不是回头调阈值。上面那条 SQL 因此只对 `kind != 'observation'` 的行有意义。
分级的理由是两种误判代价不对称（§4.2）。校准后各 kind 独立重算，
所以 `route_decisions` 必须记 `kind`，分桶统计时按 `(kind, bucket)` 两维分组。

### 0.1.3 本地 embedding 实测（★ 2026-10-22，bge-m3 纯 CPU，P0 最后一项已结）

脚本在 `bench/bench_bge_m3.py`、`bench/bench_query_len.py`、`bench/bench_threads.py`，
可重跑。本机 16 逻辑核 / 62GB 内存（可用 38.6GB）/ 无 GPU。

**① 400ms 预算成立，但前提是必须截断 query。**

| query 长度 | tokens | p50 | p95 |
|---|---|---|---|
| 16 字符 | 8 | 78 ms | 87 ms |
| 64 字符 | 31 | 103 ms | 106 ms |
| **128 字符** | **65** | **166 ms** | **239 ms** |
| 256 字符 | 131 | 239 ms | 265 ms |
| 512 字符 | 259 | 383 ms | **403 ms ← 已击穿** |

固定开销约 **78ms**（8 tokens 也要这么多），之后基本随长度线性增长。
取 **128 字符 / `max_seq_length=128`** 作为读路径上限：p95 239ms，
给 PG 两通道检索 + RRF + HTTP 留出约 160ms（★ P3 是两通道，见 §0.1.3 ⑤）。

★ **不设上限会灾难性击穿**：bge-m3 的 `max_seq_length` 默认是 **8192**，
而用户完全可能粘一整段报错日志。4000 字符 ≈ 2000 tokens，按线性外推要 **2–3 秒**，
hook 会硬生生卡住对话开头。**读路径 encode 必须显式设 `max_seq_length=128`**，
这不是优化，是必需的护栏。

**② ★ 冷启动 15 秒 ⇒ 模型必须常驻，hook 绝不能自己拉起 Python。**

```
import sentence_transformers : 12.5s      ← 主要是 torch
load model（权重已在磁盘）    :  1.3s
```
`mem-hook` CLI 只能是一个**发 HTTP 的瘦客户端**（几十毫秒），
模型活在 FastAPI 常驻进程里。这条把 §1 的"HTTP 核心 + hook"从**设计偏好**
变成了**硬约束**：任何"每次调用起一个 python 进程"的实现都会让对话卡 15 秒。

**③ 常驻内存 1.8GB，本机吃得下。**

| 阶段 | RSS |
|---|---|
| 空进程 | 12 MB |
| import ST | 376 MB |
| load model | 638 MB |
| 预热 encode 后 | **1812 MB** |

62GB 总内存 / 38.6GB 可用 ⇒ 装得下 bge-m3，**也还装得下 bge-reranker-v2-m3**
（写路径用，约 2GB 级）。但两个模型同进程常驻要吃掉约 4GB，
建议 **reranker 放独立 worker 进程**，避免和读路径抢内存带宽。

**④ torch 线程数：默认 8 就是最优，不要调。** 每档独立进程测 60 次：

| 线程 | 读路径 p50 | 读路径 p95 | 写路径 batch32 |
|---|---|---|---|
| 1 | 281 ms | 493 ms | 12.1 s |
| 2 | 219 ms | 358 ms | 6.4 s |
| 4 | 124 ms | 203 ms | 4.4 s |
| **8（默认）** | **88 ms** | **128 ms** | **1.83 s** |
| 16 | 118 ms | 175 ms | 1.93 s |

**16 线程比 8 线程更慢**（线程同步开销盖过并行收益），所以 `OMP_NUM_THREADS`
不要为了"用满机器"去调大。批量吞吐另一条曲线：batch 8 = 38ms/条、
batch 32 = 22ms/条、batch 128 = 21ms/条 ⇒ §4.4 的按桶批量确实有收益，
但 32 以上边际收益已经很小，**桶大小取 32 就够**，不必追求 128。

**⑤ ★ 坏消息：sparse 词法通道在当前依赖下取不到。**

`sentence_transformers 2.7.0` 加载 bge-m3 后模块链只有
`[Transformer, Pooling, Normalize]`，**391 个参数张量里没有任何一个名字含
`sparse` / `colbert` / `linear`** —— ST 把 bge-m3 的 sparse 头和 colbert 头
**静默丢掉了**，`encode()` 也没有 `return_sparse` 参数。`FlagEmbedding` 未安装。

⇒ 三通道检索的**第三条（sparse）目前无法实现**。三个选项：

| 选项 | 代价 | 何时选 |
|---|---|---|
| A. 装 `FlagEmbedding`，用 `BGEM3FlagModel` | 多一个重依赖（拖 torch/transformers 版本），dense 与 sparse 得走两套加载路径 | 评测集证明 keywords[] 通道补不上术语变体时 |
| B. 升 `sentence_transformers` 到 3.x/4.x | 可能破坏现有 API；**且不保证支持 bge-m3 sparse**（这不是 ST 的常规能力） | 不推荐，收益不确定 |
| C. **先按两通道做**（dense + keywords[]），`embedding_sparse` 列继续预留 | 少一条召回通道 | ★ **建议**：P3 本来就没启用 sparse |

**建议 C**，理由：① P3 计划里 sparse 本来就是"列已预留但不启用"；
② `keywords[]`（LLM 抽标识符/专名 + GIN 索引）针对的正是 dense 糊掉
`bookingFlowService` 这类问题，与 sparse 的目标高度重叠；
③ 没有评测集之前无法判断 sparse 值不值一个重依赖——**这正是 §8 评测层要先落地的原因**。
等 P7 有 `eval_recall_cases` 了再 A/B，别现在赌。
★ 但 §5 的三通道表格必须标注这一条，否则实现时会照着写然后卡住。

## 1. 形态定位：在线记忆层

系统是给 **Claude Code / Qoder / 未来任意自定义 agent** 用的**每轮实时记忆层**，
不是离线知识库。这决定了全部延迟与异步边界。

```
┌─────────────── Agent 侧（阻塞，延迟敏感）───────────────┐
│  SessionStart hook  ──► GET /v1/context/bootstrap       │  一次性，稳定层
│  UserPromptSubmit   ──► POST /v1/recall                 │  每轮，动态层
│  MCP tools          ──► note / correct / search         │  模型主动调用
└──────────────────────────┬──────────────────────────────┘
                           │  HTTP（localhost）
┌──────────────────────────▼──────────────────────────────┐
│  FastAPI 核心服务                                        │
│   /recall  ← 零 LLM，纯 embedding+向量，p95 < 400ms      │
│   /ingest  ← 只做落库，不做抽取                          │
└──────────────────────────┬──────────────────────────────┘
                           │  jobs 表（SKIP LOCKED）
┌──────────────────────────▼──────────────────────────────┐
│  异步 worker 池（不阻塞对话，秒~分钟级）                  │
│   transcript watcher → 归属判定 → 路由 → 四动作 → 汇总   │
└─────────────────────────────────────────────────────────┘
```

三条不可妥协的边界：

1. **读路径零 LLM 调用。** 每轮阻塞用户，任何 LLM 调用都会把延迟推到秒级。
   召回 = embedding + 向量/关键词检索 + 规则加权，仅此而已。
2. **写路径全异步。** 对话产生素材只落 `conversation_log`（毫秒级 INSERT），
   抽取/路由/去重全部走 jobs 队列，崩了靠 lease 重投。
3. **归属判定零 LLM。** transcript 每行自带 `cwd`，用规则表匹配（§4.1），
   结果确定、可解释、可人工覆盖，且省掉一次调用。

### 1.1 接入方式（★ 首期只支持 Claude Code）

用户已定：**P1–P6 只对接 Claude Code**，Qoder 与自定义 agent 往后放。
但 `conversation_log.source` 字段与适配器分层保留，日后加 source 不动核心。

| 方式 | 用途 | 首期 |
|---|---|---|
| **Hooks** | 自动注入，主路径 | ✅ Claude Code 的 `SessionStart` / `UserPromptSubmit` / `Stop`。不依赖模型"愿意调工具"，是唯一可靠的自动召回方式 |
| **MCP server** | 模型主动操作 | ✅ `memory_recall` / `memory_note` / `memory_correct` / `memory_search` / `memory_pending`。Claude Code 原生支持 MCP，顺带就让模型能主动纠错 |
| **HTTP** | 核心 + UI | ✅ 上面两者都是它的薄适配器；Web UI 走同一套 |
| Qoder hooks / 其它 agent | — | ⏸ 架构上只是再写一个适配器，暂不做 |

摄取端同样只实现 Claude transcript 解析器（`~/.claude/projects/*/*.jsonl`）。
解析器接口留 `source` 参数，Qoder 的格式日后再补。

Hook 配置（Claude Code，Qoder 同构）：

```jsonc
// ~/.claude/settings.json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command",
      "command": "mem-hook bootstrap"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "mem-hook recall"}]}],
    "Stop": [{"hooks": [{"type": "command",
      "command": "mem-hook flush"}]}]
  }
}
```

`mem-hook` 是个几十行的 Python CLI：读 stdin 的 hook JSON（含 `cwd`/`session_id`/
`transcript_path`/`prompt`）→ 调 localhost HTTP → 把结果打到 stdout 供注入。
**必须自带超时（400ms）+ 失败静默降级**：记忆服务挂了不能拖死对话。

### 1.2 注入分层（控 token）

**记忆都在库里，记忆系统对模型的注入通路只有 hook 一条**（§3.2）。
（`CLAUDE.md` 由 Claude Code 原生注入，属系统外，两者互不感知，见 §3.2.1。）

| 时机 | 注入内容 | 预算 |
|---|---|---|
| SessionStart hook | `living_doc` 中 org 级 + 当前 project 级的 confirmed/pinned（按 `section_key` 分组渲染成 Markdown）+ user 级 memories 中稳定项（profile/preference/pointer） | ~2500 tok |
| UserPromptSubmit hook | 本轮 query 召回的 memories top-K（排除本 session 已注入过的，除非分数显著更高） | ~600 tok |

★ 2500 tok 是**新增**开销（v5 修正：先前误判为"不是新增"）。
记忆系统与 `~/.claude/CLAUDE.md` 无耦合（§3.2.1），两者是并列的注入来源，
token 预算相加。控制增量的唯一手段是**严格裁剪**：
在 `gds-agg-processor-tiket` 里工作时注入 dida 全系规范 + tiket 专属怪癖，
在 personal 项目里则完全不注入 dida 内容。实际增量远小于 2500。

注入格式必须带 `memory_id`，这样模型/用户能引用并纠错：

```
[记忆 #412 · project] gds-agg-processor-tiket 的 flow 服务与 other book service
        存在业务逻辑未同步的历史问题
[记忆 #388 · user/preference] 回答与思考过程一律使用中文
```

## 2. 素材来源与入库过滤（实测结论）

对真实 transcript 采样（4528 行 / 14MB，单会话最大见 101MB）后的体积分布：

| type | 行数 | 体积 | 处置 |
|---|---|---|---|
| `user`(list[tool_result]) | 567 | 大部分 4.3MB | **丢弃**（工具输出） |
| `assistant`(list[tool_use]) | 567 | 大部分 2.4MB | **丢弃**（工具调用参数） |
| `assistant`(list[thinking]) | 454 | — | **筛后保留**（高价值，见下） |
| `assistant`(list[text]) | 142 | — | **保留**（真回复文本） |
| `user`(str) | 46 | — | **保留**（真用户输入，需剔噪） |
| `api-request-blob` / `api-request` | 1135 | 2.5MB | **全丢**（请求快照，纯重复） |
| `attachment` | 624 | 4.1MB | **全丢** |
| `file-history-*` / `mode` / `permission-mode` / `atis-latch` / `cost-state` / `last-prompt` | 余下 | — | **全丢** |

**信噪比约 640 / 4528 ≈ 14%。** 不做这层过滤，`conversation_log` 会被 10 倍冗余
数据撑爆，且抽取成本翻倍。

### 2.1 真实用户输入的判定（实测）

```python
def is_real_user_turn(d) -> bool:
    if d.get("type") != "user" or d.get("isMeta"): return False
    c = d.get("message", {}).get("content")
    if not isinstance(c, str): return False          # list ⇒ tool_result，丢
    bad = ("<command-name>", "<local-command-caveat>",
           "<system-reminder>", "<command-message>")
    return not any(c.lstrip().startswith(b) for b in bad)
```

### 2.2 thinking 块：v4 完全漏掉的高价值素材

实测 454 条 thinking 里全是这类内容：

- `"The user is asking for two things: 1. Verify the passengerId passing bug…"` → 需求结构
- 被否决的方案与理由 → 正是 `polarity='negative'` 观察的来源

但全量送 LLM 成本不可接受。**两级过滤**：

1. 规则预筛：保留含信号词的 thinking（`不要|不用|改为|应该|必须|否决|放弃|偏好|约定|踩坑`
   及英文 `don't|instead|prefer|convention|reject`）
2. 只保留与"真实用户输入"相邻 ±2 轮窗口内的 thinking

预期 454 → 数十条，成本可控且信号密度更高。

### 2.3 两类特殊记录

- **compaction summary**：`"This session is being continued from a previous
  conversation…"` —— 上下文压缩产生的全量摘要，信号极密。存为 `role='summary'`，
  路由时单独处理（若其覆盖的原始消息已入库，则降权避免重复抽取）。
- **极短指令**：实测有 `"修复A2 A3"` 这类。单条无意义，**抽取必须以窗口为单位**
  （user + 后续 assistant text + 筛后 thinking），不能逐条抽。

### 2.4 幂等键

transcript 每条自带 `uuid` → `UNIQUE(source, src_uuid)`。重导入、watcher 重扫、
崩溃重放全部天然幂等，不需要 `msg_index` 计数。

## 3. 数据模型（完整 schema，已修正 v4 缺陷）

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- ══ 组织与归属 ══
-- org 只是**检索分区**：决定"哪些记忆/规范该注入到这个会话"。
-- 不承载任何权限或数据策略语义。
CREATE TABLE orgs (
    org_key          TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    is_personal      BOOLEAN NOT NULL DEFAULT FALSE,
    retention_days   INT,                                    -- NULL = 永久
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ══ LLM 端点 ══
-- 存在的理由是**按任务选模型**（路由要便宜快、汇总要归纳能力强），
-- 不是为了划分信任边界。密钥不在此表，见 §0.1。
CREATE TABLE llm_endpoints (
    endpoint_key  TEXT PRIMARY KEY,      -- 'dida-gateway'/'ollama-local'/...
    base_url      TEXT NOT NULL,
    model         TEXT NOT NULL,
    api_protocol  TEXT NOT NULL DEFAULT 'anthropic'
                  CHECK (api_protocol IN ('anthropic','openai','ollama')),
    max_input_tokens INT,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    note          TEXT
);

CREATE TABLE org_match_rules (                             -- ★ 归属判定：纯规则，零 LLM
    id           BIGSERIAL PRIMARY KEY,
    pattern_type TEXT NOT NULL
                 CHECK (pattern_type IN ('cwd_prefix','git_remote_host','git_remote_path')),
    pattern      TEXT NOT NULL,
    org_key      TEXT NOT NULL REFERENCES orgs(org_key),
    project_key  TEXT,                                     -- NULL = 由 cwd 末段推导
    priority     INT  NOT NULL DEFAULT 100,                -- 小者优先
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (pattern_type, pattern)
);

CREATE TABLE workspace_map (                               -- 归属解析结果缓存
    cwd           TEXT PRIMARY KEY,
    org_key       TEXT REFERENCES orgs(org_key),           -- 规则未命中时填默认桶 'personal'
    project_key   TEXT CHECK (project_key IS NULL OR project_key ~ '^[^/]+/[^/]+$'),
    match_rule_id BIGINT REFERENCES org_match_rules(id),
    resolved_by   TEXT NOT NULL DEFAULT 'rule'
                  CHECK (resolved_by IN ('rule','human','default')),
                  -- ★ 'default' = 没命中任何规则，暂落默认桶，UI 待确认（§4.1）
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    resolved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- ★ project_key 强制 'org/repo' 复合形式：跨 org 撞名在类型层面不可能

-- ══ 层0：原始对话（真相源，只增不改）══
CREATE TABLE conversation_log (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,                             -- 'claude'/'qoder'/...
    src_uuid    TEXT NOT NULL,                             -- transcript 原生 uuid
    session_id  TEXT NOT NULL,
    org_key     TEXT, project_key TEXT,                    -- 取自 workspace_map；project_key 可为 NULL（非仓库目录）
    cwd         TEXT, git_branch  TEXT,                    -- transcript 直接给
    role        TEXT NOT NULL
                CHECK (role IN ('user','assistant','thinking','summary')),
    content     TEXT NOT NULL,
    turn_no     INT,                                       -- 窗口抽取用
    tokens_est  INT,
    msg_ts      TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, src_uuid)                              -- ★ 重导入幂等
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
    PRIMARY KEY (source, session_id)                        -- ★ 修正 v4 跨 source 撞键
);

-- ══ 受控词表：section 不再自由生成 ══
CREATE TABLE doc_sections (
    section_key TEXT PRIMARY KEY,                          -- 'enum_const'/'git_workflow'/...
    label       TEXT NOT NULL,                             -- '枚举与常量'
    aliases     TEXT[] NOT NULL DEFAULT '{}',              -- ★ #50 之后是运行时匹配目标，不是注释
    match_embedding vector(1024),                          -- ★ label + aliases 的 bge-m3 dense 向量
                -- #50：`section_key` 由向量匹配定，不由 LLM 自由选。
                -- 只有 22 行，改词表时重算这几条即可（秒级），不进 job 队列。
    match_text  TEXT,                                      -- 生成上面向量所用的原文，便于校验是否过期
    applies_to  TEXT NOT NULL DEFAULT 'org'                -- ★ 取代原 doc_key 分组
                CHECK (applies_to IN ('org','project','both')),
    org_key     TEXT REFERENCES orgs(org_key),             -- NULL = 通用词表，所有 org 可用
    sort        INT NOT NULL DEFAULT 100,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE
);
-- 22 行的表不值得建 HNSW，直接顺序扫描算余弦（< 1ms）

-- ══ 层1：原子事实 ══
CREATE TABLE memories (
    id           BIGSERIAL PRIMARY KEY,
    scope        TEXT NOT NULL CHECK (scope IN ('user','org','project')),
    org_key      TEXT, project_key TEXT,
    subject      TEXT NOT NULL CHECK (subject IN ('user','project')),
    role         TEXT NOT NULL
                 CHECK (role IN ('profile','preference','pointer','fact')),
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    keywords     TEXT[] NOT NULL DEFAULT '{}',             -- ★ 精确标识符/专名
    why          TEXT,
    embedding        vector(1024),
    embedding_sparse sparsevec(250002),                    -- ★ 预留，P6 回填
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','deleted')),   -- 软删除
    source_msg   BIGINT REFERENCES conversation_log(id),
    mention_count  INT NOT NULL DEFAULT 1,                 -- ★ NOOP 累加，用于调权
    revision_count INT NOT NULL DEFAULT 0,                 -- ★ UPDATE 累加，不调权
    prompt_ver   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT (scope='user' AND subject='project')),       -- ★ 消歧义
    CHECK (scope <> 'org'     OR org_key IS NOT NULL),
    CHECK (scope <> 'project' OR (org_key IS NOT NULL AND project_key IS NOT NULL)),
    UNIQUE (source_msg, content_hash)                       -- ★ 崩溃重放硬幂等
);
CREATE INDEX memories_emb_hnsw ON memories
    USING hnsw (embedding vector_cosine_ops) WHERE status='active';
CREATE INDEX memories_kw_gin ON memories
    USING gin (keywords) WHERE status='active';
CREATE INDEX memories_bucket ON memories
    (scope, org_key, project_key, subject, role) WHERE status='active';
-- ★ partial index：查询必须带 status='active' 才走得到

-- ══ 层2：规范线索沉淀池 ══
CREATE TABLE raw_observations (
    id           BIGSERIAL PRIMARY KEY,
    org_key      TEXT NOT NULL, project_key TEXT,
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
    UNIQUE (source_msg, content_hash)                       -- ★ 重跑不插重复
);
CREATE INDEX obs_hnsw ON raw_observations
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX obs_pending ON raw_observations
    (org_key, section_key, id) WHERE consumed = FALSE;      -- ★ 汇总器主查询
CREATE INDEX obs_negative_hnsw ON raw_observations          -- ★ 否决记忆（§3.2.2）
    USING hnsw (embedding vector_cosine_ops)
    WHERE polarity = 'negative';
    -- 比对② 只在否决集里找相似项；独立部分索引避免扫全表 HNSW

-- ══ 层3：活文档 + 历史（★ 库即终点，不发布成文件，见 §3.2）══
CREATE TABLE living_doc (
    id          BIGSERIAL PRIMARY KEY,
    org_key     TEXT NOT NULL REFERENCES orgs(org_key),
    project_key TEXT,                   -- NULL = org 级；非空 = 该 repo 专属（供应商怪癖）
    section_key TEXT NOT NULL REFERENCES doc_sections(section_key),
    content     TEXT NOT NULL,
    authority   TEXT NOT NULL DEFAULT 'draft'
                CHECK (authority IN ('draft','confirmed','pinned')),
    origin      TEXT NOT NULL DEFAULT 'summarized'
                CHECK (origin IN ('summarized','human')),
                -- ★ 无 'imported_from_file'：系统不导入任何外部规则文件（§3.2.1）
    origin_ref  TEXT,                   -- human 时可记 UI 编辑备注；无文件溯源语义
    source_ids  BIGINT[] NOT NULL DEFAULT '{}',   -- 支撑它的 observation id
    version     INT NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- ★ project_key 可空，PG 默认 NULL 互不相等 ⇒ 用 COALESCE 表达式索引保证唯一
CREATE UNIQUE INDEX living_doc_uniq
    ON living_doc (org_key, section_key, COALESCE(project_key, ''));
CREATE INDEX living_doc_inject                                  -- SessionStart 注入主查询
    ON living_doc (org_key, project_key, authority, section_key);
CREATE TABLE living_doc_history (                           -- ★ v4 缺失
    id            BIGSERIAL PRIMARY KEY,
    living_doc_id BIGINT NOT NULL REFERENCES living_doc(id) ON DELETE CASCADE,
    version       INT NOT NULL,
    old_content   TEXT, new_content TEXT NOT NULL,
    old_authority TEXT, new_authority TEXT NOT NULL,
    proposal_id   BIGINT,
    changed_by    TEXT NOT NULL CHECK (changed_by IN ('human','summarizer','replay')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON living_doc_history (living_doc_id, version);

CREATE TABLE doc_proposals (
    id          BIGSERIAL PRIMARY KEY,
    org_key     TEXT NOT NULL,
    project_key TEXT,                                       -- 与 living_doc 对齐
    section_key TEXT NOT NULL,
    old_content TEXT, new_content TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','accepted','rejected','expired')),
    job_id      BIGINT,                                     -- ★ accepted 后的落库 job
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);
CREATE INDEX ON doc_proposals (status, created_at) WHERE status='pending';

-- ══ 待人工裁决（状态机补全）══
CREATE TABLE pending_items (
    id          BIGSERIAL PRIMARY KEY,
    org_key     TEXT, project_key TEXT,
    stage       TEXT NOT NULL
                CHECK (stage IN ('route','dedup','section')),
                -- ★ 无 'attribution'：归属未命中不再阻塞抽取，改走
                --   workspace_map(resolved_by='default') + UI 归属管理页（§4.1）
    raw_content TEXT NOT NULL,
    source_msg  BIGINT,
    suggestion  JSONB,
    reason      TEXT,
    prompt_ver  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'             -- ★ 补 writing/done/failed
                CHECK (status IN ('pending','approved','writing',
                                  'done','rejected','failed','expired')),
    resolution  JSONB,                                      -- 人定的 kind/scope/...
    job_id      BIGINT,
    last_error  TEXT,
    resolved_by TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ
);
CREATE INDEX ON pending_items (status, created_at)
    WHERE status IN ('pending','writing','failed');

-- ══ 任务队列（PG 原生，无 Redis）══
CREATE TABLE jobs (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,     -- ingest/extract/route/write_facts/
                                    -- summarize/apply_decision/replay/retention
    payload      JSONB NOT NULL,
    org_key      TEXT,              -- 入队时固化，用于统计与回溯纠正（不参与端点选择，§7.1）
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
    WHERE status IN ('pending','running');                   -- ★ 同任务不重复排
CREATE INDEX jobs_claim ON jobs (status, id)
    WHERE status IN ('pending','running');

-- ══ 标注资产（不属派生层，删库重建不丢）══
CREATE TABLE route_decisions (
    id           BIGSERIAL PRIMARY KEY,
    source_msg   BIGINT,
    raw_snippet  TEXT NOT NULL,
    prompt_ver   TEXT NOT NULL,
    kind         TEXT NOT NULL
                 CHECK (kind IN ('observation','atomic_fact','discard')),
                 -- ★ 从 model_output 里冗余出来：置信度阈值按 kind 分级（§4.3），
                 --   校准时要按 (kind, bucket) 两维分桶统计改判率，不能只按 prompt_ver
    confidence   NUMERIC(4,3) NOT NULL,
    model_output JSONB NOT NULL,
    human_output JSONB,                                      -- NULL = 模型判被接受
    overridden   BOOLEAN NOT NULL DEFAULT FALSE,
    org_key      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON route_decisions (prompt_ver, kind, overridden);

-- ══ 召回审计 + 评测 ══
CREATE TABLE recall_log (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT, session_id TEXT NOT NULL, turn_no INT,
    query_text   TEXT NOT NULL,
    org_key      TEXT, project_key TEXT,
    hits         JSONB NOT NULL,          -- [{memory_id,score,channel,rank}]
    injected_ids BIGINT[] NOT NULL DEFAULT '{}',
    latency_ms   INT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON recall_log (session_id, turn_no);
CREATE INDEX ON recall_log (created_at);

CREATE TABLE memory_feedback (
    id           BIGSERIAL PRIMARY KEY,
    memory_id    BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    recall_log_id BIGINT REFERENCES recall_log(id),
    kind         TEXT NOT NULL CHECK (kind IN ('corrected','useless','useful')),
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON memory_feedback (memory_id);

CREATE TABLE eval_recall_cases (
    id BIGSERIAL PRIMARY KEY,
    label TEXT, query_text TEXT NOT NULL,
    org_key TEXT, project_key TEXT,
    expected_ids BIGINT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE eval_runs (
    id BIGSERIAL PRIMARY KEY,
    run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    prompt_ver TEXT, config JSONB,
    hit_at_5 NUMERIC, mrr NUMERIC, suspend_rate NUMERIC,
    override_rate NUMERIC, p95_ms INT, notes TEXT
);

-- ══ shadow 重跑 ══
CREATE TABLE replay_runs (
    id BIGSERIAL PRIMARY KEY,
    prompt_ver TEXT NOT NULL,
    scope_filter JSONB,
    status TEXT NOT NULL DEFAULT 'running'
           CHECK (status IN ('running','awaiting_review','applied','discarded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE replay_actions (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES replay_runs(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('ADD','UPDATE','DELETE','NOOP')),
    target_memory_id BIGINT,
    proposed_content TEXT,
    proposed_meta JSONB,
    applied BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ON replay_actions (run_id, applied);

-- ══ 审计 ══
CREATE TABLE memory_events (
    id          BIGSERIAL PRIMARY KEY,
    memory_id   BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    action      TEXT NOT NULL CHECK (action IN ('ADD','UPDATE','DELETE','NOOP','RESTORE')),
    old_content TEXT, new_content TEXT,
    source_msg  BIGINT,
    actor       TEXT NOT NULL DEFAULT 'system',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON memory_events (memory_id, id);              -- ★ v4 缺失，UI 看历史会全表扫
```

### 3.1 分类体系（消歧义后）

**scope（可见性）**：`user` 穿透 / `org` 组织内 / `project` 单仓库。

**subject × role**：

| role | 含义 | 半衰期 | 归属 |
|---|---|---|---|
| `profile` | 稳定身份属性（"用户是后端工程师"） | 365d | memories |
| `preference` | **个人偏好/要求**（"回答用中文"、"不要 mock 数据库"） | 365d | memories |
| `pointer` | 外部资源指向（"bug 在 Linear INGEST"） | 180d | memories |
| `fact` | 事实（"项目用 React 18"） | 90d | memories |
| ~~`rule`~~ | **已删除** | — | — |

★ **v4 的 `role='rule'` 是双写源头，本版删除。** 边界改为：

- 关于**用户个人**的"应该怎么做" → `memories(role='preference')`
- 关于 **org/project 的团队规范** → `raw_observations` → `living_doc`

两者不重叠，召回时不会出现"同一规范两处存、谁权威未定义"。

CHECK 约束保证 `scope='user'` 时 `subject` 只能是 `user`
（`scope=project, subject=user` 合法：如"用户在这个项目里坚持用 pytest"）。

## 3.2 活文档的唯一真相源是数据库（★ 已修正）

> **所有记忆一律存 PG，`living_doc` 不发布成文件。**
> 规范的注入通路只有一条：SessionStart hook 从库里读。

前一版我曾设计"living_doc 发布回 `CLAUDE.md`、由 Claude Code 原生注入"，
理由是复用已有注入机制、避免双写。**这个设计是错的**，因为它把文件变成了
第二真相源：你手改文件 vs 库里 draft 改内容，两边都会动，于是不得不引入
`published_snapshot`、漂移检测、三方 reconcile 提案——
**那一整层复杂度不是需求，是错误设计的症状。** 删掉。

现在的模型：

```
observations ──汇总器──► living_doc(draft)
                            │
                     doc_proposals（人审 diff）
                            │ accepted
                            ▼
                  living_doc(confirmed / pinned)     ← ★ 终点，就在库里
                            │
              SessionStart hook 读取并注入（唯一通路）
```

好处：单一真相源、可查询、带 scope/authority/provenance/why、
版本历史在 `living_doc_history`，且**没有同步问题**。

### 3.2.1 与 `~/.claude/CLAUDE.md` 无任何耦合 ★ 已定

> **记忆系统不读它、不导入它、不修改它、不感知它是否存在。**

`CLAUDE.md` 是你手工维护的文件，由 Claude Code 原生注入，继续照旧。
`living_doc` 只装**从对话里学到的东西**，从空库起步。
两者是并列的两套注入来源，系统层面互不感知。

这条约束的价值是**让记忆系统自包含**：不依赖某个 agent 的某个特定配置文件，
换成 Qoder 或自定义 agent、换一个没有 CLAUDE.md 的用户，同一套代码照样跑。
一旦引入"解析规则文件"的导入器，系统就绑定死了 Claude Code 的文件约定。

先前定的"方案 A：导入进库 + 精简 CLAUDE.md"已作废——它让系统承担了两个
不属于它的职责（解析外部文件格式、改写用户文件），换来的只是省一点重复。

#### 代价与对策

| 代价 | 对策 |
|---|---|
| **注入 token 变成相加**：CLAUDE.md（~2500 tok，全局注入）+ living_doc（按 org/project 裁剪）。§1.2 原先"不是新增开销"的说法不再成立 | living_doc 严格按 org/project 裁剪，personal 会话里不注入 dida 内容，实际增量远小于 2500 |
| **冷启动噪声期**：AI 会把你 CLAUDE.md 里已有的规则当新发现提议一遍 | ★ **否决记忆**机制（§3.2.2）：你否决一次，之后不再提 |
| 两处内容可能长期重叠 | 裁决权在你：提案时你一眼能看出"这条我已经写在 CLAUDE.md 里了"→ 否决。系统不需要知道那个文件里有什么 |

### 3.2.2 冷启动：空库 + 否决记忆（★ 取代原导入器）

不导入任何文件，从空库起步。防止"反复提议同一条"靠**否决记忆**，
而不是靠比对某个外部文件：

```
你在 UI 否决一个 doc_proposal
  → 写 raw_observations(polarity='negative', section_key=同提案)
  → content = 被否决的规范文本

此后每次 observation 落库前，做两次向量比对：
  ① vs living_doc 同 section 的现有内容        → 相似即 NOOP（已有，不必再提）
  ② vs raw_observations(polarity='negative')   → 相似即 discard(reason='rejected_before')
```

② 就是"你已经说过不要了"的记忆。它让系统**从你的否决里学会你的边界**，
而不需要去读你把规则写在哪个文件里。同一机制也覆盖"AI 提议的规范你不认可"
这类普通否决，不是专为 CLAUDE.md 设计的特例。

收敛速度取决于你否决几次。实测素材量下（72 会话仅 24 条含规范信号），
这个噪声期很短。

★ `living_doc.origin` 因此只剩 `summarized` 与 `human` 两个取值，
`imported_from_file` 删掉（§3 schema 同步改）。

### 3.2.3 服务不可用时的韧性（替代"发布成文件"的唯一正当理由）

"发布成文件"剩下的唯一好处是：记忆服务挂了，规则还在。
用一个**哑缓存**解决，而不是引入第二真相源：

```
SessionStart hook 成功拿到 bootstrap payload
   → 顺手写到 ~/.mem-assistant/cache/bootstrap-<org>-<project>.md
服务不可达（超时/连接失败）
   → hook 直接 cat 这个缓存文件注入，并在开头标注
     "⚠ 记忆服务离线，以下为上次缓存的规则快照（可能过期）"
```

缓存是**只读派生物**，永不回写库，因此不构成真相源。

### 3.2.4 边界：什么进 living_doc，什么进 memories

| | living_doc | memories |
|---|---|---|
| 生效方式 | **稳定、每会话无条件注入** | **情境性、按 query 命中才注入** |
| 粒度 | org 级 / project 级的成体系规范 | 单条原子事实 |
| 典型内容 | 编码规范、框架约定、分支流程、个人回答风格 | 某次排查的结论、某个字段的历史坑、外部资源指针 |
| 例子 | "禁止使用 Lombok" | "gds-agg-processor-tiket 的 flow 服务与 other book service 存在业务逻辑未同步" |

★ 这修正了 §3.1 里 `role='preference'` 的定位：**全局、每会话生效的偏好属于
living_doc**（如"先给结论再展开"）；`memories(role='preference')` 只装
**情境性**偏好（如"在 X 项目里坚持用 pytest"）。两者不重叠。

### 3.2.5 文件内容属于 derivable

`api-request-blob` 里含 Claude Code 注入的 CLAUDE.md 全文，§2 已整体丢弃该类记录，
所以不会污染——**系统不需要读那个文件**（§3.2.1）。

但对话里仍会不断复述已成文的规则（AI 引用、你重申）。兜底不是"去比对文件"，
而是 §3.2.2 的两次向量比对：

```
observation 落库前：
  ① vs living_doc 同 section 现有内容        → 相似即 NOOP
  ② vs raw_observations(polarity='negative') → 相似即 discard(reason='rejected_before')
```

① 覆盖"这条库里已经有了"，② 覆盖"这条你已经说过不要了"。
两者都只查库，不碰文件系统。

## 3.3 `doc_sections` 初始词表（★ 基于你的真实规则文件与会话证据整理）

不是通用模板——每一条都能对应到你已有的工程共识（写在 `~/.claude/CLAUDE.md` 里的规则），
或你历史会话里的真实发言。`aliases` 供路由器匹配用。

★ **"现有证据"列是本文档的设计期依据，不是运行时输入。**
词表由我读你的规则文件后人工整理定稿；系统跑起来后只按 `section_key` 分类，
**从不读那个文件**（§3.2.1）。改词表 = 改 `doc_sections` 表，不是改文件。

分组维度只有两个：**`org_key`**（属于谁）+ **`applies_to`**（org 级还是 project 级）。
不需要第三个分组名——`org + project + section` 已能唯一定位任何一条规范。

★ **v5 定稿：22 个栏位**（原 25 个，砍掉 3 个只有 1 条规则的薄栏位，见本节末「合并记录」）。
`aliases` 是我按你的会话用语拟的**初稿**，你改起来比从零填快——
直接在 UI「词表」页改，或告诉我改哪几个。

### ① `org_key='personal'`, `applies_to='org'`（5 个）

| section_key | label | 收什么 | aliases（初稿） |
|---|---|---|---|
| `local_env` | 本机环境与工具 | OS/Shell/JDK/Python/Node/Git 版本、Maven 实际路径、SASE 常驻进程 | JDK、Java 版本、Maven、mvn、Python、Node、环境变量、PATH、本机、我电脑上、SASE |
| `env_pitfalls` | 环境已知坑与规避 | 模型分类器超时的根因与四步应对、写操作失败处置 | 已知坑、踩坑、超时、老是失败、写操作失败、分类器超时、绕一下、workaround、auto 模式 |
| `answer_style` | 回答与表达风格 | 先结论后展开、中文作答、代码风格偏好（小函数<40行） | 中文、先给结论、结论前置、别说太多、简洁、表达方式、小函数、函数别太长 |
| `ai_working_rules` | 给 AI 的工作纪律 | 大日志不许全读只读相关段、失败不无限重试、写后校验落盘 | 日志不要全读、别一直重试、重试次数、写完要校验、落盘检查、工作纪律 |
| `external_communication` | 对外沟通 | 给供应商技术支持写英文信的语气要求 | 写信、邮件、英文、给对方、供应商技术支持、语气、别让人觉得是我们的问题 |

### ② `org_key='dida'`, `applies_to='org'`（project_key=NULL，全系适用，13 个）

| section_key | label | 收什么 | aliases（初稿） |
|---|---|---|---|
| `dependency` | 依赖与第三方库 | 禁用/允许引入的库 | 依赖、pom、三方库、第三方库、Lombok、引包、能不能用这个库 |
| `naming_docs` | 命名与注释 | 类/方法/常量命名、Javadoc `@author`/`@since` | 命名、类名、方法名、常量名、注释、Javadoc、@author、@since、必须大写吗 |
| `code_style` | 代码风格与格式 | P3C 行宽120/4空格/K&R、import 规范、函数长度；**资源关闭**（`feign.Response` 必须 try-with-resources）；**接口与方法签名**（转换器只传需要的参数、包装类型、内部 null 安全） | 代码风格、格式、行宽、缩进、4空格、K&R、import、全路径、函数长度、try-with-resources、feign.Response、资源没关、接口签名、方法参数、包装类型、null 安全、转换器传参 |
| `enum_const` | 枚举与常量 | 枚举优先禁魔法值、禁重复枚举、双向查找 | 枚举、enum、魔法值、硬编码、常量、双向查找、枚举映射 |
| `error_handling` | 异常与失败处理 | fail-fast 不盲目 fallback、不对外泄内部异常 | 异常、报错、fail-fast、fallback、兜底、别把内部异常返回出去、吞异常 |
| `layering` | 分层与模块依赖 | `persist → util` 单向、Converter 禁引用实体 | 分层、模块依赖、persist、util、Converter、单向、循环依赖、能不能引用 |
| `framework` | 框架约定 | ServiceImpl 继承 Abstract 基类 + `@Override` 钩子、logger 自声明 | 框架、ServiceImpl、Abstract 基类、继承、@Override、钩子、logger 声明、公司框架 |
| `domain_model` | 领域模型与映射 | `flightClass`(单词) vs `cabinGrade`(单字母)、航段/PNR/journeys 语义 | 领域模型、舱位、cabin、cabinGrade、flightClass、航段、leg、journeys、PNR、PCC、FFN、改签、change、验价、verify、婴儿票、infant、couponStatus、字段语义、这两个别搞混 |
| `supplier_protocol` | 供应商对接通用协议 | `GdsStatus`(Integer) vs 供应商 code(String) 禁混用、BaseResponse 构造、禁伪造 code | 供应商协议、GdsStatus、code、BaseResponse、响应码、状态码、透传、别伪造 code、返回码 |
| `logging` | 日志与可观测 | 高并发 search 用 DEBUG + `isDebugEnabled` 包裹、verify/book 用 INFO 成对报文、飞书通知 | 日志、log、DEBUG、INFO、isDebugEnabled、报文、成对报文、飞书、通知、可观测、打日志 |
| `build_env` | 构建与联调 | `-pl <m> -am`、**不能加 `-o`**、groupId `com.didatravel`、processor 改动需 `mvn install` + 升 gds-agg 版本属性 | 构建、编译、打包、mvn、maven、`-pl`、`-am`、离线、`-o`、groupId、com.didatravel、install、版本属性、gds-agg、联调 |
| `git_workflow` | 分支与提交 | **新分支必须从 dev 最新代码创建**、未获许可不提交、review 对比 dev | 分支、branch、dev、提交、commit、push、先不提交、review、合并、从 dev 拉、新分支 |
| `process` | 协作流程与判断方法 | 实现与 plan 不一致以 plan 为准、偏离需显式记录；**调研判断方法**（判断字段冗余必查框架层消费者、最终判据看运行时 Nacos 配置而非代码） | 流程、协作、plan、以 plan 为准、偏离要说明、调研、字段冗余、框架层有没有用、Nacos、运行时配置、最终看配置、别只看代码 |

### ③ `org_key='dida'`, `applies_to='project'`（project_key 必填，一供应商一份）

`project_key` 直接取 `workspace_map` 的值，如 `dida/gds-agg-processor-tiket`。
25+ 个供应商 repo **不需要预建任何行**——`living_doc` 的行在有观察积累时才产生。

| section_key | label | 收什么 | aliases（初稿） |
|---|---|---|---|
| `field_format` | 字段格式与取值约束 | 供应商对字段格式/语言/前缀的硬要求 | 字段格式、取值、长度、前缀、去前缀、`86-`、手机号、mobile、不能用中文、英文名、firstNameEn、lastNameEn、contact、联系人、枚举值、大小写、必填字段、传空 |
| `business_boundary` | 业务边界与否决项 | 明确不做的业务、必须携带的特殊值 | 不支持、不做、业务层面、婴儿、infant、儿童、宠物、团体、免费行李、baggage、id=0、prices=0、也要带上、必须携带、特殊值、边界、不覆盖 |
| `flow_gotcha` | 流程与时序陷阱 | 改签/退票/验价等流程中的易错点、状态码判定 | 改签、change、executeChange、退票、refund、验价、verify、verifyChangeDoneByRetrievePnr、时序、先后顺序、易错、坑、状态码、`0000`、GdsStatus、判成功、要校验行程、RetrievePnr |
| `contact_ops` | 对接与运维事项 | 对方技术支持沟通结论、账号/PCC 配置、联调约定 | 对接、联调、对方、技术支持、供应商说、账号、密码、PCC、pcc、office、couponStatus、测试环境、生产环境、白名单、开通、配额、限流、联系人邮箱 |

### 词表使用规则

1. ★ **`section_key` 不由 LLM 自由选，改用向量匹配定。**
   拿候选文本的 embedding 去和 `doc_sections.label + aliases` 的 embedding 比，
   取 top-1；分数低于阈值或 top-1/top-2 差距过小 → `pending_items(stage='section')`。
   **为什么改**：3+3 实测里 LLM 自选栏位三次跑飘
   （Lombok → `dependency`/`code_style`/`dependency`），
   栏位一飘，§3.2.2 的两次向量比对就在错分区里比，查重和否决记忆双双失效。
   向量匹配是确定的、可解释的，且 §3.3 的 `aliases` 正好就是它的匹配目标。
   词表增删也只需重算这几个短文本的 embedding，不用改 prompt、不用重新校准。
2. `applies_to='project'` 的 section 若没解析出 project_key →
   同样进 pending，**不许降级存成 org 级**（供应商怪癖污染全系规范）。
   project_key 走 §4.1 的纯规则归属判定（`cwd` → `workspace_map`），不用 LLM。
3. 初期词表偏细没关系——**空 section 不产生成本**（注入时跳过），
   但错分类会污染活文档。宁可多几个待裁决。
   ★ 但栏位飘的代价现在变实了（见规则 1），所以词表**不宜再细下去**：
   22 个是上限，后续只允许合并、不允许新增，除非 pending 里同一主题反复出现。
4. 词表本身进 `doc_sections` 表并可在 UI 维护（增删 label/aliases/sort）。
   ★ `aliases` 我已经按你的会话用语填了**初稿**（Lombok、pom、cabinGrade、
   PNR、PCC、改签/change、验价/verify、婴儿票/infant、couponStatus、`-pl`/`-am`/`-o` 等），
   你只需要**改错和补漏**，不用从零填。
   改完 UI 里存一下即可生效——因为规则 1 之后 aliases 是匹配目标，不是给人看的注释。

### 合并记录（25 → 22，★ v5 定稿）

砍掉的都是**只有 1 条规则**的薄栏位。理由：栏位一多，向量匹配的 top-1/top-2
差距就变小，规则 1 的判定会频繁落进 pending；空栏位虽然不花注入成本，
但会花**你的裁决成本**。

| 原栏位 | 并入 | 那 1 条规则 | 为什么并得进去 |
|---|---|---|---|
| `resource_concurrency` | `code_style` | `feign.Response` 必须 try-with-resources | 本质是"写代码时的硬格式要求"，和 P3C 行宽/import 规范同类；并发相关的真正规则（线程池、锁）目前一条都没有 |
| `api_design` | `code_style` | 转换器只传需要的参数、包装类型、内部 null 安全 | 说的是方法签名怎么写，不是对外接口契约；真有 REST 契约规则时再拆出来 |
| `investigation` | `process` | 判断字段冗余必查框架层消费者、最终判据看运行时 Nacos 配置而非代码 | 这是"怎么下判断"的方法论，和"以 plan 为准、偏离要说明"同属协作流程 |

**没砍但要注意的**：`local_env` 与 `build_env` 在实测里被 LLM 混着用
（JDK 17 两次给 `local_env` 一次给 `build_env`）。这两个栏位**语义确实该分开**
（`local_env` = 这台机器上装了什么；`build_env` = 这个项目怎么构建），
所以保留两个，靠 `aliases` 把它们掰开：
`local_env` 收"装在/JDK/Python/路径/在 D 盘"，`build_env` 收"mvn/打包/编译/版本属性"。
★ 这是 aliases 初稿里最该你亲自过一眼的两行。

## 4. 写入链路

### 4.1 归属判定：纯规则，零 LLM ★

transcript 每行自带 `cwd` 与 `gitBranch`，所以**根本不需要用 LLM 判归属**——
省一次调用，且结果是确定的、可解释、可人工覆盖。

归属的作用是**检索分区**：让 `gds-agg-processor-tiket` 的会话只注入 dida 规范 +
tiket 怪癖，而不被 personal 的偏好和别的供应商的坑干扰（§1.2）。
判错的后果是"注入了不相干的记忆"，属于质量问题，不是事故——所以**不阻塞抽取**。

```
1. workspace_map 命中 cwd（enabled）           → 直接用
2. org_match_rules 按 priority 匹配：
     git_remote_path   → '62bc118a.../Flight/*'           → dida    （★ 权威信号）
     cwd_prefix        → 'C:/Users/x/IdeaProjects/dida'   → dida
   命中 → 写回 workspace_map 缓存
3. 都不命中 → 暂记 org_key = 'personal'（默认桶）
              + workspace_map.resolved_by = 'default'
              + 抽取照常进行，不阻塞
              + UI「归属管理」页列出这些目录供你一次性确认
                （确认后改 workspace_map，已入库的记忆按 source_msg 回溯改 org_key）
```

`project_key` 由 `git remote` 的 repo 名或 cwd 末段推导，强制拼成 `org_key/repo`。

★ 默认落 `personal` 而不是留 NULL：NULL 会让 §5.1 的可见性谓词无从匹配，
这些记忆永远召不回来，等于白抽。`personal` 是"最不相干"的桶，
误判的代价只是多注入几条个人偏好，且可事后批量纠正。

#### 实测：dida 的真实归属信号

```
git remote: https://codeup.aliyun.com/62bc118a3e81781f3ad1d681/Flight/gds/agg/gds-agg-processor-tiket.git
                                 └────────── dida 在 codeup 上的组织 ID ──────────┘
仓库布局:   ~/IdeaProjects/dida/gds-agg-processor-<供应商>   （25+ 个，一供应商一 repo）
                                 gds-agg / gds-sabre / flight-* / flight-db ...
其它前缀:   ~/dida/...      （工具与前端）
            ~/own/...       （★ 混合：既有 personal 也有 dida 相关，如 own/dida-flight）
            D:/ai/...       （personal）
```

★ **`git_remote_host` 单独匹配是错的**：`codeup.aliyun.com` 是公共代码托管
（等同 github.com），别的公司也在上面。必须匹配到**组织路径**
`62bc118a3e81781f3ad1d681` 才能确定归属。因此 `git_remote_path` 的优先级
**高于** `cwd_prefix`——remote 才是所有权的权威信号，cwd 只是文件放在哪。

（边界情形：从公司仓库 fork 出来的个人项目会保留原 remote，会被判成 dida。
这类仓库少，遇到时用 `workspace_map.resolved_by='human'` 覆盖即可，
人工覆盖优先于规则。）

#### 初始 seed 数据

```sql
-- 端点（首批一个即可；日后要按任务分模型再补行）
INSERT INTO llm_endpoints (endpoint_key, base_url, model, api_protocol)
VALUES ('dida-gateway', 'http://dragon-open-api.didainternal.com',
        'qwen3.8-max-anthropic', 'anthropic');

-- 组织（★ 首批只建两个，纯检索分区）
INSERT INTO orgs (org_key, name, is_personal)
VALUES ('personal', '个人',      TRUE),
       ('dida',     'dida 机票', FALSE);

-- 归属规则（priority 小者优先）
INSERT INTO org_match_rules (pattern_type, pattern, org_key, project_key, priority) VALUES
 ('git_remote_path', '62bc118a3e81781f3ad1d681/%', 'dida', NULL, 10),  -- ★ 权威信号
 ('cwd_prefix', 'C:/Users/yong.xu/IdeaProjects/dida/', 'dida', NULL, 50),
 ('cwd_prefix', 'C:/Users/yong.xu/dida/',              'dida', NULL, 50),
 ('cwd_prefix', 'C:/Users/yong.xu/own/',          'personal', NULL, 80),
 ('cwd_prefix', 'D:/ai/',                         'personal', NULL, 80);
-- project_key 留 NULL：由 remote 末段推导，如 'dida/gds-agg-processor-tiket'
```

★ `~/own/` 是混合目录（既有 personal 也有 `own/dida-flight` 这类 dida 相关内容），
所以它的 `cwd_prefix` 优先级设为 80（低于 git 规则的 10）——
真实 dida 仓库会先被 remote 规则命中，只有非仓库目录才落到 personal。

★ **未命中任何规则 ⇒ 默认落 `personal` 并继续抽取**，同时在「归属管理」页
列出该目录待你确认。判错只是注入了不相干的记忆，可事后按 `source_msg` 批量回溯
改 `org_key`；留 NULL 反而会让这些记忆永远召不回来。

### 4.2 路由器（按窗口，1 次 LLM）

以"真实用户输入"为锚点切窗口：`user + 后续 assistant text + 筛后 thinking`。
按任务选 LLM 端点（§7），带 `prompt_ver`。

```json
[{"kind":"atomic_fact","scope":"project","subject":"project","role":"fact",
  "content":"gds-agg-processor-tiket 的 flow 服务与 other book service 存在业务逻辑未同步",
  "keywords":["gds-agg-processor-tiket","bookingFlowService"],
  "why":null,"confidence":0.93},
 {"kind":"observation","polarity":"negative",
  "content":"当前系统业务层面不支持婴儿票",
  "keywords":["婴儿票","infant"],"confidence":0.41},
 {"kind":"discard","reason":"derivable","confidence":0.99}]
```

★ **模型输出里没有 `section_key` 了**（#50）。栏位改成模型返回后由向量匹配定：
拿 `content` 的 embedding 去比 `doc_sections.label + aliases`，取 top-1。
理由是实测 LLM 自选栏位三次跑飘，而栏位一飘，下面那条"两次向量比对"就在错分区里做。

硬规则：
- ★ **主判据：这条信息是不是"每次开工都要无条件提醒 AI"？**
  - **是** → `observation`（进规范线索池 → 汇总成 living_doc → hook 每会话注入）
  - **否，只在问到相关事情时才用得上** → `atomic_fact`（进 memories，按 query 召回）
  - **情境性个人偏好**（"在这个项目里坚持用 pytest"）→ `atomic_fact` + `role='preference'`
  - **全局每会话生效的偏好**（"先给结论再展开"）→ `observation`（§3.2.4）

  `is`/`should` 口气**不再是判据**（#47 曾把它降为"挑 section_key 的次判据"，
  但 #50 之后栏位由向量匹配定，这个次判据也没用了）。

  **为什么改**：原判据只看口气，会把"JDK 是 17""Maven 装在这个路径""模型分类器
  超时的四步应对"这类 `is` 形内容全送进 memories，导致 `local_env` / `env_pitfalls`
  两栏永远填不满——而它们恰恰是每次开工都要提醒 AI 的。§3.2.1 与 CLAUDE.md 解耦后
  没有导入器兜底，这个洞会一直空着（#47）。
  ★ **实测这条判据光写进 prompt 不够**：3+3 采样下 thinking ON 也只有 73% 判对，
  错得最多的正是"本机 JDK 是 17""Maven 在 D:/tools/maven"这类环境项（0–2/3）。
  ⇒ prompt 必须配 **few-shot 反例**，明确给出"环境项也算 observation"的正例（#52）。
- ★ **`observation` 一律进人工待办，不设置信度门控**（§4.3，#51）。
  理由是两种错误的代价不对称：
  - 误判成 `observation`：只是进线索池，后面还要过汇总 + 你审 diff（§4.6）两道，
    **错了有机会拦下来**；
  - 误判成 `atomic_fact`：一条本该每次注入的规矩被当成零碎事实存进小本子，
    **永远不会被提议成规矩、永远不会注入**——静默丢失，且没有任何环节能发现。
  而 confidence 这个信号实测对错判**毫无区分度**（错判也报 0.93–1.0），门控形同虚设。
- ★ `section_key` 由向量匹配定（见上），匹配不出或 top-1/top-2 差距过小
  → `stage='section'` 进 pending，由人定名并决定是否新增栏位。
  **词表 22 个是上限，后续只合并不新增**（§3.3 规则 3）。
- ★ **落库前两次向量比对**（§3.2.2，只查库，不读任何文件）：
  ① vs `living_doc` 同 section 现有内容 → 相似度 > 阈值即 NOOP
     （库里已经有了，不必再提议一遍）
  ② vs `raw_observations(polarity='negative')` → 相似即
     `discard(reason='rejected_before')`（你否决过，别再来烦你）

  这是 living_doc 的核心价值所在——**它要捕捉的是还没被固化下来的新规范**。
  冷启动期噪声靠 ② 收敛：你否决几次，系统就学会你的边界。
- ★ **content 必须自包含**：写"在 X 项目中，Y 使用 Z"，不写"该项目使用 Z"。
  代词化的短句 embedding 质量差、跨会话召回命中率低——这是比检索算法更大的质量杠杆。
  实测你的真实输入里有 `"修复A2 A3"` 这种裸指令，窗口化 + 自包含是唯一解
- scope 宁窄勿宽
- `keywords` 抽标识符/专名/术语（供精确通道）

### 4.3 置信度门控（★ 规矩类**不设门控**，全部人审）

> **2026-10-22 3+3 实测改的**：原判错的样本 confidence 全在 0.93–1.0，
> thinking OFF 时甚至 15 条全报 1.0。**门控对规矩类完全无效**，
> 留着只是让人误以为有保护。规矩类改成"一律进人工待办"，见 §0.1.1 ③。

```
observation（规矩线索）  ★ 无门控 → 100% 进人工待办，审过才落线索池
atomic_fact（零碎事实）  confidence ≥ 0.85 → 入 write_facts job
discard（判定为丢弃）    confidence ≥ 0.95 → 记日志
                          ↑ 丢掉的东西捡不回来，所以也要严

低于阈值 / kind 在两个去处之间摇摆 / section_key 匹配不出
  → pending_items(status='pending') + 写 route_decisions
  → ★ 待办卡片**预选 observation**（偏向规矩侧），你一眼就能改回 atomic_fact
```

为什么两边不对称：**代价不对称 + 门控有效性不对称**。

- 误判成 `observation`：只是多进一次待办，后面还有汇总 + 审 diff 两道能拦。
- 误判成 `atomic_fact`：**永远不会被提议、不会注入**——静默丢失，无检测路径。
- 而 confidence 这个信号在规矩类上**实测无区分度**（错判也报满分），
  在事实类上还没验证过，先按保守值用，攒够 `route_decisions` 再按分桶改判率重算（§0.1.2）。

**规矩类天然是双道人审，不是重复劳动：**

| 道 | 在哪 | 审什么 | 错了的后果 |
|---|---|---|---|
| 第一道 | §4.3 待办 | 这条**该不该**当规矩线索（分类） | 后面还有第二道兜 |
| 第二道 | §4.6 `doc_proposals` | AI 归纳出的**规矩文本你认不认**（内容） | 否决 → 写 negative，以后不再提 |

只有过了第二道，内容才会被 hook 注入。所以"偏向 observation"是安全的：
最坏情况是你多审一条线索，然后否决它。

量级不是问题：实测 72 个会话只有 **24 条**含规范信号（16.4%）。
★ 按 3+3 的 73% 准确率算，这 24 条里约 **6–7 条会被判错**——
门控拦不住其中任何一条，**全审是唯一能捞回来的办法**。24 条人审完全扛得住。

### 4.4 四动作写入器（★ 批量，非每条一次）

v4 的"每条 +1 次 LLM"在实测数据量下会失控（单会话可产出上百条）。改为**按桶批量**：

```
新事实 → embedding（本地，批量）
      → 按 (scope, org_key, project_key, subject, role) 分桶
      → 每桶：向量+关键词召回各自 top5 候选
      → 1 次 LLM 判定整桶：[{action, memory_id, merged_content, mention_delta}]
      → ADD / UPDATE / DELETE(软) / NOOP，全部写 memory_events
```

事务边界（★ 保证真幂等）：

```
[事务外] LLM 调用 → 拿到判定结果
[事务内] 写 memories + memory_events + 推进 extraction_cursor
         唯一约束 (source_msg, content_hash) 兜住崩溃重放
```

- `NOOP` → `mention_count += 1`, `last_seen_at = now()`
- `UPDATE` → `revision_count += 1`，**不动 mention_count**
- 矛盾 → 软删除（`status='deleted'`），旧内容留在 `memory_events`

`UNIQUE(source_msg, content_hash)` 是硬幂等，LLM 的 NOOP 判定是语义幂等；
前者保证崩溃重放不产生重复行，后者保证不同来源的同义事实被合并。两者缺一不可。

### 4.5 汇总器

```
触发：org×project×section 的未 consumed observations 数 / 该 section 现有内容量 ≥ 阈值
      （新 section 3 条即触发，成熟 section 20 条）+ UI 手动
拉未 consumed observations + 当前 living_doc 同 (org, project, section) 行
  confirmed/pinned → 不改，新线索攒着待审
  draft            → 合成新 draft（排除 negative）
  → doc_proposals(pending)，observations 标 consumed
```

### 4.6 人工裁决（状态机补全 ★）

v4 的 `pending → approved` 就结束了；若批准后写入失败（LLM 超时），这条永久丢失，
违背"不丢未完成"。本版补全：

```
pending ──人批准──► approved ──排 job──► writing ──成功──► done
                      ▲                    │
                      └────失败可重试────── failed
pending ──人否决──► rejected          （超 expires_at）──► expired
```

```sql
-- 抢占式批准（幂等）
UPDATE pending_items SET status='approved', resolution=$2,
       resolved_by='human', resolved_at=now()
 WHERE id=$1 AND status='pending';          -- 影响行数=1 才算抢到
-- 随后：INSERT INTO jobs(kind='apply_decision', org_key=..., dedup_key='pending:'||$1)
-- job 成功后才由 worker 置 status='done'；失败置 'failed' + last_error
```

`doc_proposals` 同构：`accepted` → 排 job → job 内更新 `living_doc` +
写 `living_doc_history` → 成功才算数。

所有裁决同时写 `route_decisions`，作为 few-shot 反哺 + 评测集。

## 5. 读取链路

### 5.1 可见性条件（★ 修正 v4 的跨 org 串味）

```sql
WHERE status = 'active'
  AND ( scope = 'user'
     OR (scope = 'org'     AND org_key = :cur_org)
     OR (scope = 'project' AND org_key = :cur_org          -- ★ 必须带 org_key
                           AND project_key = :cur_project) )
```

v4 的 project 分支只比 `project_key`，两家公司都有 `web-app` 仓库时会互相召回。
本版双保险：`project_key` 用 `org/repo` 复合形式 + 召回条件带 `org_key`。

这不是权限控制，是**检索精度**：在 tiket repo 里召回 traveloka 的怪癖
只会挤掉真正相关的 top-K，纯噪声。

### 5.2 中文检索方案（CPU-only 下的最优解）

**结论：混合通道 + RRF 融合，reranker 只进写路径。**
★ 设计上是三通道（dense + keywords + sparse），但 **P3 实际只跑得起来两通道**——
本机依赖取不到 bge-m3 的 sparse 权重，见 §0.1.3 ⑤ 与下面通道表里划掉的那一行。

| 通道 | 实现 | 解决的失效模式 |
|---|---|---|
| dense | bge-m3 → `vector(1024)` + HNSW | 语义/同义改写（"数据库怎么配" ↔ "DB 连接池配置"） |
| keywords | 路由 LLM 抽取 → `TEXT[]` + GIN，`&&` 重叠 | **精确标识符**：`bookingFlowService`、`pgvector`、项目名、人名。dense 对这些经常糊掉 |
| ~~sparse~~ | ~~bge-m3 lexical weights → `sparsevec(250002)`~~ | ★ **P3 不做，见 §0.1.3 ⑤**：本机 `sentence_transformers 2.7.0` 加载 bge-m3 后**静默丢掉了 sparse 头与 colbert 头**（391 个参数张量里没有一个含 sparse/colbert），取不到词法权重。列继续预留，等 P7 有评测集再 A/B 决定要不要为它引 `FlagEmbedding` |

融合用 **RRF（k=60）**，不用加权和：两/三路分数量纲不同，加权和需要反复调参，
RRF 只看排名、尺度无关、跨数据集稳定。★ P3 实际是**两路** RRF。

**明确排除的方案及原因：**

- ❌ **`zhparser` / `pg_jieba`**：需在 Windows 上编译 PG 扩展，部署痛苦且易碎。
  ★ 原理由"bge-m3 的 sparse 已覆盖同类需求"**现在不成立了**（sparse 取不到，见上表），
  但结论不变：`keywords[]` 通道针对的正是同一类问题（精确标识符/专名），
  而它是 LLM 抽取 + GIN 索引，零编译成本。**若 P7 评测发现术语变体召回差，
  优先补 sparse（选项 A），不要回头装分词扩展。**
- ❌ **PG 内置 `to_tsvector`**：对中文基本无效（不分词，整句一个 token）
- ❌ **ColBERT / bge-m3 的 colbert 输出**：pgvector 不支持 late interaction，
  需存 1024维×N token 并在 Python 里算 MaxSim，存储与延迟都不可接受。
  ★ 且实测 ST 连 colbert 头都没加载，这条路在当前依赖下根本不通
- ❌ **读路径加 cross-encoder rerank**：无 GPU。bge-reranker-v2-m3 是 568M 参数，
  CPU 上 30 个候选约 1–3s，直接击穿 400ms 预算
- ★ **读路径 encode 不设长度上限**：bge-m3 `max_seq_length` 默认 **8192**，
  实测 512 字符就已 p95 403ms（击穿），粘一段 4000 字符的报错日志按线性外推要 2–3 秒，
  hook 会卡死对话开头。**必须显式设 `max_seq_length=128`**（§0.1.3 ①），这是护栏不是优化。

**Reranker 的正确位置**：写路径的四动作去重（§4.4）。那里异步、不限延迟，
而精度收益最大（避免重复/矛盾记忆入库）。召回候选从向量 top5 扩到 top20，
rerank 后取 top5 送 LLM 判定。

### 5.3 HNSW + 过滤的静默失效（★ 必须处理）

pgvector 的 HNSW 是"先索引取 `ef_search` 个候选，再套 WHERE 过滤"，
**被过滤掉的不会补回来**。数据量一大，`LIMIT 5` 经常只返回 1–2 条甚至 0 条，
且不报错——表现为"记忆系统好像不记事"，极难定位。

处理（★ pgvector 版本已确认 ≥ 0.8.0，走正道，不需要兜底方案）：

```sql
SET LOCAL hnsw.iterative_scan = relaxed_ordering;
SET LOCAL hnsw.max_scan_tuples = 20000;
```

`status='active'` 的过滤用 **partial index** 消化（§3 已建），
查询里带上同样谓词即可命中。

（版本 < 0.8 时的退路是 over-fetch：`LIMIT 300` 后在应用层过滤取 top5，
或按 org 分区表 + 分区 HNSW 索引。**已确认不需要，仅留作记录。**）

### 5.4 召回参考实现

```sql
SET LOCAL hnsw.iterative_scan = relaxed_ordering;

WITH d AS (                                    -- dense 通道
    SELECT id, row_number() OVER (ORDER BY embedding <=> :qvec) AS r
    FROM memories
    WHERE status='active' AND embedding IS NOT NULL
      AND ( scope='user'
         OR (scope='org'     AND org_key=:org)
         OR (scope='project' AND org_key=:org AND project_key=:proj) )
    ORDER BY embedding <=> :qvec
    LIMIT 50
),
k AS (                                         -- keywords 通道
    SELECT id,
           row_number() OVER (
             ORDER BY (SELECT count(*) FROM unnest(keywords) w
                       WHERE w = ANY(:qkw)) DESC ) AS r
    FROM memories
    WHERE status='active' AND keywords && :qkw
      AND ( scope='user'
         OR (scope='org'     AND org_key=:org)
         OR (scope='project' AND org_key=:org AND project_key=:proj) )
    LIMIT 50
),
fused AS (
    SELECT id, SUM(1.0/(60 + r)) AS rrf
    FROM (SELECT id, r FROM d UNION ALL SELECT id, r FROM k) u
    GROUP BY id
)
SELECT m.id, m.scope, m.role, m.content, m.mention_count, m.last_seen_at,
       f.rrf / MAX(f.rrf) OVER () AS rrf_norm,
       (f.rrf / MAX(f.rrf) OVER ())
         * (CASE m.scope WHEN 'project' THEN 1.0
                         WHEN 'org'     THEN 0.85
                         ELSE 0.7 END)
         * EXP(-LN(2) * GREATEST(0, EXTRACT(EPOCH FROM now()-m.last_seen_at)/86400)
                        / :half_life_days)                    -- 按 role 取 90/180/365
         * LEAST(1.6, 1 + 0.15 * LN(1 + m.mention_count))     -- ★ 封顶
         AS final_score
FROM fused f JOIN memories m USING (id)
ORDER BY final_score DESC
LIMIT :k;
```

★ `mention_count` 项 **必须封顶**（这里 1.6）。v4 用无上界的 `log(occurrence)`，
跑一段时间后必然压过时间衰减项——衰减降权 ≠ 遗忘，一条被反复纠正的高频错误记忆
会永远排在新事实前面。

query 侧：`:qkw` 由规则抽取（`[A-Za-z_][A-Za-z0-9_]{2,}` + 已知 org/project 名 +
`doc_sections.aliases`），`:qvec` 由 bge-m3 本地编码（CPU 上短文本约 20–50ms）。
查询文本不是裸 prompt，而是 **prompt + 上一轮 assistant 回复摘要 + 当前 project 名**，
上下文增广几乎零成本但显著改善短指令（如"修复A2 A3"）的召回。

每次召回写 `recall_log`（含 latency_ms），既供 UI 展示"本轮注入了什么"，
也是评测与调优的数据源。

### 5.5 组装

```sql
-- SessionStart：稳定层（全部来自库，唯一通路）
SELECT section_key, label, content FROM living_doc d
  JOIN doc_sections s USING (section_key)
 WHERE authority IN ('confirmed','pinned')
   AND ( (d.org_key = :org AND d.project_key IS NULL)      -- org 级规范
      OR (d.org_key = :org AND d.project_key = :project) ) -- 本 repo 专属
 ORDER BY s.sort, section_key;

-- 加上 user 级稳定记忆（情境性偏好不在此，靠每轮召回）
SELECT content FROM memories
 WHERE status='active' AND scope='user' AND role IN ('profile','pointer')
 ORDER BY last_seen_at DESC LIMIT 20;
```

```
[SessionStart]  上述两段 → 按 section 分组渲染 Markdown
[每轮]          §5.4 召回结果 top-K，排除本 session 已注入者
```

★ 在 personal 项目的会话里 `:org='personal'`，dida 的规范一条都不会注入——
这是当前单一全局 `CLAUDE.md` 做不到的。

## 6. 中断点与可恢复性

| 中断点 | 载体 | 恢复依据 |
|---|---|---|
| 1 摄取 | `UNIQUE(source, src_uuid)` | watcher 重扫不重复 |
| 2 抽取 | `extraction_cursor(source, session_id)` | 水位线续跑 |
| 3 任务 | `jobs.status` + `lease_until` | lease 过期自动重投 |
| 4 分类裁决 | `pending_items.status` | pending/failed 待处理 |
| 5 文档审核 | `doc_proposals.status` | pending 待处理 |
| 6 重跑 | `replay_actions.applied` | 逐条 apply 可续 |

worker 认领（崩溃安全）：

```sql
UPDATE jobs SET status='running', lease_until=now()+interval '5 minutes',
                attempts=attempts+1, started_at=now()
WHERE id = (
    SELECT id FROM jobs
    WHERE status='pending'
       OR (status='running' AND lease_until < now())     -- 僵尸 lease 回收
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

`attempts >= max_attempts` → `failed` + `last_error`，UI 可见可手动重投。

★ worker 执行前调 `endpoint_for(jobs.kind)`（§7.1）取端点。
`jobs.org_key` 仍需入队时固化，但用途是**统计与回溯纠正**，不参与端点选择。

## 7. LLM 调用编排

数据全部存本机 PG，embedding 用本地 bge-m3，**只有 LLM 调用需要出网**。
端点的选择依据是**任务对模型能力的要求和调用频率**，与 org 无关。

### 7.1 按任务选端点

| 任务 | 频率 | 对模型的要求 | 端点 | `thinking`（★ #52） |
|---|---|---|---|---|
| 路由器（§4.2） | 每个窗口 1 次 | 严格 JSON、分类准确、能读中文 thinking 块 | `default` | **ON**（73% vs 47%，异步链路吃得下 24.6s） |
| 四动作（§4.4） | 每批 1 次 | 需比对已有记忆做去重/更新判断，上下文较长 | `default` | ON（同为异步，且判错要改库，代价高） |
| 汇总器（§4.5） | 低频（攒够 N 条 observation 才触发） | **归纳能力最强的一环**，产出直接进活文档 | `default`，日后可单独换更强的 | ON |
| embedding | 每条记忆 1 次 | — | 本地 bge-m3，不走端点 | — |
| reranker | 写路径批量 | — | 本地 bge-reranker-v2-m3，不走端点 | — |
| 栏位匹配（§3.3 规则 1） | 每条 observation 1 次 | — | 本地 bge-m3，不走端点 | — |

★ **三个 LLM 任务全在异步写路径上，所以 `thinking` 一律 ON**；
读路径（SessionStart 注入、recall）**完全不调 LLM**，400ms 预算不受影响。
`thinking` 做成 `(endpoint, task)` 双维配置，不是全局开关。

```toml
# ~/.mem-assistant/config.toml
[llm]
default_endpoint = "dida-gateway"
# 可选按任务覆盖，不填就用 default
# [llm.task_endpoints]
# summarize = "stronger-model"

# thinking 按任务配（★ 默认全 ON，读路径不调 LLM 所以无需配）
[llm.thinking]
route = true
write_facts = true
summarize = true
```

`endpoint_for(task)` 是代码里唯一的取值点。`llm_endpoints` 表存在的意义
就是让你换模型时只改一行数据、不动代码。

### 7.2 失败处理

| 情况 | 处置 |
|---|---|
| 超时 / 5xx | `jobs` 指数退避重试，`max_attempts=5`，耗尽 → `failed` + `last_error`，UI 可手动重投 |
| 响应解析失败 | 带错误信息重试 2 次；仍失败 → `pending_items(stage='route', reason='llm_parse_failed')`，**不丢素材**（§0.1.1） |
| 端点整体不可用 | job 留在 `pending` 积压，**不自动切到别的端点**——不同模型的分类口径不一致，混用会污染 `route_decisions` 的校准数据（§0.1.2）。宁可积压 |
| 网关的分类器旁路超时（本机已知坑） | 见 §0.1.1；重试上限后落 pending，不无限重试 |

### 7.3 保留期清理必须覆盖 6 张表

`orgs.retention_days` 到期清理时，**原文散落在多处，只删 `conversation_log` 等于没删**：

`conversation_log`、`pending_items.raw_content`、`route_decisions.raw_snippet`、
`memory_events.old_content/new_content`、`doc_proposals.old_content/new_content`、
`recall_log.query_text`。

★ 例外：`doc_proposals` 中 `accepted` 的记录**不清理**——它是活文档变更的唯一凭证
（另有 `living_doc_history` 双保险）。

## 8. 评测（v4 完全缺失）

没有度量，`confidence` 阈值该调高调低（v4 §6 末尾）就是拍脑袋。

| 指标 | 来源 | 意义 |
|---|---|---|
| 挂起率 | `pending_items` / 总抽取项 | 打扰程度 |
| **改判率** | `route_decisions WHERE overridden AND confidence ≥ 该 kind 阈值` | **最危险**：模型高置信却判错，这些会静默污染记忆库。★ 必须按 `kind` 分开看（§4.3 阈值分级） |
| **规矩漏判率**（人工抽样） | 定期从 `memories` 里筛 content 含规范性措辞（必须/禁止/不要/一律/每次/不允许/应该）的条目，抽 30 条人工看有多少本该是 `observation` | ★ **唯一**能发现"静默丢失的规矩"的手段。高置信误判成 `atomic_fact` 的条目既不进 pending 也不会被改判，任何自动指标都看不见它——而后果是一条本该每次注入的规矩永远不被提议（§4.2） |
| hit@5 / MRR@10 | `eval_recall_cases` + runner | 召回质量 |
| p50/p95 延迟 | `recall_log.latency_ms` | 是否击穿预算 |
| 纠错次数 | `memory_feedback(kind='corrected')` | 记忆准确性的真实信号 |
| 空 section 数 | `doc_sections` 中 `living_doc` 无对应行的 section 数 | 词表定得太细 / 某类内容根本没有填充路径。★ `local_env`、`env_pitfalls` 长期为空就是 #47 那个洞没补上 |
| **栏位待裁决率** | `pending_items(stage='section')` / 总抽取项 | ★ #50 之后新增。向量匹配 top-1 分数低或 top-1/top-2 太接近就落这里，**它高说明 `aliases` 缺词**（§3.3 规则 4），是词表该补的直接信号 |
| **规矩类人审吞吐** | 待办中心 `kind='observation'` 的日均新增 vs 日均处理 | ★ #51 取消了门控，规矩类 100% 进人审。这个数一旦持续为正（积压增长），说明"全审"扛不住了，得回头给 prompt 加 few-shot 把准确率提上去，而不是把门控加回来 |

`route_decisions` 天然就是标注集（模型判 vs 人判），**不需要额外标注成本**。
P2 就把这张表和两个比率落下来，P3 加召回评测集。跑在 CI 或手动脚本里都行，
一张表 + 一个脚本，别上评测框架。

★ **方法论教训（#52 踩的坑，写成硬规矩）**：
任何关于"模型行为"的结论**不得基于单次采样**。单样本探测让我写出
"关掉 thinking 分类更准"，3+3 采样直接推翻（真实是 73% vs 47%，反着的）。
本节所有涉及模型判断力的指标，采样下限 **n ≥ 3 且要跑多轮看波动**；
延迟/token 这类确定性数字 1 次够，准确率/召回/稳定性这类**必须多轮**。

## 9. Web UI

| 页面 | 功能 | 优先级 |
|---|---|---|
| 待办中心 | pending_items + doc_proposals 待办数，统一红点 | 高 |
| 待确认 | 卡片：原文+窗口上下文+模型建议+为什么拿不准；选 scope/subject/role/kind/section → 批准/否决；批量。★ **kind 预选 `observation`**（偏向规矩侧，§4.3），一眼可改回；卡片上要直接标出"这条如果当零碎事实存，就永远不会被注入" | 高 |
| 待审 diff | 活文档 draft 逐段 采纳/否决/手改 | 高 |
| 归属管理 | org_match_rules + workspace_map；**默认归属（`resolved_by='default'`）的目录列表**，一次确认后批量回溯改 `org_key` | 高 ★新增 |
| 召回调试 | 输入 query → 看各通道命中（★ P3 是 dense + keywords 两路）、RRF、final_score 分解、latency | 中 ★新增 |
| 记忆库 | 按 org/scope/subject/role 浏览、编辑、删除、看 why 与 memory_events | 中 |
| 活文档 | **规则的正式编辑入口**（库是唯一真相源，§3.2）：按 org/project/section 树状浏览、直接改 confirmed 内容、draft 升 confirmed、看 `living_doc_history` | **高** ★升级 |
| 观察池 | 线索与否决项分开展示；手动触发汇总；**否决项是冷启动收敛的唯一手段**（§3.2.2），需能查看/撤销某条否决 | **高** ★升级 |
| 词表 | `doc_sections` 维护（label/aliases/sort/applies_to） | 中 ★新增 |
| 指标 | §8 全部指标的时序图 | 中 ★新增 |
| 重跑 | replay_runs 的 diff 审阅与批量 apply | 低 |
| 组织管理 | orgs 增删、retention；`llm_endpoints` 维护与 `endpoint_for(task)` 的按任务覆盖 | 中 |
| 设置 | embedding/LLM、阈值、汇总触发、保留期 | 中 |

"召回调试"页值得强调：多通道 + RRF + 多个加权因子，不可视化根本调不动。

★ "活文档"页是规则的唯一编辑入口——库里是唯一真相源，没有"改文件"这条路，
所以它必须与 hook 注入（P5）**同期交付**，否则你改规则只能去写 SQL。
最小可用范围：能浏览 section 树、能编辑 content、能看 diff 并采纳/否决。
其余（指标、重跑、批量操作）可以后补。

★ 观察池页的"否决项"不是附属功能：§3.2.2 的比对② 全靠它。
误否决一条 = 之后同类规范永远不再被提议，所以必须能查看并撤销。

## 10. 目录结构

```
memory-assistant/
├─ backend/app/
│  ├─ api/            # FastAPI：/recall /bootstrap /ingest /memories /pending ...
│  ├─ core/           # 配置·DB·embedding(bge-m3)·reranker·LLM客户端(endpoint_for(task))
│  ├─ ingest/         # transcript watcher·过滤规则·归属判定(纯规则)
│  ├─ pipeline/       # 路由·置信度门控·四动作·汇总器·抽取游标
│  ├─ queue/          # jobs 认领/重试/lease
│  ├─ retrieve/       # 多通道召回·RRF·加权·上下文组装（P3 两路，sparse 预留）
│  ├─ eval/           # 评测 runner + 指标
│  └─ prompts/        # 路由/四动作/汇总（带版本号）
├─ backend/alembic/
├─ backend/tests/
├─ integrations/
│  ├─ mcp-server/     # memory_recall/note/correct/search/pending
│  └─ hooks/          # mem-hook CLI（Claude Code / Qoder 通用）
└─ frontend/          # React + Vite
```

## 11. 分阶段路线

**P0 地基（★ 三项前提全部实测完毕，无阻塞项）**
- ✅ ~~确认 pgvector 版本 ≥ 0.8.0~~ **已确认**，§5.3 直接走 `iterative_scan` 正道，不需要 over-fetch 兜底
- ✅ ~~探测 `dida-gateway` 是否支持 tool use~~ **已探测**：支持，含数组形状 schema。
  附带实测出 thinking 开关、延迟量级与一个严重的分类误判，全部记在 §0.1.1。
  仍未测：上下文窗口上限、并发限流（需你再次授权才发请求，§13）
- ✅ ~~bge-m3 的 CPU 实测~~ **已测完，见 §0.1.3**。四条结论：
  **400ms 预算成立**（128 字符上限下 p95 239ms）／**必须显式设 `max_seq_length=128`**
  （默认 8192，用户粘一段日志就会让 hook 卡 2–3 秒）／**模型必须常驻**
  （冷启动 15s，hook 只能是瘦 HTTP 客户端）／**torch 线程保持默认 8**（16 反而更慢）。
  ★ **唯一坏消息：sparse 词法通道在当前依赖下取不到**，建议先按两通道做，见 §0.1.3 ⑤。
- ★ **`huggingface.co` 在本机不可达**（12s 超时，公司网络封锁）。
  必须设 `HF_ENDPOINT=https://hf-mirror.com`（实测 200/1.8s）；
  `modelscope.cn` 也通且更快（200/0.43s），可作备选下载源。
  **这条要写进 README 和环境初始化脚本**，否则每次下载都白等超时。
  bge-m3 权重（2.27GB `pytorch_model.bin`）已下到 `~/.cache/huggingface`，
  ★ 之后跑代码要带 `HF_HUB_OFFLINE=1`，否则每次启动都会去联网校验、白等 12s 超时。
- ✅ 本机 Python 环境已就绪，无需大装：`torch 2.13.0+cpu`、
  `sentence_transformers 2.7.0`、`transformers 4.57.6`、`fastapi 0.115.6`、
  `numpy 2.5.1`。**缺 `psycopg`（要装 psycopg3 + pgvector-python）**。
  ~~与 `FlagEmbedding`~~ → ★ **暂不装**：`sentence_transformers 2.7.0` 确实取不到
  bge-m3 的 sparse 头（模块链只有 `[Transformer, Pooling, Normalize]`，
  391 个参数张量里没有一个含 sparse/colbert），但按 §0.1.3 ⑤ 选项 C，
  P3 先做两通道，等有评测集再决定要不要为 sparse 引这个重依赖。
  磁盘余 41G，模型占 2.2G，常驻内存 1.8G（总 62G / 可用 38.6G），都够。
- 全 schema + Alembic + jobs 队列骨架
- LLM 客户端：多协议适配（anthropic/openai/ollama）+ `endpoint_for(task)`（§7.1）
  + 异常/日志 redact（§0.1）+ **`thinking` 开关按 `(endpoint, task)` 双维可配**
  （★ #52 实测：`route` 用 ON 准确率 73% vs OFF 47%，写入链路全异步所以 24.6s 可接受；
  `recall` 必须 OFF，那边有 400ms 预算）
- `orgs` / `llm_endpoints` / `org_match_rules` / `workspace_map` / `doc_sections` 初始化

**P1 摄取（只做 Claude Code）**
- Claude transcript watcher（tail `~/.claude/projects/*/*.jsonl`）+ §2 过滤规则
  + `UNIQUE(source,src_uuid)`
- 归属判定（纯规则，§4.1 seed）+ 未命中默认 personal + 抽取游标
- 验收：跑一遍全部 72 个历史会话（196MB），`conversation_log` 行数应约为
  JSONL 行数的 14%；**真实用户输入应约 146 条**（实测基线，偏差大说明过滤规则错了）
- ★ 实测数据量提醒：用户输入很少（146 条 / 72 会话），
  **素材主体是 thinking 块（单会话 454 条）**。§2.2 的两级筛是成本与质量的关键，
  不是可选优化；反过来 LLM 调用总量远低于预期，批量化（§4.4）仍要做但不紧迫

**P2 写入闭环**
- ★ **从空库起步，不做任何文件导入**（§3.2.1/§3.2.2）。
  路由器落库前的两次向量比对是这一阶段的核心实现：
  ① vs `living_doc` 同 section（冷启动时该表为空，① 自然不命中）；
  ② vs `raw_observations(polarity='negative')`（靠你在 P5 的否决逐步积累）
- 路由器（窗口制，★ `thinking=ON`，#52）+ `route_decisions`
- ★ **栏位匹配器**（#50）：`content` embedding vs `doc_sections.match_embedding`，
  取 top-1；分数低或 top-1/top-2 差距过小 → `pending_items(stage='section')`。
  22 行顺序扫描，同步做，不进队列
- ★ **规矩类不设置信度门控**（#51）：所有 `kind='observation'` 的候选一律进待办；
  `atomic_fact` 0.85 / `discard` 0.95 门控照旧
- 四动作批量写入器（先 ADD，再补 UPDATE/DELETE/NOOP）+ reranker 进写路径
- `pending_items` 完整状态机
- 验收：改判率可见（**按 kind 分开统计**）；同一 session 重跑不产生重复记忆（硬幂等生效）；
  比对① 生效——手工插一条 living_doc 后，同类 observation 不再入库；
  ★ 拿不准的条目确实进了 `pending_items` 且 kind 预选为 `observation`
  ★ **栏位稳定性**：同一条 observation 连跑 3 次，`section_key` 必须三次一致
  （#50 要治的就是这个飘移；LLM 自选时实测飘 2–3 个栏位）
  ★ **规矩漏判率**人工抽样（§8）跑一轮，作为 P2 验收项而非可选指标

**P3 读取闭环 ★ 此时系统可用**
- **两**通道召回（dense + keywords）+ RRF + 加权
  （★ sparse 不是"暂缓"，是**当前依赖下取不到**，见 §0.1.3 ⑤ / §5.2）
- `/bootstrap` + `/recall` + `recall_log`
- `mem-hook` CLI + Claude Code hooks 接入，实测端到端延迟
- ★ **两条从 §0.1.3 实测来的硬约束，实现时别漏**：
  ① 读路径 encode **必须设 `max_seq_length=128`**（默认 8192，长输入会让 hook 卡 2–3s）；
  ② `mem-hook` 只能是**发 HTTP 的瘦客户端**，模型常驻 FastAPI 进程
  （冷启动 15s：import torch 12.5s + load 1.3s，起进程的方案一律不可行）
- `eval_recall_cases` + runner
- 验收：真实对话中 p95 < 400ms（★ 已知底数：encode p95 239ms @128 字符，
  剩约 160ms 给 PG 两通道 + RRF + HTTP，**要按这个分配去测**）；hit@5 有基线数字；
  ★ 补一条**长输入护栏测试**：拿一段 4000 字符的报错日志当 query，端到端仍须 < 400ms

**P4 MCP + 纠错闭环**
- MCP server；`memory_correct` → 软删/UPDATE + negative observation + `memory_feedback`

**P5 活文档闭环**
- 观察池 + 汇总器（按比例触发，§4.5）+ `doc_proposals` + `living_doc_history`
- ★ **否决写回**：你在 UI 否决一个 `doc_proposal`
  → 写 `raw_observations(polarity='negative', section_key=同提案)`
  → 观察池页可查看/撤销（撤销 = 删除该 negative 行）。
  这是冷启动噪声期收敛的唯一机制（§3.2.2），不能省
- SessionStart 的 living_doc 渲染器：按 `section_key` 分组输出 Markdown，
  按 org/project 精确裁剪（★ 不做文件发布，见 §3.2）
- 服务离线时的哑缓存回退（§3.2.3）
- **最小可用的活文档编辑页必须与本阶段同期交付**（不能推到 P6）。
  库是唯一真相源，没有"改文件"这条退路；
  若此时没有编辑入口，你将被迫在库里改 SQL。
- 打开 hook 的 living_doc 注入，用真实会话验证规则确实生效
- 验收：在 `gds-agg-processor-tiket` 会话里只注入 dida org 级 + tiket project 级规范，
  在 personal 项目会话里完全不注入 dida 内容；停掉服务后 hook 仍能注入缓存；
  **否决一次后，同类 observation 不再产生提案**（比对② 生效，可用 replay 重跑验证）

**P6 Web UI**
- 待办中心 / 待确认 / 待审 diff / 归属管理 / 召回调试（先做这五个）
- 活文档页补全（基础编辑已在 P5）：`living_doc_history` 版本 diff 视图、
  `origin`/`origin_ref` 溯源展示、section 树、批量改 authority
- 再补记忆库 / 观察池 / 词表 / 指标 / 重跑 / 组织管理 / 设置

**P7 增强**
- sparse 通道回填 + A/B（有评测集才能判断值不值）
- shadow 重跑（`replay_runs`）
- 裁决 few-shot 反哺、保留期清理、expired 僵尸项、本地端点模型调优、confidence 阈值校准（§0.1.2）

## 12. v4 → v5 变更清单

**架构级**
1. **形态定为在线记忆层**：HTTP 核心 + Hooks（自动注入主路径）+ MCP（模型主动）三接入。
   读路径零 LLM、p95 < 400ms；写路径全异步 PG 队列。
2. **归属判定改为纯规则**（transcript 自带 `cwd`/`gitBranch`），不用 LLM。
   结果确定、可解释、可人工覆盖，且省掉每会话一次调用。
3. **摄取从"导出"改为 watcher tail JSONL**，并按实测（14% 信噪比）定义过滤规则；
   新增 `thinking` 块（两级筛后）与 compaction summary 作为素材。
4. **抽取单位从单条消息改为窗口**（user + assistant text + thinking）。
   实测有"修复A2 A3"这类裸指令，单条抽取无意义。
5. **引入 `jobs` 队列**（PG SKIP LOCKED + lease），取代模糊的"定期"触发，
   并作为三个中断点（游标/pending/proposal）之外的第四个可恢复点。
6. **中文检索方案定型**：dense + keywords[] + sparse 三通道 RRF；
   reranker 只进写路径（CPU-only 实测结论）；明确排除 zhparser / 内置 tsvector / ColBERT。

**Schema 修正（对应上轮 review 编号）**
7. project 召回加 `org_key` + `project_key` 强制 `org/repo` 复合 —— 修跨 org 串味（#1）
8. HNSW 改 partial index + `iterative_scan`（pgvector ≥ 0.8.0 已确认）—— 修 top-K 静默不足（#2）
9. 加 `content_hash` + `UNIQUE(source_msg, content_hash)`；LLM 调用移出事务 —— 修假幂等（#4）
10. `pending_items` 状态机补 `writing/done/failed` + `job_id` —— 修批准后写入失败丢数据（#5）
11. `extraction_cursor` PK 改 `(source, session_id)` —— 修跨 source 撞键（#6）
12. 删除 `role='rule'`，新增 `role='preference'`，划清与 living_doc 边界 —— 修双写（#7）
13. 加 `doc_sections` 受控词表 —— 修 section 自由生成导致碎片化（#8）
14. `occurrence` 拆为 `mention_count`/`revision_count`，加权项封顶 1.6 —— 修反向调权（#9）
15. 加 `route_decisions` 表，定位为标注资产（不属可重建的派生层）（#10）
16. 加 `replay_runs`/`replay_actions`，重跑改 shadow 模式 + diff 审阅（#11）
17. 加 `living_doc_history`；`accepted` proposal 不随保留期清理（#19）
18. `orgs` 只保留检索分区语义（`org_key`/`name`/`is_personal`/`retention_days`）
19. 补索引：`obs_pending`、`memory_events(memory_id,id)`、`memories_bucket` 加 `project_key`（#17）
20. 保留期覆盖全部 6 张存原文的表（#16）
21. `workspace_map.enabled`；新增 `memory_feedback`、`recall_log`、评测三表（#14）

**端点模型（确定 LLM 配置后追加）**

22. **`llm_policy` 二元策略 → `llm_endpoints` 命名端点**：v4 的 `cloud|local-only`
    无法表达真实配置（Anthropic 协议兼容网关 + Qwen 后端），改为端点表 +
    按**任务**选择（§7.1）。表存在的意义是换模型只改一行数据。
23. ~~跨 org 借用内网网关的校验~~ → **已撤销**（见 #45）：`kind`、`owner_org_key`、
    `resolve_endpoint` 的硬校验全部删除。
24. ~~`personal` 不得绑 internal_gateway~~ → **已撤销**（见 #45）。
25. ~~local 端点是 P0 硬前提~~ → **已撤销**（见 #45）。P0 前提最终剩 **2 个**
    （原 4 个：local 端点、pgvector 版本、bge-m3 CPU 延迟、reranker CPU 延迟；
    前两个已分别由 #45 与「pgvector ≥ 0.8.0 已确认」消除）。
26. 密钥卫生：token 不入库/不入文档，`jobs.payload`、`last_error`、`recall_log`
    与异常字符串必须 redact（httpx 异常常带请求头，而这两个字段会在 UI 显示）。
27. Anthropic-compat + Qwen 适配：结构化输出双层策略（tool use 优先，
    围栏 JSON + Pydantic 校验 + 2 次重试兜底）；**解析失败不丢弃，落 pending**；
    响应需跳过 thinking 块。
28. **confidence 需实测校准**，不可直接当概率用；给出按分桶统计改判率的 SQL，
    取改判率 <5% 的分位点为阈值，校准前先保守设 0.85。

**首批 org / 词表 / Claude-only 确定后追加**

29. **★ 发现你已有 `~/.claude/CLAUDE.md`（7.6KB，含 12 条 dida MUST 规则）**，
    且 Claude Code 每会话自动注入它。当时判断 living_doc 另行注入即构成双写冲突
    → 先后引出 30（发布回文件）、32（导入建基线）、44（方案 A）。
    ★ **最终裁决见 46：两者无耦合，不是双写冲突。** 30/32/44 全部作废。
30. ~~living_doc 定位为"规则文件的起草层"，人审 diff 发布回 `CLAUDE.md`~~
    → **已推翻，见 38。**
31. **living_doc vs memories 的边界划清**：稳定/每会话无条件注入 → living_doc；
    情境性/按 query 召回 → memories。`role='preference'` 收窄为"情境性偏好"，
    全局偏好归 living_doc。
32. ~~**新增冷启动导入器**（P2 前置依赖）：解析现有规则文件 → `living_doc(confirmed)`
    基线；路由器据此执行 `already_codified` 丢弃~~ → **已删除，见 46。**
    被 §3.2.2「空库 + 否决记忆」取代。
33. ~~漂移检测 + `published_snapshot` + 三方 reconcile~~ → **已删除，见 38。**
34. **`doc_sections` 初始词表落地**（§3.3）：25 个 section，
    每条都对应 CLAUDE.md 现有规则或历史会话真实发言，非通用模板。
35. **归属规则实测**：dida 的 git remote 是 `codeup.aliyun.com/62bc118a3e81781f3ad1d681/...`。
    ★ `codeup.aliyun.com` 是公共托管，**只用 host 匹配会误判他司仓库**，
    必须匹配组织路径；`git_remote_path` 优先级(10) 高于 `cwd_prefix`(50/80)。
    `~/own/` 是混合目录，靠 git 规则优先兜住。~~未命中一律进 pending，绝不默认 personal~~
    → 已改：org 只是检索分区不是权限边界，未命中落默认桶 personal 并继续抽取
    （`resolved_by='default'`），UI 归属管理页一次性确认 + 按 `source_msg` 回溯纠正。
36. **首期只支持 Claude Code**（Qoder/自定义 agent 延后）；`source` 字段与
    适配器分层保留，日后加 source 不动核心。
37. 实测素材量修正预期：72 会话仅 **146 条真实用户输入**，
    素材主体是 thinking 块 → §2.2 的两级筛从"优化"升级为"必需"；
    LLM 调用总量远低于原估，批量化仍做但不紧迫。

**用户指出"所有记忆都应该在数据库"后追加**

38. ★ **推翻"发布成文件"设计，确立 PG 为唯一真相源。** 文件发布会制造第二真相源
    （手改文件 vs 库内 draft 双向漂移），我为了兜它引入的 `published_snapshot` /
    漂移检测 / 三方 reconcile **全是这个错误设计的症状，一并删除**。
    规范注入通路收敛为一条：SessionStart hook 从库读。
39. **删除 `doc_targets` 表与 `doc_key` 概念**（`dida_standards` 等分组名作废）。
    `org_key + project_key + section_key` 已能唯一定位任何一条规范，
    `doc_key` 是文件发布设计的残留。`doc_sections` 改用 `applies_to ∈
    {org, project, both}` 表达"这条规范是全局还是单 repo 专属"。
40. **韧性改用哑缓存**（§3.2.3）：hook 成功时把 bootstrap payload 落本地文件，
    服务不可达时 cat 它并标注"离线快照可能过期"。缓存是只读派生物、永不回写库，
    因此不构成真相源——用 20 行代码替代了整套双向同步。
41. `living_doc` 唯一约束改 `COALESCE(project_key,'')` 表达式索引
    （PG 中 NULL 互不相等，原 `UNIQUE(...,project_key)` 会让 org 级规范可重复插入）。
42. 新增 `living_doc.origin/origin_ref`，新增 `living_doc_inject` 索引；
    注入渲染改为按 org/project **精确裁剪**——在 tiket repo 里不注入其它供应商的怪癖，
    在 personal 项目里完全不注入 dida 内容。
    ★ `origin` 取值后收窄为 `summarized` / `human`（`imported_from_file` 随 #46 删除），
    `origin_ref` 不再有"来源文件行号"语义。
    ~~**比现在的全局文件更省 token**~~ → 已推翻：两者无耦合，token 是**相加**（见 #46）。
43. ~~§3.2.1 明确了现有 `~/.claude/CLAUDE.md` 的三种处置方案（A 导入后精简文件 /
    B 保留手工维护不导入 / C 导入为只读镜像）~~ → **A/B/C 框架整体作废，见 46。**
    其中"选 A 的必然代价：规则编辑入口从改文件变成改 UI"这一条仍然成立，
    但理由变了——不是因为导入了文件，而是因为**库本来就是唯一真相源**（#38）。
44. ~~★ **方案 A 已选定**，补充六步迁移顺序（备份 → P2 导入建基线 → 人工抽查 →
    P5 编辑页可用 → 验证 hook 注入 → 才精简文件），以及精简后 `CLAUDE.md` 的
    5 行模板~~ → **已全部删除，见 46。**
45. ★ **拆除端点信任模型，聚焦记忆系统本身**（用户指示："先不考虑数据安全和权限"）。
    删除：`llm_endpoints.kind` / `owner_org_key` / `orgs.llm_endpoint_key` /
    `orgs.allows_public_cloud` / 循环外键 / `resolve_endpoint()` 及其两个异常 /
    原 §7 隐私模型整节 / "local 端点是 P0 硬前提"。
    替换：§7 改为「LLM 调用编排」——端点按**任务**选（路由/四动作/汇总），
    首批就一个 `dida-gateway`，`endpoint_for(task)` 是唯一取值点。
    保留但重新定性：`orgs` / `scope` / `org_match_rules` / §5.1 可见性谓词
    **不是权限控制，是检索分区**——判错的后果是"注入了不相干的记忆"（噪声），
    不是事故。因此归属未命中从「阻塞抽取 + 进 pending」改为
    「默认落 `personal` + 继续抽取 + UI 列出待确认 + 可按 `source_msg` 批量回溯纠正」。
    连带简化：P0 前提 4 → 3 个（pgvector ≥ 0.8.0 确认后 → **2 个**）；
    §13 待决项 6 → **3 个**。
    密钥 redact（#26）保留——那是防止 token 出现在调试 UI 里的实现细节，不是策略。

**用户指出"不应该和 CLAUDE.md 发生任何关系"后追加**

46. ★ **记忆系统与 `~/.claude/CLAUDE.md` 完全解耦：不读、不导入、不修改、不感知其存在。**
    推翻 #29/#32/#43/#44 与方案 A。`CLAUDE.md` 继续由你手工维护、由 Claude Code
    原生注入；`living_doc` 从空库起步，只装**从对话里学到的东西**。
    理由：导入器让系统承担两个不属于它的职责（解析外部文件格式、改写用户文件），
    并把系统绑死在 Claude Code 的文件约定上——换成 Qoder 或自定义 agent 就废了。
    解耦后同一套代码对任何 agent、任何用户都能跑。
    **代价**：① 注入 token 从"替换"变成"相加"（§1.2 已修正，靠 org/project 裁剪控增量）；
    ② 冷启动噪声期——AI 会把你已成文的规则当新发现提议一遍。
    **对策 = 否决记忆**（§3.2.2）：否决一个 `doc_proposal` → 写
    `raw_observations(polarity='negative')`；此后 observation 落库前做两次向量比对，
    ① vs `living_doc` 同 section（已有 → NOOP）、② vs negative 集（否决过 →
    `discard(reason='rejected_before')`）。取代 `already_codified` 规则，
    且它是通用机制（覆盖任何"你不认可的提议"），不是 CLAUDE.md 特例。
    连带改动：`living_doc.origin` 删 `imported_from_file`；新增 `obs_negative_hnsw`
    部分索引；删除六步迁移顺序与 5 行精简模板；P2 删导入器；P5 增"否决写回"；
    观察池页从「中」升「高」（否决项是冷启动收敛的唯一手段，需可查看/撤销）；
    §3.3 词表的"现有证据"列明确定性为**设计期依据，非运行时输入**。

**用户指出"描述太不接地气"并要求"不确定的都要人工审核、尤其规矩类"后追加**

47. ★ **路由主判据从"看口气"改成"看用途"**（§4.2）。
    原规则：`is`（是什么/在哪）→ `atomic_fact`；`should`（应该怎么做）→ `observation`。
    新规则：**"这条是不是每次开工都要无条件提醒 AI？"** 是 → `observation`；
    否 → `atomic_fact`。`is`/`should` 降为次判据，只用于在 observation 内部挑 `section_key`。
    **改的原因**：`doc_sections` 里 `local_env`（JDK/Python/Maven 路径）与
    `env_pitfalls`（分类器超时的四步应对）装的全是 `is` 形内容，按原规则会被送进
    memories，这两栏**永远填不满**——而它们恰恰是每会话都要注入的。
    导入器还在时靠导入兜底，#46 解耦后彻底没有填充路径。
48. ★ **置信度阈值按 kind 分级 + 拿不准一律进人工待办**（§4.3，用户明确要求）。
    `observation` 0.95 / `atomic_fact` 0.85 / `discard` 0.95（丢掉的东西捡不回来）。
    依据是**两种误判代价不对称**：误判成 observation 只是进线索池，后面还有
    汇总 + 人审 diff 两道能拦；误判成 atomic_fact 则是**一条本该每次注入的规矩
    被静默存成零碎事实，永远不会被提议、不会注入，且没有任何环节能发现**。
    因此待办卡片的 kind **预选 observation**（偏向规矩侧），你一眼可改回。
    连带：`route_decisions` 增 `kind` + `confidence` 两列（校准要按 `(kind, bucket)`
    两维分桶）；§0.1.2 的校准 SQL 改两维分组；§8 增两个指标——
    **规矩漏判率**（人工抽样，唯一能发现静默丢失的手段）与 **空 section 数**
    （`local_env`/`env_pitfalls` 长期为空就是 #47 的洞没补上）；
    §9 待确认页说明预选行为；P2 验收补一条。
    量级不是问题：实测 72 会话仅 24 条含规范信号，人工审得过来。
49. **沟通方式**：用户指出"描述越来越不接地气，很多名词很难理解"。
    文档正文保持精确（那是施工图），但**对话里必须先讲人话再补术语**，
    自造名词（活文档/观察池/否决记忆/四动作/窗口制）每次提到都要顺手解释一句。

**用户授权 token 探测后，跑了 3+3 对照采样（2026-10-22），下面四条全由实测数据驱动**

50. ★ **`section_key` 不再由 LLM 自由选，改成向量匹配 `label + aliases`**（§3.3 规则 1）。
    实测：thinking ON 时同一条信息三次跑出 2–3 个不同栏位
    （Lombok → `dependency`/`code_style`/`dependency`；JDK17 → `local_env`/`build_env`/`build_env`）。
    **栏位一飘，§3.2.2 的两次向量比对就在错误的分区里做**——
    同栏位查重查不到重复，否决记忆也拦不住已经否决过的提议。
    改成拿候选文本 embedding 去比 `doc_sections.label + aliases` 的 embedding 取 top-1，
    分数低或 top-1/top-2 差距过小 → `pending_items(stage='section')`。
    连带：§3.3 的 `aliases` 从"给人看的注释"**升格为运行时输入**，
    所以三张表全部填了初稿；§13 第 1 项改写成"请你过一眼"而非"请你填"；
    规则 3 补一条"22 个是上限，后续只合并不新增"（栏位越多 top-1/top-2 越接近，pending 越多）。
51. ★ **规矩类（`observation`）取消置信度门控，100% 进人工待办**（§4.3，改写 #48 的阈值分级）。
    实测：15 条 kind 判错的样本 confidence **全在 0.93–1.0**；thinking OFF 时
    15 条**全部自报 1.0**。0.95 的阈值对这些高置信错判**一条也拦不住**，
    而 §8 的改判率指标只统计进了待办的，同样抓不到。
    #48 的分级保留在 `atomic_fact`（0.85）与 `discard`（0.95）上——
    那两类量大且门控尚未证伪。
    按 73% 准确率算，24 条规矩信号里约 6–7 条会被判错，全审是唯一捞回手段，
    且 24 条的量人审完全扛得住。这也正好落在用户"不确定的都要人工审核、
    尤其 rule 类"的要求上，只是现在有了数据支撑而非仅凭代价推理。
52. ★ **`route` 任务用 thinking ON，`recall` 任务不用**（§0.1.1 ②，翻案）。
    **原来的结论是错的，且错在方法上**：单样本探测（各 1 次）显示"OFF 分类更准、
    只是少抽 1 条"，据此写了"默认关掉 thinking"。3+3 采样后事实相反——
    召回两种模式都是 15/15，分类 ON 11/15=73% vs OFF 7/15=47%。
    代价是延迟 24.6s vs 5.1s、token 932 vs 337，但写入链路全异步
    （PG job 队列 + lease），延迟落在后台用户无感；
    而分类准确率直接决定规矩会不会被静默丢掉，不可逆。
    ⇒ `thinking` 必须是**端点级 + 任务级**双维开关，不能全局一刀切。
    **教训写进 §8**：任何"模型行为"类结论都不得基于单次采样。
53. **栏位 25 → 22**：砍掉三个只有 1 条规则的薄栏位——
    `resource_concurrency` 与 `api_design` 并入 `code_style`，`investigation` 并入 `process`。
    理由见 §3.3「合并记录」。同时把 §3.3 三张表的「现有证据」列换成 `aliases（初稿）` 列
    （原列的内容是设计期依据，已由 #46 定性为非运行时输入，占着列位没用了）。
    ★ 保留 `local_env` 与 `build_env` 两个栏位不合并（实测里 LLM 常混用，
    但语义确实该分开：前者"这台机器装了什么"，后者"这个项目怎么构建"），
    改用 aliases 掰开，见 §3.3「合并记录」末尾。
54. ★ **bge-m3 CPU 实测完成，P0 前提全部结清**（新增 §0.1.3，脚本在 `bench/`）。
    四条正面结论：**400ms 预算成立**（128 字符上限下 p95 239ms，78ms 是固定开销）；
    **torch 线程保持默认 8**（实测 1/2/4/8/16 五档，16 线程 p50 118ms 反而比 8 线程
    88ms 慢，线程同步开销盖过并行收益）；**常驻内存 1.8GB**（本机 62GB / 可用 38.6GB，
    装得下 bge-m3 也还装得下 reranker，但建议 reranker 独立进程避免抢内存带宽）；
    **批量吞吐 batch 32 = 22ms/条、batch 128 = 21ms/条**，边际收益 32 以上就平了
    ⇒ §4.4 的桶大小取 32 即可。
    两条硬约束（写进 P3 验收）：
    ① **读路径 encode 必须显式设 `max_seq_length=128`**——bge-m3 默认 8192，
      实测 512 字符已 p95 403ms 击穿预算，用户粘一段 4000 字符报错日志按线性外推要 2–3 秒，
      hook 会卡死对话开头。这是护栏不是优化。
    ② **模型必须常驻，`mem-hook` 只能是发 HTTP 的瘦客户端**——冷启动 15s
      （import torch 12.5s + load 1.3s）。这把 §1 的"HTTP 核心 + hook"
      从设计偏好升级为硬约束。
    ★ **一条坏消息，需用户拍板**：`sentence_transformers 2.7.0` 加载 bge-m3 后
    模块链只有 `[Transformer, Pooling, Normalize]`，**391 个参数张量里没有任何一个
    名字含 sparse / colbert / linear**——ST 把 bge-m3 的 sparse 头和 colbert 头
    **静默丢掉了**，`encode()` 也没有 `return_sparse` 参数；`FlagEmbedding` 未安装。
    ⇒ **三通道检索的第三条（sparse）当前无法实现**。
    三个选项见 §0.1.3 ⑤，**建议选项 C：P3 先按两通道（dense + keywords）做**，
    `embedding_sparse` 列继续预留，等 P7 有评测集再 A/B。
    理由：P3 本来就没启用 sparse；`keywords[]` 与 sparse 的目标高度重叠
    （都是治 dense 对 `bookingFlowService` 这类糊掉的问题）；没有评测集无法判断
    sparse 值不值一个重依赖。
    连带改动：§5.2 通道表 sparse 行划掉并改述、"明确排除的方案"里
    `zhparser/pg_jieba` 的原理由"sparse 已覆盖"改为"keywords[] 覆盖"（**结论不变，
    理由换了**）、colbert 那条补一句"ST 连头都没加载，这条路当前不通"；
    P3 从"三通道"改为"两通道"；技术栈表 embedding 行标注 sparse 待定；
    §13 第 3 项改为已结清 + 一个待拍板取舍。
    ★ **方法论**：本轮第一次跑长度扫描时 p95 抖到 3.5 秒，重跑一次后完全单调
    （见 [[feedback-no-single-sample-conclusions]]）。**噪声样本必须重跑确认**，
    否则会把系统瞬时负载当成模型特性写进方案。

## 13. 仍需你拍板

已定：LLM 端点 = `dida-gateway`（全部任务共用，§7.1）；首批 org = `personal` + `dida`；
首期只支持 Claude Code；`doc_sections` 初版见 §3.3；
**与 `~/.claude/CLAUDE.md` 无任何耦合**（不读不导入不改，冷启动空库 + 否决记忆，#46）；
**路由看用途不看口气，拿不准一律进人工待办且偏向规矩侧**（#47/#48）；
**规矩类不设置信度门控、100% 人审**（#51，实测门控对错判无区分度）；
**`section_key` 由向量匹配定，不由 LLM 自由选**（#50）；
**`route` 任务用 thinking ON、`recall` 不用**（#52）；
**不做端点信任/权限模型**（#45），org 只作检索分区；
pgvector ≥ 0.8.0 已确认（`hnsw.iterative_scan` 可用）。

剩余：

1. **`doc_sections` 的 `aliases` 请你过一眼**：★ 我已按你的会话用语填了**初稿**
   （22 个栏位全填了），你只需改错补漏，不用从零填。
   **改完之后它是运行时输入，不再是注释**——因为 #50 把 `section_key` 从
   "LLM 自由选"改成了"拿候选文本去和 `label + aliases` 做向量匹配"，
   aliases 直接决定匹配命中率，缺项会变成 `stage='section'` 的 pending。
   ★ 最该你亲自看的是 `local_env` 与 `build_env` 两行：实测里 LLM 把这两个
   混着用（JDK 17 三次给了 `local_env`/`build_env`/`build_env`），
   我靠 aliases 把它们掰开，但你的用语习惯只有你知道。
   另外请确认：③ 组那 4 个 project 级 section
   （`field_format` / `business_boundary` / `flow_gotcha` / `contact_ops`）
   够不够覆盖 25 个供应商的怪癖？（22 个是上限，后续只合并不新增，见 §3.3 规则 3）
2. ~~是否要我用你的 token 对 `dida-gateway` 发探测请求~~ ✅ **已做**（2026-10-22，
   你授权后跑了 1 次单样本 + 3+3 对照）。tool use 可用、含数组 schema；
   延迟与分类准确率见 §0.1.1 ②③。**仍未测**：上下文窗口上限、并发/限流、
   长 prompt 的延迟曲线——这三项要发请求，等你再点头。
3. ~~**P0 剩一个前提**：bge-m3 的 CPU 实测延迟~~ ✅ **已测完，P0 前提全部结清**（§0.1.3）。
   400ms 预算成立（128 字符上限下 p95 239ms）；torch 线程保持默认 8（实测 16 反而更慢）；
   常驻内存 1.8GB，本机 62GB 装得下。
   ★ **但测出一个需要你拍板的取舍**：`sentence_transformers 2.7.0` **取不到 bge-m3 的
   sparse 词法权重**（它加载后只保留 dense 头，sparse/colbert 头被静默丢掉），
   而 `FlagEmbedding` 未安装。所以"三通道检索"的第三条现在做不了。
   我建议**先按两通道（dense + keywords）做**——P3 本来就没启用 sparse，
   且 `keywords[]` 针对的正是同一类问题（精确标识符/专名）；
   等 P7 有了评测集再 A/B，别现在为一个不确定收益引重依赖。**若你不同意，说一声。**
   ★ 下载相关：`huggingface.co` 本机不可达，必须 `HF_ENDPOINT=https://hf-mirror.com`；
   权重已缓存，之后跑代码带 `HF_HUB_OFFLINE=1` 免掉每次启动 12s 的联网超时。
