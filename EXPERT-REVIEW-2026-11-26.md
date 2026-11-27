# mem-xy 个人记忆系统 —— 第二轮架构评审与 Roadmap（2026-11-26）

> 评审方式：本轮为逐行代码核查（不依赖上一轮评审的结论惯性），并与其 2026-11-09 版《EXPERT-REVIEW.md》及 decisions-log #99–#110 的实施记录交叉对照；另有读路径、写入链路两路独立专项审查（subagent 深审）于收尾时回收，新发现去重后整合进第六节（关键条目主审已抽查核实）。
> 本轮实际读过的代码：`backend/app/retrieve/recall.py`（全）、`backend/app/api/read.py`（全）、`backend/app/retrieve/bootstrap.py`（全）、`backend/app/pipeline/router.py`（全）、`backend/app/pipeline/facts.py`（全）、`backend/app/queue/jobs.py`（全）、`backend/app/worker.py`、`backend/app/core/llm.py`（前 150 行 + 异常语义）、`backend/app/prompts/route.py`（受控词表与 schema）、`integrations/hooks/mem-hook.py`(全)；保留期/清理以全库 grep 确认。
> 诚实声明：本轮曾并行派出两个分链路审查子代理，一度因会话代际切换未能回收，遂改为主审直接逐行读码（第二、三节所有发现均落到文件行号）；两份子代理报告最终在收尾时送达，已去重整合为第六节。

---

## 一、总体判断：主要矛盾已经转移

上一轮（11-09）的定性是"工程纪律 A 级、架构选型 B+、产品闭环 D 级"。两周后对照实施记录：备份三层防线真机闭环、评测集从 0 到 96 条并出了基线（hit@5=0.904 / MRR@10=0.686 / p95=147ms）、hooks+MCP 真实接入、51 会话回填闭环、pytest 独立库根治零 flaky（536 passed）、规矩漏判率实测 38.2% 并推动 route prompt 升 v2。**产品闭环已从 D 升到 B。**

但"机器在跑"不等于"记忆有用被证明"。当前系统的核心矛盾从「资产会丢 / 效果不可测」转移为三个新矛盾：

1. **生产链路上存在设计与实现脱节的静默缺口**——最典型的是查询上下文增广在生产 hook 路径上是死代码（S1）与压缩后记忆不回归（S2）。这类缺口的共同特征：测试全绿、评测集测不到、只有读码才能发现。
2. **健壮性欠账**——无重试退避（S3）、route 段② 无窗口级 checkpoint（S4）、保留期清理仍为零（S6）。在 LLM 调用 53s/次的现实下，这些欠账的代价是真实金钱和工期。
3. **"记忆有用"仍未被度量**——评测集只测检索排序，不测注入效果；mention 信号只在写路径更新，从未见过真实使用数据（§三）。

---

## 二、新发现的缺陷（按严重度排序，全部落到行号）

### S1 ★ 生产 hook 不传 prev_assistant_summary——查询增广在生产路径上是死代码

- `integrations/hooks/mem-hook.py:213-221`：recall 请求体只有 `query / session_id / cwd / source / exclude_ids`，**没有 `prev_assistant_summary`**。
- 而 `backend/app/retrieve/recall.py:161-184` 精心设计的"query + 上一轮回复摘要 + 项目名"增广（§6.4，明确写着"'修复A2 A3'这种 query 单独编码基本召不回任何东西"）依赖这个字段；`backend/app/api/read.py:48` 也为它留了 max_length=2000 的入口。
- 结论：**设计意图没有在生产路径落地**。"短指令召不回"这个当初驱动设计的场景，今天依然存在。评测集 96 条都是干净短 query，掩盖了这一点。
- 连带问题（即使将来接通也会失效）：`recall.py:8-9` 写明 encode 走全局 `max_seq_length=128`，而 `build_query_text` 的拼接顺序是 query→summary→project。用户 prompt 常超 128 token（中文约百字即满），此时 bge-m3 右侧截断会把 summary 和 project 名**全部**截掉——增广在长 query 下静默无效。评测集 query 短，所以基线 hit@5=0.904 里看不出这个天花板。

**修法**：
1. hook 侧从 stdin 的 `transcript_path` 读最后一条 assistant 消息、截 2000 字符作为 `prev_assistant_summary` 传入（Claude Code 的 UserPromptSubmit 事件带 transcript_path）；
2. encode 输入改为分段预算：query 本体压缩到 ~96 token（超长时保头部 + 抽 keywords），summary/project 保留 ~32 token——否则增广永远先被截死。

### S2 压缩之后，注入过的记忆永远不再回来

- `mem-hook.py:241-251`：`session-{id}.json` 记录本会话已注入的 memory id（上限 200，FIFO），每轮 recall 作为 `exclude_ids` 传给后端——这个机制本身正确（防同一条记忆每轮重复烧 600 token 预算）。
- 问题：Claude Code 的 `/compact` 或自动压缩后，**上下文里已注入的记忆随压缩消失，但 hook 的 exclude 状态文件还在**——这些记忆在本会话余下的轮次里不会再被召回注入。SessionStart(compact) 只重注稳定层（living_doc confirmed/pinned + user profile/pointer，`backend/app/retrieve/bootstrap.py:24-41`），动态事实层在压缩点之后全部断层。
- plan 里"SessionStart 稳定层不排除本会话早期记忆"的例外只覆盖了 recall.py 的 session-born 排除，没有覆盖 hook 侧的 injected 状态。

**修法**：`mem-hook.py` 的 bootstrap 分支（`mem-hook.py:144-163`）开头清空/忽略 `session-{id}.json`（一行改动）。SessionStart 在 compact 时也会触发，正好是重置点。

### S3 job 重试无退避——网关故障期把重试预算几十秒内烧光

- `backend/app/queue/jobs.py:111-145`：`fail()` 在 attempts 未耗尽时**立即**把 job 打回 pending；`jobs.py:182-199`：worker loop claim 不到才 sleep 0.5s，claim 到就连续干。`backend/app/core/llm.py:33` HTTP read 超时 180s，客户端内部**没有任何重试**（`complete()` 单次直呼，`llm.py:119-141`）。
- 后果链：dida-gateway 短暂抖动/维护窗口 → route job 首窗即抛 LLMError → 立即重新入队 → 立即被（同一个或另一个）worker 取走再失败……`job_max_attempts` 次预算在**分钟级**内烧完，job 落 `failed`，之后必须人工重排。回填期一次 20 分钟的网关故障就能把队列打出一排 failed。
- 且 LLMError 触发的是**会话级**重来（见 S4），每次重试都可能重付几分钟 LLM 钱。

**修法**：jobs 表加 `next_retry_at` 列；`fail()` 按指数退避设置（如 60s×2^n，封顶 1h）；`CLAIM_SQL` 增加 `AND (next_retry_at IS NULL OR next_retry_at <= now())`。改动小、语义清楚。

### S4 route 段② 无窗口级 checkpoint——第 8 窗失败，前 7 窗 ×53s 全部重付

- `backend/app/pipeline/router.py:219-252`：三段式（读→LLM→写）修好了"LLM 不进事务"（旧 B1），但把"断点续跑粒度"留在了**会话级**：段② 的全部判定结果只存内存（`_Outcome` 列表），段③ 一次性写库、游标一次推进。段② 中途抛 LLMError → 什么都没写 → 队列重试 → 从第一个窗口重来。
- 实测口径下单窗 ≈53s、平均 9 窗/会话：一次网络抖动的期望损失是 ~4.8 分钟 LLM 时间，而 `route_decisions` 表本来就是 insert-only、天然可逐窗落库。

**修法**：`_call_llm` 每窗成功后立即单窗短事务 INSERT `route_decisions`（含 prompt_ver）；段③ 写 pending/facts/游标时跳过已有同版本 decision 的窗口。重试从断点续，钱和时间都省下来。

### S5 写入器去重的"角色墙"——role 判错一次，同一事实两行且彼此永不可见

- `backend/app/pipeline/facts.py:52-54`：候选查询的 `_BUCKET_WHERE` 要求同 `(scope, subject, role, org, project)` 才互为候选；`SUBJECTS=("user","project")`（`prompts/route.py:24`）足够粗，但 `ROLES` 四值之间语义有真实交叠（fact vs pointer、preference vs fact）。
- 后果：route LLM 把同一事实的两次表述判成不同 role → 两行落库、分属不同桶、**结构上永远互相看不见**（去重候选只在桶内取 top5）。`UNIQUE(source_msg, content_hash)` 只防同源重放，防不了跨会话跨 role 的语义重复。#108 已经抓到一对实证（#2441/#2506 近重复），当时归因"判重缺口"，根因在这里。
- 附带：`facts.py:246-250` 整桶 parse 失败时全桶按 ADD 落库（正确方向，宁重复不丢），但重复的收敛完全指望"下一批写入还有机会合并"——而合并的前提恰恰是落进同一个桶。

**修法**（择一或并用）：
1. 候选查询放宽到同 `(scope, org, project)`，role 降级为排序加分项（一行 WHERE 的改动）；
2. 增设周期性"合并扫描"job：对新增记忆在同 scope 内做 embedding 近邻查询（阈值 ~0.9），命中即生成合并裁决卡进人审。注意：这是**减少**未来重复的净减负，不是新增人工卡点，与"不新增人工环节"的规矩同向。

### S6 保留期清理仍为零，且增长面比上一轮评估的更宽

- 全库 grep 确认：`orgs.retention_days` 字段还在（`backend/app/orgs.py:6` 注释自认"P7 清理会读它"），**6 张原文表之外**，`recall_log`（每轮对话一行）、`llm_usage`（每次 LLM 调用一行）、`memory_events`、`route_decisions`、`jobs` 历史全部只进不出。
- hooks 已真实接入，recall_log 的增长速率从"评测时偶发"变成了"每轮必写"（`recall.py:296-310`）。单机磁盘是唯一资源（上一轮 §5.2.5 的定性没变），这块从 Phase 2.5 拖到现在，是旧 roadmap 里唯一原封未动的止血项。

**修法**：一个 `cleanup` job（队列里已有全部基础设施）：按 `orgs.retention_days` 清 6 张原文表；recall_log / llm_usage / jobs( done/failed ) / route_decisions 按固定窗口滚动（如 90 天，UI 需要的聚合先落 metrics 表）。一次性把"只进不出"的表全部纳管。

### S7 TOKEN_BUDGET=12000 已过时——route 成本可近线性下调

- `backend/app/pipeline/router.py:34-37`：切窗预算 12000 的依据是"端点 32000 上限"时代的实测（max 18924 也在限内）。#106（decisions-log）实测网关 ≥124643 token 可用、prefill 34ms/k tok 平缓，延迟大头在 thinking——**切窗预算没有跟着重校准**。
- 窗口数直接乘在"53s/次调用"这个固定成本上：12000→40000 约把窗口数降到 1/3，259 窗的量级从 ~3.8h 降到 ~1.3h，且 route 在写路径、不受 400ms 约束。
- 约束要先测：单次输出 max_tokens=4096（`llm.py:32`），窗口变大 → 单窗 items 变多 → 可能先撞输出上限或增加 parse 失败率。

**修法**：影子重放 20 个会话 × 预算 {12000, 40000}，对比窗口数 / parse 失败率 / 漏判抽样（用 1.4 的方法）；好则切换。这是当前 ROI 最高的一处调参。

### S8 评测集的三个盲区（方法论层）

96 条（正 94 / 负 2）作为**基线哨兵**合格，作为 **Phase 3 调参裁判**不够：

1. **规模与结构**：否定例 2 条无法检验负向泄漏的回归；没有专设"同项目同主题多条记忆的区分度"case（而 7.10 实测未命中模式② 恰恰是这类）。建议扩到 ~300 条：否定例 ≥30、近邻区分组 ≥30。
2. **同源偏置**：出题模型见过记忆库，出的是"干净"query；真实 query 在 `recall_log.query_text` 里躺着——长的、带代码粘贴的、口语化的。应从 recall_log 采 100 条真实 query 补题、人工标答案。真实分布进评测集之前，任何召回调优结论都要打折扣。
3. **只测排序，不测注入效果**：hit@5/MRR 是检索指标；系统的最终交付物是"注入后 agent 行为有没有变好"，这个维度目前零度量。低保真起步即可：recall-log UI 加"有用/没用"人工标记（用户本来就会翻这个页），负标记自动进 rule_miss 式抽样池。

### S9 人审吞吐没有扩展机制（旧 C2 完全未动，且压力在涨）

- 11-11 记录的积压口径是 route 41 + section 41 张待审卡；route-v2 全量重判后 observation 量还会涨（1.4 结论：漏判 38.2% 的卡会陆续回流人审）。
- UI 目前没有相似卡聚簇、没有键盘流、没有重复 observation 的自动放行。observation 100% 人审在数据量线性增长下人工时间也线性增长——这是产品闭环的下一个真瓶颈。
- 建议（全部是**减**人工方向，与"不新增人工卡点"的规矩同向）：
  1. 相似 observation 聚簇审（embedding 近邻分组，一次裁决一簇）；
  2. 键盘流（j/k/1/2/3 裁决，不打断视线）;
  3. 重复白名单：同一 section + 高相似 content 的 observation 第 N 次出现自动通过（negative 反例除外）——把"人审"集中到首见模式上。

---

## 三、设计权衡提醒（不是缺陷，但要心里有数）

- **mention 信号空转**：`mention_count` 只在写路径更新（`facts.py:286-292` NOOP/UPDATE 碰撞时 +），"被召回→被注入→真的帮到了"从不回写。排序权重里最活跃的信号从未见过使用数据；comment 侧的半衰期（90/180/365 天）、MENTION_CAP=1.6、SCOPE_WEIGHT（1.0/0.85/0.7）也全是先验拍定、从未被评测校准。评测集扩容后应做一轮敏感性扫描，允许结论是"不用动"。
- **bootstrap 的 user 记忆截断**：`bootstrap.py:67` user_memory_limit=20 按 last_seen_at DESC 取——user 级 profile 长过 20 条时静默丢弃最旧的，无任何告警。
- **排除数组无界增长**：`recall.py:212` 的 exclude 数组 = 调用方 200 上限 + session-born 无上界；长会话后期每轮召回拖一个大数组做 `id <> ALL(...)`，HNSW 迭代扫描候选被过滤浪费。个人量级无感，但这是"系统规模上限"的隐形项之一。
- **hook 侧 prompt 硬限**：API `max_length=4000`（`read.py:41`），超长 prompt 422 → hook 静默零注入。粘贴大段代码的轮次记忆系统等于不存在。hook 侧对超长 prompt 取头 2000 + 尾 1000 即可。
- **HTTP read 超时 180s vs route 实测均值 61s**：thinking 长的调用会逼近超时，一旦超时就落进 S3 的热重试循环。配合 S3 一起修。
- **会话状态文件卫生**：`session-{id}.json` 永不清理，cache 目录单调增长（微小，顺手做）。
- **合规边界明示**：个人库（含 personal org 的内容）与公司库内容同走 dida-gateway。单机 127.0.0.1 下风险可控，但这属于上一轮 C6 说的"要写成显式契约"的内容——建议在 plan 里补一行触发条件（接入任何非 dida 端点/多用户/云部署前必须先做什么）。

---

## 四、值得肯定、明确保持不动的

1. 三硬边界（读零 LLM / 写全异步 / 归属零 LLM）——两周实跑证明选型正确，勿动。
2. PG 队列 + SKIP LOCKED + lease + `started_at` 所有权令牌 + `dedup_hash` 幂等三件套——个人规模下教科书级，别换中间件。
3. `recall_log` 只存 id+分数（`recall.py:296-310`）、bootstrap 缓存只读不回写（`mem-hook.py:158-163`）——单一真相源纪律贯彻到了边角。
4. 503 不跳熔断（`mem-hook.py:102-105`）、parse 失败全部落待办不静默（`router.py:320-323`）——"静默失效是头号敌人"不是口号，代码里处处是证据。
5. 出题/审题分离的评测、影子重放回归、pytest 独立库——方法论资产，继续。

---

## 五、Roadmap（按性价比排序；全程不含新人工卡点）

### Phase A：小改动大收益（本周内，全部 ≤半天/项）

| # | 事项 | 对应 | 改动量 |
|---|------|------|--------|
| A0 | 写链路三急修：锚点/游标语义统一、_apply_all 校验候选集、write_facts 续租 + memory_events 幂等 | W1/W2/W3 | 各 ~5–10 行 |
| A1 | hook 三处修复：SessionStart 清 injected 状态；接 prev_assistant_summary；超长 prompt 截断 | S1/S2 | 各 ≤10 行 |
| A2 | jobs 加 next_retry_at 退避 + claim 过滤 | S3 | 1 列 + 2 处 SQL |
| A3 | route 逐窗 checkpoint（route_decisions 逐窗落库 + 重试跳过） | S4 | 中 |
| A4 | 保留期清理 job 落地（6 原文表 + recall_log/llm_usage/jobs/route_decisions 全纳管） | S6 | 1 个 handler |
| A5 | TOKEN_BUDGET 重校准实验（12k vs 40k 影子重放对比） | S7 | 1 个 bench 脚本 |

### Phase B：把反馈环闭上（2–4 周）

| # | 事项 | 对应 |
|---|------|------|
| B1 | 评测集扩容：recall_log 真实 query 100 条 + 否定例 30 + 近邻区分组 30；runner 进 service.ps1 常规动作 | S8 |
| B2 | 写入器角色墙：候选放宽到同 (scope,org,project) + 周期合并扫描 job | S5 |
| B3 | 人审吞吐：聚簇审 + 键盘流 + 重复白名单 | S9 |
| B4 | 注入有效性低保真闭环：recall UI 有用/没用标记 → 负标记进抽样池 | S8③ |
| B5 | 读侧质量三件：相关性闸门、召回命中回写使用信号（或 decay 下限）、query 向量 LRU 缓存 | R1/R2/R3 |

### Phase C：质量调优与规模化（1–2 月，每项必须过 B1 评测集 A/B 才合入）

| # | 事项 | 说明 |
|---|------|------|
| C1 | keywords 通道修复（旧 3.1） | 写入小写化+GIN、中文分词、词边界、IDF、2 字符 token、抽词配额；与 S1 联动（query 侧同样受 128 截断影响） |
| C2 | reranker 真实化（旧 3.2） | hf-mirror 重试下载 bge-reranker-v2-m3；A/B 四动作判对率 |
| C3 | sparse A/B（旧 3.3） | 允许结论是"不需要" |
| C4 | 排序参数敏感性扫描 | mention/半衰期/scope 权重第一次被数据校准 |
| C5 | confidence 校准（旧 3.5） | route_decisions 标注集做可靠性曲线 |
| C6 | embedding 模型迁移 runbook | 换模型 = 全表重嵌入 + 可能改 vector(1024) 维度——现在写好，避免将来停机 |
| C7 | replay 生产化 + 安全契约化（旧 3.6/3.7） | 含"个人库走公司网关"边界明示 |

### 仍然不建议做的（与上一轮一致）

消息队列中间件；多机同步/云部署；评测集扩容前调召回参数；恢复 living_doc 发布文件；为 confidence 发明新阈值。

---

## 六、两路独立专项审查整合（读路径 / 写入链路，subagent 报告）

以下为本报告正文未覆盖的**新增发现**（与 S1–S9 重叠处已去重：subagent 的"hook 不传 summary"＝S1、"重试无退避"＝S3；行号均经 subagent 实测、关键条目主审抽查属实）。

### 写入链路新增（W 系列，按严重度）

- **W1 ★★ 锚点漂移 → 游标卡死 → 永久静默漏路由（写链路最危险新发现）**：`pipeline/windows.py:80-82` flush 时 `anchor=bucket[0]` 但 `Window(anchor_id=chunk[0].id)`——锚点桶超 TOKEN_BUDGET=12000 切成多块后，第 2+ 块的 anchor_id 是块首行（常为 assistant/thinking 行）；配合 `router.py:278` 的 `w.anchor_id > last_log_id` 过滤与 `router.py:339/345/352` 用 anchor_id 推游标：长会话尾桶 p99=14403 token 实测存在，Stop-hook 扫描时机错开一轮即触发，同桶后续窗口 anchor_id ≤ 游标被**永久过滤**；行级扫描（router.py:429）仍排 job 但打开后无窗口可路由 → 无限空转。丢的是从未被判过的素材、无任何失败痕迹，与 parse-failed 落待办的兜底哲学（router.py:321）直接矛盾。修法（~10 行）：过滤改 `w.rows[-1].id > last_log_id`、done_through 取 `max(rows[-1].id)`、anchor_id 恢复 bucket[0]（顺带消除 route_decisions/pending_items 的 source_msg 指向非锚点行的污染）。
- **W2 ★★ `_apply_all` 无条件信任 LLM 输出的 memory_id**：`facts.py:276-349`——NOOP/UPDATE/DELETE 直接用 `decision.memory_id`，从不校验是否属于候选集（:154-176 查出的 20 条）。模型幻觉一个 id 即可穿透所有分区过滤，DELETE 可软删**跨 org/project 的任意 active 记忆**且静默、memory_events 审计链被污染。修法（~5 行）：`valid_ids = {r["id"] for r in fact.candidates}`，不在集合内按 unjudged→ADD 兜底 + log.warning；UPDATE/DELETE 查 rowcount==0 计异常。
- **W3 ★ write_facts 全程无续租 → lease 过期并发双执行**：多桶会话每桶一次 LLM（~53s/次）总时长轻松超 300s lease；route 有续租（worker.py:42）而 write_facts 没有（worker.py:50-53）。complete/fail 有 started_at 令牌不误标状态，但已提交 DB 写无法撤回：mention_count/revision_count 双加、memory_events 双写、DELETE 双执行（ADD 被 UNIQUE 挡住）。修法：仿 route 加 per-bucket 续租回调 + memory_events 加幂等约束（UNIQUE(memory_id, action, source_msg)）+ extraction_cursor upsert 补 GREATEST 单调保护（router.py:403-406）。
- **W4** summarize 分区循环无异常隔离（summarize.py:110-143 + ORDER BY n DESC :44）——最活跃分区故障即队头阻塞，整条汇总链停摆；修法 try/except 逐分区容错或每分区一个 job（dedup_key 已按分区收敛 :307）。
- **W5** 栏位匹配 embedding 冷启动发生在写事务内（router.py:142-143 → sections.py:70；review.py:228-231 同型）——15-33s idle-in-transaction，违反自家 §5.5/B2 纪律（db.py:65 注释明写）；修法：_decide 阶段先算好向量传入。
- **W6** UPDATE 整体替换 keywords（facts.py:298-304）——旧关键词被抹、召回面单调收缩不可逆；修法：新旧合并去重。
- **W7** workspace_map 缓存永不失效（attribution.py Resolver）——归属误判固化并随分桶传播，长期"召回莫名变差"；修法：规则变更清 `resolved_by<>'human'` 行。
- **W8** 人改判 atomic_fact 在无 project_key 会话被静默丢弃（review.py:171-182 兜底 scope=project，facts.py 跳过仅 warning）——人已批准的事实不落库、卡片标 done；修法：缺归属 bounce 回 pending 而非跳过。
- **W9** `_apply_accept`/`_apply_reject` 幂等检查在锁外（proposals.py:104-143 TOCTOU；living_doc_history.proposal_id 无 UNIQUE）——并发双执行 history 双行 version+2；修法：检查挪进 FOR UPDATE 之后 + 部分唯一索引。
- **轻微 16 项择机修**：zip 维度不匹配静默按短边截断（换 embedding 模型后分数悄悄错，sections.py:72）、sections 匹配 N+1 无缓存、RRF 候选截 20 砍半（facts.py:165）、UPDATE/NOOP 不查 rowcount、"强制汇总"被 dedup 静默吞（summarize.py:297-317）、negative 只喂最近 30 条、禁用 section 的观察行从列表消失（JOIN→LEFT JOIN）、跨多 jsonl 时 turn_no 重复（scan.py）、贴图消息正文被丢（filters 仅收 kinds=={"text"}）、parse_ts 的 Z 后缀（3.11+ 无碍）、ssh 端口泄入 remote_path + Windows 路径 casefold、handler 白占池连接 25%、claim 无优先级（回填饿死短 job）、write_facts dedup_key 含 cursor 可重复入队重复 LLM、LLMError 丢弃已成功窗口判定（已知取舍）。

### 读路径新增（R 系列，按严重度）

- **R1 ★ 无相关性门槛——"必然注入 5 条"**：`recall.py:259-293` score() 用批内最大 rrf 归一，top-1 恒 1.0，无余弦距离阈值/无 rrf 下限/无空结果一等公民——用户说"谢谢""继续"也注入 5 条最不相关记忆，600 token 预算被垃圾占用；"乱插话"比漏召回更伤信任。修法：dense 余弦阈值（用现有 eval 标定，0.5± 起步）+ 归一化分下限；注意 score() 注释声称归一化"跨 query 可比"实际相反（每 query top-1 恒 1.0），阈值要按未归一化距离做。
- **R2 ★ 衰减只认写入不认使用 → 长期记忆系统性沉底**（与正文"mention 空转"提醒同根，此处给出量化）：fact 半衰期 90d 算 last_seen_at（recall.py:34/:272-274），而它只在写路径 NOOP/UPDATE 时更新——12 个月未重写但一直被使用的 fact 衰减 2^-4≈0.06，MENTION_CAP=1.6 需 16× 才补得回，与 recall.py:39-40 "衰减降权≠遗忘"的注释意图正好相反。修法：召回命中回写 last_seen_at（或加 last_used_at/used_count 参与衰减）或 decay 下限 0.25。
- **R3** query 向量零缓存（embedding.py:163-164 encode_one 每次全量）——encode p95 239ms ≈ 预算 60%，重复/相似 prompt 每次重付全款；LRU(text→vector) 命中后 p95 可 <100ms。
- **R4** keywords 通道被自我削弱（合并 subagent H5/M1/M2）：词表子串匹配无词边界（keywords.py:79-81，"mem"命中"remember"）+ overlap 排名无 IDF + rrf_k=60 在 pool=50 下通道内名次区分度仅 1.8× 被压平（k=60 是为千级深池调的）；标识符正则 `{2,}` 漏 2 字符 token（keywords.py:23）——方案文档自己的例子"修复A2 A3"就是受害者；extract(limit=32) 保序切片会让 prev summary 的英文标识符挤死中文唯一依赖的词表通道（S1 修通后即触发）。
- **R5** 同 source_msg 挤占 K（写侧一窗口产多条 fact 共 source_msg，读侧无 per-source 配额/MMR/近重复抑制；config.py:100-105 自认"超集"与"重复"余弦分不开）——top-5 可能是同一规范的 3 个变体，多样性与信息覆盖双输。
- **R6** exclude_ids 硬排除反噬重复提问（mem-hook.py:220-221 + recall.py:44-48）——"刚才那条规矩是什么来着"恰是最该召回的时刻，该记忆被排除返回次优 4 条；eval 全部空 exclude 测的是理想化条件。修法：改降权 -50% 或与上轮注入高相似时放行。
- **R7** 评测用例钉死 memory id + prompt_ver 恒 NULL（evaluation.py:199-219/:242-245，补正文 S8）——DELETE/合并后旧行 id 失效 → 用例静默假阴性累积且无健康检查；eval_runs 无配置快照（k/pool/rrf_k/权重），跑批结果无法归因，A/B 无从做起。修法：expected 改存 content_hash + "expected id 非 active"健康检查 + 配置快照。
- **R8 工程细节**：HNSW 全默认、未设 hnsw.ef_search（默认 40 < pool 50，db.py:88-97 只设了 iterative_scan）；recall_log INSERT 与召回同事务且 latency 不含 commit（recall.py:216-227）——审计失败会打死召回；bootstrap 无审计无长度预算（bootstrap.py:35-41/:66-93，20 条长 pointer 可烧几千 token 且不可追溯）；"p95<400ms"是后端口径，hook 侧进程启动 ~150-255ms 不在测量内（L8，建议 hook 回传 total_ms）；Hit.text 不截断，单条超长记忆可独自烧光 600 token 预算（L1，服务端按 ~300 字截断）；平局按 id 升序 = 系统性偏老记忆（L2）；万条级前不用动：lower() 吃 GIN 索引（L3）、连接池参数、memory_recall 无 exclude 参数（L9）。

## 七、一句话总结

这套系统已经从"零件合格"走到"机器在跑"；但两路深审共同指认：**静默失效仍是最危险的短板**——写链路存在三条会静默丢数据/损坏数据的路径（W1 游标卡死、W2 幻觉 id 穿透、W3 lease 双跑），读链路把"该不该召回"留给了运气（R1 无相关性门槛、R2 只认写入不认使用的衰减）；叠加生产链路设计与实现的脱节（S1/S2）、健壮性欠账（S3/S4/S6）与"记忆有用"未度量（S8/S9）。下一步主线两条：先消灭静默失效（Phase A0 三急修），再把反馈环闭上——真实 query 进评测集、注入效果有信号、人审吞吐与数据量脱钩。
