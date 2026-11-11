# mem-xy 个人记忆系统 —— 架构评审与 Roadmap

> 评审日期：2026-11-09
> 评审范围：memory-system-plan.md（2157 行）、decisions-log.md（95+ 条决策）、backend/（71 个 py 文件，31 测试文件 / 447 用例）、frontend/（13 视图）、integrations/（hook + MCP）、service.ps1、bench/（9 个实测脚本）
> 评审方式：只读代码审计 + 规范/决策日志交叉核对，未修改任何文件、未运行服务

---

## 一、总体判断

这是一个**工程文化罕见诚实**的个人项目：三条硬边界（读路径零 LLM、写路径全异步、归属判定零 LLM）是正确的架构直觉；决策日志几乎每条结论都带实测数字和翻案记录；"静默失效是头号敌人"的方法论（debug 端点吐中间量、bench 必须 import 生产模块、真服务真库验收）超过多数商业团队。

但从行业视角看，系统当前处在一个危险的位置：**P0–P6 全部完成、447 测试全绿，而系统的核心价值链从未真正闭合**——没有接入任何真实 agent（hooks 未装）、没有效果评测集（hit@5/MRR 无法度量）、living_doc 真实数据长期为 0、数据库零备份。测试全绿证明的是"零件合格"，不是"机器能跑"。

问题按严重度分三层：**资产安全层**（会丢数据）、**架构一致性层**（自己违反自己的纪律）、**产品闭环层**（效果不可证伪、人审不可扩展）。

---

## 二、架构问题（按严重度排序）

### A 级：资产安全 —— 系统唯一资产处于裸奔状态

**A1. 数据库零备份。** 全仓库找不到任何 pg_dump/备份脚本或计划任务。195MB transcript 语料 + 全部 memories/living_doc/人工裁决结果是不可再生的（人工裁决是花真金白银的时间审出来的）。个人单机 + Windows + 无多机同步故事 = 机器即单点，一次磁盘故障或一次误操作 DELETE 就是全损。**这是全系统最便宜也最致命的缺口。**

**A2. 摄取层会静默丢数据 —— 层 0"真相源"本身漏。**
- `backend/app/ingest/filters.py:112-116`：assistant 记录 thinking+text 并存时 text 被丢，注释依赖"实测从不混块"的外部假设——Claude Code 一次版本升级就可能触发，且无任何告警；
- `backend/app/ingest/transcripts.py:59-60`：尾行不以 `\n` 结束即 return，完整 JSON 无结尾换行会漏采，直到下次追加才入库。
下游所有幂等、去重、校准都建立在"层 0 完整"的前提上，这个前提目前有洞。唯一回归哨兵是"真人输入长度 p50 涨回几千"，太粗。

**A3. 幂等缺口（decisions-log #70 自认未修）。** raw_observations / pending_items 无 UNIQUE 约束，队列重复消费是真损失（重复待办卡直接膨胀人审队列）；`backend/app/queue/jobs.py:87-92` complete() 不校验 lease 所有权——租约过期被回收后旧 worker 仍可标 done，存在重复执行窗口，目前全靠 memories 表硬幂等兜底，但那只兜得住最后一层。

**A4. 保留期清理未实现。** 6 张原文表定义了保留期但清理任务不存在（P7）。transcript 全量入库含 thinking，只进不出，隐私面和库体积同时单调增长。

### B 级：架构一致性 —— 项目违反了自己立的纪律

**B1. ★ 最大代码级隐患：LLM 调用被包进长事务。** `backend/app/router/router.py:206-220` 把整个会话路由（含 `router.py:264` 的 LLM 调用，实测 ~8 分钟）包在一个数据库事务里，池上限仅 8 连接（`backend/app/core/db.py`）。这与 plan §5.5 和 facts.py 自己遵守的"LLM 不进事务"纪律直接矛盾。后果：idle-in-transaction 持连接 8 分钟、WAL 膨胀、几个并发会话就能耗尽连接池拖垮读路径的 400ms 预算。

**B2. embedding 冷启动竞态。** `backend/app/core/embedding.py:64-112` 真正的 SentenceTransformer 加载在锁外，锁只护标志位 → 并发冷启动可双份加载 ~2.3GB 模型；`embedding.py:137-138` encode() 懒加载可能把调用方阻塞 15–33s。`backend/app/api/review.py:200,255` 甚至在**事务内**调 encode_one()（proposals.py 是正确示范：嵌入在事务外）——模型冷时该事务被阻塞 15–33s。

**B3. worker 吞吐自限。** run_worker 每处理完一个 job 都 sleep poll_interval=1.0s（而非仅空闲时 sleep），单 worker 吞吐上限 ~1 job/s。配合"单次 route ≈53s、259 窗口 ≈3.8 小时"的现实，全量回填的工期估算必须把这个常数算进去。

**B4. 单点依赖 dida-gateway，无退路且未探明。** 端点不可用不自动切换（#45，理由成立：防污染 confidence 校准），但代价是 job 积压无出路；且网关三项探测（上下文上限 / 并发限流 / 长 prompt 延迟曲线）至今未做，max_input_tokens=32000 是猜的。**并发能力是全量回填的硬前置**，这项不测，回填计划就是空中楼阁。

**B5. 无进程守护。** worker 由后端 lifespan 自启，但崩溃后无人拉起直到下次后端重启——注释里承认这种静默故障 2026-11-02 真实发生过（批准积压）。日志也无轮转：backend/frontend.log 每次启动 `>` 截断（历史证据直接销毁），worker-N.log 追加无上限。

### C 级：功能设计缺陷 —— 效果不可证伪、闭环未闭合

**C1. ★ 评测集空缺是方法论层面最大的缺陷。** eval_recall_cases 表建了、指标定义了（hit@5/MRR/p50/p95/改判率/漏判率），但**一条真实评测数据都没有**。后果已经在决策史里显形：阈值类决策反复横跳（0.95→分级→取消→0.85→0.0，#48/#51/#68/#90），每次都"有实测理由"，但因为缺一个事先建好的评测集，每次都在用局部小样本重新定标，无法回答"这次改动整体上让召回变好还是变坏"。**没有评测集，这个系统的所有优化都是不可证伪的**——这与项目自己"n≥3、跑不了写待测"的实证文化相悖，是灯下黑。

**C2. 人审吞吐是闭环的真瓶颈，且无扩展方案。** observation 100% 人审（#51 的结论正确：confidence 无区分度），但回填将产生 ~690 张待办卡，策略至今未定（§14 item3）。人审量从 24→255→690 一再膨胀。更根本的问题：**随着数据积累，100% 人审不可持续**，而系统没有设计降级路径（如：按 rule_miss 风险分层、高相似 observation 合并审、否决记忆反哺 few-shot 后逐步放行低风险类）。

**C3. #90 的静默风险敞口未固化。** 规矩被误判成 atomic_fact → 直接入库、永不注入 living_doc、无人发现。这是系统自定义的头号敌人"静默丢失规矩"的现实通道，目前只靠"rule_miss 抽样候选 + 两周观察点"，未成为固化机制。

**C4. 召回质量实际是单通道。** keywords 通道半残：大小写修复推迟 P7、GIN 索引失效、中文 query 实测只抽出 1 个英文词零贡献（C.13②）；reranker 是 stub（bge-reranker-v2-m3 下载失败，退化为 RRF 截断 top-5）；sparse 头被 ST 静默丢弃。设计上"两通道 + RRF"，**实际运行是 dense 单通道**。这未必是坏事（可能够用），但没人知道，因为 C1——没有评测集。

**C5. living_doc 闭环从未经历真实冷启动。** P5 只在合成数据上验过（C.12），真库 22 个栏位长期为 0。"空库 + 否决记忆收敛噪声"的核心产品假设（#46）未被任何真实数据检验。

**C6. 安全模型拆除后的债务无息累积。** #45 把 org/scope 降级为"检索分区非权限控制"在当前单机单用户下成立，但：公司仓库内容与公司规矩同库、同走 dida-gateway；token 明文曾泄露（#66 轮换只是"建议"）；未来若接入任何公共云端点，没有硬闸。#45 的恢复清单是唯一凭据，应升级为带触发条件的显式契约（"当 X 发生时必须先恢复 Y"）。

**C7. 平台耦合。** Windows 特性深入骨髓（::1 解析、Defender 拖慢 import、url2pathname）、摄取深绑 Claude Code JSONL 私有格式、source 适配层只有一个实现。作为个人项目可接受，但要在文档里明说"可移植性 = 0，迁移成本 = 重写摄取层"。

### 轻微问题（列出备查）

- `backend/app/api/ingest.py:21` 跨模块引用 read.py 私有 _error；
- `backend/app/core/keywords.py:79-80` 词表子串匹配，短词（"org"）可误命中长单词内部；
- config.get_settings() 全局缓存无 reload，改配置须重启（可接受但未在文档强调）；
- 测试薄弱区：无并发 race 测试（claim 竞争、complete vs reclaim、双加载）、renew_lease 无直测、无 400ms 性能回归；测试跑在活库上（#95 隔离方案脆弱，user 级记忆穿透分区）。

---

## 三、值得肯定的设计（保持，勿动）

1. **三条硬边界**是教科书级的正确取舍：读路径零 LLM 保延迟、写路径异步保吞吐、归属零 LLM 保确定性。
2. **PG 表队列 + FOR UPDATE SKIP LOCKED + lease + 续租回调**：对这个规模是恰到好处的选择，别换 Kafka/Redis。
3. **living_doc 唯一真相源在库、不发布文件**（#38）：避免了双真相源漂移这个多系统都栽过的坑。
4. **实测驱动文化**：n≥3 硬规矩、bench 必须 import 生产模块、测试总数对账发现静默覆盖 24 条（#89）、debug 端点吐中间量。
5. **决策日志只增不改 + 翻案留痕**：这是比代码更值钱的资产。

---

## 四、建议与 Roadmap

原则：**先保资产（备份/不丢数据）→ 再修一致性（事务/竞态）→ 再建度量（评测集）→ 再闭合产品环（接入/回填/ living_doc 真实冷启动）**。在评测集建成之前，冻结一切召回算法调优（sparse A/B、reranker、keywords 修复都往后放）——没有度量就没有调优，只有折腾。

### Phase 0：止血（1–2 天，全部是确定性修复）

| # | 事项 | 位置/做法 |
|---|------|-----------|
| 0.1 | **每日 pg_dump 备份** + 恢复演练（单机部署专用方案见 §五） | Windows 计划任务；`pg_dump -Fc` 保留 7 日 + 4 周；至少一份副本**离开这台机器**（公司内网盘或加密后上云）；恢复演练进临时库跑冒烟 |
| 0.2 | router.py 事务边界重构：LLM 调用移出事务 | `backend/app/router/router.py:206-220`；参照 facts.py 的"事务外调 LLM、事务内只写"模式 |
| 0.3 | 摄取两处丢数据 bug | `filters.py:112-116`（thinking+text 并存时保留 text 并打 WARN 计数）、`transcripts.py:59-60`（容忍无结尾换行的完整 JSON 行）；各加一条回归测试 |
| 0.4 | raw_observations/pending_items 补 UNIQUE（COALESCE 表达式索引，沿用 #41 方案）；complete() 校验 lease 所有权 | `backend/app/queue/jobs.py:87-92` |
| 0.5 | worker 崩溃自动拉起（最小 watchdog：计划任务每 5 分钟探 /healthz + workers_ctl.ensure）；日志改轮转不截断 | service.ps1 / workers_ctl.py:92 |
| 0.6 | embedding 加载纳入锁内（双检锁），review.py:200,255 的 encode 移到事务外 | `backend/app/core/embedding.py:64-112` |

### Phase 1：让效果可度量（1–2 周）——**本 roadmap 的核心**

| # | 事项 | 说明 |
|---|------|------|
| 1.1 | **建 eval_recall_cases（50–100 条）** | 从真实语料抽：每条 = (query, 期望命中的 memory ids)。来源：已入库的 255 窗口 + 人工构造典型场景（跨会话偏好、项目规矩、否定记忆不召回）。这是后续一切调优的唯一裁判 |
| 1.2 | eval 跑批脚本 + 基线快照 | hit@5 / MRR / p50 / p95，进 CI；同时加一条 400ms 性能预算回归（真实 PG + 真实模型） |
| 1.3 | ~~网关三项探测~~ **已完成（#106，见 7.11）** | 上下文实测 ≥124k 可用（配置回填 120000）；并发零 429、并行 2 甜点、回填可开 4 worker；延迟大头在 thinking 不在输入长度 |
| 1.4 | ~~规矩漏判率人工抽样 30 条~~ **已完成（#108，见 7.12）** | 候选池 34 条全量判定（AI 代判，用户授权，每条附理由）：**净漏判率 38.2%（13/34）、宽口径 47.1%**。触发 §10 预设动作：修 route prompt（few-shot 区分"用户确立的规矩 vs 技术事实"），方案待用户确认 |
| 1.5 | run_worker sleep 语义修正 | 仅空闲时 sleep，处理完立即取下一个 |

### Phase 2：闭合产品环（2–4 周）

| # | 事项 | 说明 |
|---|------|------|
| 2.1 | 接入 Claude Code（§14 item4 授权决策） | 建议先只装 SessionStart bootstrap（一次性 ~2500 token，零每轮延迟），跑一周看否决记忆收敛情况，再决定是否装每轮 recall（+290ms/轮） |
| 2.2 | 回填策略定案并执行 | 建议采纳 §14 的方案 C：回填期 atomic_fact 全自动、只审 observation（690→~230 卡）；配合 1.3 的并发结论排工期 |
| 2.3 | living_doc 真实冷启动 | 回填跑完后观察：22 栏位哪些自然长出内容、噪声期多长、否决记忆是否真的收敛——这是 #46 核心假设的第一次真实检验 |
| 2.4 | rule_miss 机制固化 | 从"两周观察点"升级为常驻队列：含规范性措辞（必须/禁止/总是/不要）的 atomic_fact 自动进 rule_miss 抽样池 + UI 一键改判 |
| 2.5 | 保留期清理实现（6 张表） | P7 遗留，Phase 2 做完数据开始真实增长，此时不清理就开始欠隐私债 |

### Phase 3：质量调优与规模化（1–2 月，**每项都必须用 1.1 的评测集 A/B 后才合入**）

| # | 事项 | 说明 |
|---|------|------|
| 3.1 | keywords 通道修复 | 写入小写化 + GIN 恢复、中文分词（jieba 或简易 n-gram）、词表子串匹配改词边界；A/B 验证 RRF 双通道是否真的优于 dense 单通道——**允许结论是"不需要"** |
| 3.2 | reranker 真实化 | 经 hf-mirror 重试下载 bge-reranker-v2-m3（写路径不受 400ms 约束）；A/B 四动作判对率 |
| 3.3 | sparse 通道 A/B | P7 遗留，同样以评测集为准 |
| 3.4 | 人审吞吐扩展 | 高相似 observation 合并审、按 rule_miss 风险分层、否决记忆反哺 few-shot（corrected few-shot 是 P7 遗留）；目标：人审量与数据量脱钩 |
| 3.5 | confidence 校准 | 用 route_decisions 标注集做可靠性曲线，让"阈值"这个词第一次有含义 |
| 3.6 | replay shadow 重跑生产化 | replay 表至今无生产写入；接入真实 agent 后这是唯一安全的"改 prompt 不炸生产"通道 |
| 3.7 | 安全债务契约化 | 把 #45 恢复清单升级为显式触发契约（接入任何非 dida 端点 / 多用户 / 云端部署前必须先恢复的清单）；token 轮换从"建议"变执行 |
| 3.8 | 第二 source 适配器（可选） | Qoder 或其他 agent；只在真实需要时做，做了才证明 #36 的适配层抽象是对的 |

### 明确不建议做的事

1. **不要引入消息队列中间件**（Kafka/Redis/Celery）：PG 队列 + lease 在这个规模是正确且可调试的。
2. **不要做多机同步/云端部署**：个人单机定位下，备份 + 迁移 runbook 就够了。
3. **不要在评测集建成前调任何召回参数**：那是折腾不是优化。
4. **不要恢复 living_doc 发布回文件**（#38 已正确否决）：唯一真相源原则值得守。
5. **不要为 confidence 再发明新阈值**：在 3.5 校准做完前，它的正确用法就是不用它。

---

## 五、单机部署约束下的备份与架构收敛（2026-11-09 用户确认：仅本机部署）

"只在本机部署"改变两件事：**备份的目标不再是异地多活，而是"这台机器可被替换"**；**架构的目标不再是水平扩展，而是把常驻部件数压到最少**。

### 5.1 备份：三层防线，核心是"副本离机"

单机部署下，最大的敌人不是服务宕机，而是**磁盘损坏、机器丢失/报废、勒索软件**——三者都会同时带走数据库和它的本地备份。因此：

| 层 | 做法 | 要点 |
|---|------|------|
| L1 逻辑备份（每日） | 计划任务 `pg_dump -Fc memxy`，保留 7 份每日 + 4 份每周 | -Fc 压缩格式，恢复可并行；写到非系统盘 |
| L2 离机副本（每日随 L1） | 备份文件同步到**公司内网盘/内部共享**（首选），或加密（7z AES-256 / age）后上网盘 | 库里含公司仓库 transcript 与 thinking 全文，走内网盘与 dida 数据合规一致；若必须用公共云，先加密再上传，密钥不进备份 |
| L3 恢复演练（每季度） | 恢复到临时库 → 对账关键表行数 → 跑一次 /recall 冒烟 → 结果记入 decisions-log | 备份的价值 = 成功恢复的次数；没演练过的备份等于没有 |

配套两条：
- **`~/.memxy`（secrets.toml、config.toml、cache/）纳入同一备份**，否则新机器恢复不完整；
- WAL 归档（RPO 从 24h 缩到分钟级）列为可选——个人系统里最贵的是人工裁决产物，若接受"最多丢一天新数据"则不必做，简单优先。
- **把备份做成系统的一等公民**：service.ps1 增加 `backup` / `restore` / `verify-backup` 子命令，而不是游离的手工约定。

### 5.2 架构收敛：机器即单点 → 让单点可替换、部件尽量少

1. **迁移 runbook（替代多机同步的正确方案）**：一份文档 + 一个脚本，目标是新 Windows 机器 **30 分钟内恢复服务**：装 PG+pgvector → restore pg_dump → 恢复 ~/.memxy → pip install → 模型预热（HF_ENDPOINT=hf-mirror.com）→ npm ci && build → service.ps1 start → /healthz 200 + /recall 冒烟。每季度随恢复演练跑一遍，保证 runbook 不腐烂。
2. **进程收敛**：~~frontend 目前是常驻 `npm run dev`（Vite）。单机个人使用建议改为 **构建静态产物由 FastAPI mount 托管**，少一个常驻 Node 进程 = 少一类静默故障 + 释放内存。开发时仍可切回 dev server。~~ **已完成（2026-11-10，#105）**：backend mount frontend/dist，UI 与 API 同源 http://127.0.0.1:8787/；dev frontend 仅改前端代码时手动启停；service.ps1 带 dist 新鲜度检查。
3. **守护用 Windows 原生方案**：计划任务每 5 分钟探 /healthz + workers_ctl.ensure，不引入 NSSM/容器——单机 Windows 下这是运维成本最低的守护。
4. **网络面确认**：backend 只绑 127.0.0.1（确认 serve 未监听 0.0.0.0），localhost-only 即天然免鉴权免 TLS；这也让 #45"暂不做安全权限"的决策在单机边界内继续成立。
5. **磁盘治理升格**：单机磁盘是有限且唯一的资源——日志轮转（worker-N.log 目前追加无上限，是最可能静默吃满盘的部件）和保留期清理（6 张原文表）从"卫生问题"升格为"单机可用性前提"。
6. **把约束写进 plan**：在 memory-system-plan.md 显式记录"本系统仅单机部署，不考虑多机同步/HA/云部署"及其推论（备份策略、127.0.0.1 绑定、无鉴权）。防止未来给系统加不必要的分布式复杂度，也让 #45 的安全债务边界清晰可查。

## 六、一句话总结

这个系统的**工程纪律是 A 级的，架构选型是 B+ 级的，但产品闭环是 D 级的**——最值钱的资产（数据）没有备份，最核心的假设（记忆有用）没有评测，最重要的用户（agent）还没接入。Roadmap 的主线只有一条：**先让数据死不了，再让效果量得出，最后让环转起来**；算法调优全部押后到有裁判之后。

---

## 七、实施记录（2026-11-09，供下个会话接续）

### 7.1 已完成的代码改动

| 文件 | 改动 | 状态 |
|------|------|------|
| `backup.ps1`（新增，约 430 行） | Phase 0.1 备份方案完整实现：`init / backup / verify / restore / status / install-task / uninstall-task` 七个子命令 | **已写入，未真机验证** |
| `service.ps1` | 新增 `backup` / `verify-backup` / `restore` 三个 Action（委托 backup.ps1，`ValueFromRemainingArguments` 透传剩余参数）；usage 注释与 Show-Help 已更新 | **已写入，未真机验证** |
| `EXPERT-REVIEW.md` | 本评审报告（含 §五 单机部署约束下的备份与架构收敛） | 完成 |

### 7.2 backup.ps1 设计要点（改代码前先读）

- **三层防线**：L1 本地每日 `pg_dump -Fc` → L2 离机副本（GitHub 私有仓库）→ L3 季度恢复演练（`verify`）。
- **加密是离机硬闸**：7z AES-256 分卷 50MB（`-mhe=on`，避开 GitHub 100MB 单文件限制），密码在 `~/.memxy/secrets.toml [backup] password`（`init` 自动生成 32 字节 base64）或环境变量 `MEM_BACKUP_PASSWORD`。**无 7z/无密码时允许本地明文 dump，但 `Sync-Offsite` 拒绝推送明文**。
- **GitHub 同步方式**：`~/.memxy/backup-repo` 本地暂存仓 + orphan 分支 `backup` + force-push 单提交（历史不膨胀）；每次重写全部保留集文件 + `manifest.json`（含每文件 sha256）。push 失败**大声抛错**（红字 + 手动重推命令），不静默。
- **保留策略**：最近 7 天各留最新 + 最近 4 周各留最新（ISO 周，Monday/FirstFourDayWeek），按 stem `memxy-yyyyMMdd-HHmmss` 归组删除。
- **~/.memxy 的 config.toml + secrets.toml 一并打进加密卷**（新机器恢复不缺配置）。
- **DSN 解析与 `backend/app/core/config.py` 同序**：`MEM_DSN` 环境变量 > `~/.memxy/config.toml` 里第一个 `dsn = "..."` > 默认 `host=127.0.0.1 port=5432 dbname=memxy user=postgres`。生产库名从 DSN 提取；`restore -DbName <生产库名>` 必须 `-Force`，覆盖用 `pg_restore --clean --if-exists`。
- **verify 流程**：解密最新备份 → `CREATE DATABASE memxy_verify_<ts>` → pg_restore `--no-owner --no-privileges` → 对账 6 张表（conversation_log/memories/raw_observations/pending_items/living_doc/jobs）restored ≤ live 且 restored>0（live>0 时）→ finally DROP 临时库。
- **计划任务**：`install-task` 用 `Register-ScheduledTask`（当前解释器路径 `(Get-Process -Id $PID).Path`，每日默认 12:30，任务名 `memxy-daily-backup`）；`status` 会检查 `LastTaskResult` 非 0 并红字告警。
- 备份目录默认 `~/.memxy/backups`（`MEM_BACKUP_DIR` 可指非系统盘）；pg 工具发现顺序：PATH → `C:\Program Files\PostgreSQL\*\bin`（降序取最新）；7z：PATH → `C:\Program Files\7-Zip\7z.exe`。

### 7.3 未完成 / 待验证清单（下个会话从这里接）

1. ~~**pwsh 在本会话被沙箱前置检查挡住，所有脚本未经真机验证**~~ **已解决（2026-11-09 第二轮会话）**：该会话中 `powershell`/`pwsh` 均可用。三个脚本（backup.ps1 / watchdog.ps1 / service.ps1）已过 PS 5.1 与 pwsh 7 双 AST 解析器（0 语法错误）；`backup.ps1 help/status`、`watchdog.ps1`（→ `/v1/workers/ensure` 全链路 200）、`service.ps1 restart backend` 均真跑通过。
2. 待跑验证（顺序）——**2026-11-10 深水区已跑通（除 push 与 install-task），过程修掉 5 个 bug，见 7.6**：
   - [x] `.\backup.ps1 init`：依赖检查输出、密码写入 secrets.toml 格式（`[backup]` 段追加逻辑、已有段时的跳过分支）——全部通过
   - [x] `.\backup.ps1 backup`：dump → 加密分卷 → 明文删除 → 保留策略——通过；GitHub push 仍未验（需先建空的私有仓库并 `-GitRemote` 绑定）
   - [x] `.\backup.ps1 verify`：临时库对账全流程（含 finally 清理）——通过，六表对账全 OK
   - [x] `.\service.ps1 backup` / `verify-backup` / `restore` 参数透传——通过（restore 缺参友好报错、verify-backup 全流程）；`restore -DbName x -Force` 的真实覆盖路径未验（破坏性，留给真灾难场景）
   - [x] `.\backup.ps1 install-task`：**已注册→真触发→确认不可行→已卸载**（2026-11-10）——SASE 应用控制策略禁止 pg_dump/psql 在计划任务上下文执行（Win32 1260，连 `--version` 都拦；7z/git/python/pwsh 放行），计划任务模式在本机被策略封死。每日备份改为 **backend 内置调度**（`app/backup_daily.py`，默认 12:30 触发、幂等补跑、失败指数退避），详见 decisions-log #100；正路是申请 SASE 白名单后切回 install-task（代码已保留并加好任务日志重定向）。
   - [x] `.\service.ps1 install-watchdog` 注册 + `schtasks /Run` 手动触发（2026-11-10 完成：Last Result 0，每 5 分钟无限期重复；踩坑记录见 decisions-log #98）
3. 已知未验证风险点：
   - GitHub push 在该网络环境（HF 主站被墙）下稳定性未知；SSH 超时可换 HTTPS + credential manager，或把 `Sync-Offsite` 目标改成公司内网盘路径（离机副本已独立成单函数，换目标只动它）。
   - `Register-ScheduledTask` 不带 `-User` 时是否需要交互确认，未实测。
   - ~~语法整体按 PS 5.1/pwsh 7 双兼容写，但**没跑过解析器**~~ 已跑双解析器，0 错误（见第 1 条）。
4. 用户需自备：GitHub 空的私有备份仓库（建议名 `memxy-backup`，不与代码仓库混用）；7z（`winget install 7zip.7zip`）；抄一份加密密码到物理安全处（**密码丢失 = 备份全灭**）。

### 7.4 与 Roadmap 的对应

- 第一轮落地 = **Phase 0.1（备份）** 的代码部分；0.1 的"恢复演练"要等 `verify` 真机跑通才算闭环。
- ~~Phase 0 其余项均未动~~ **第二轮（同日）已全部落地**：0.2 router 事务重构、0.3 摄取丢数据、0.4 幂等缺口（pending_items dedup_hash + lease 所有权令牌）、0.5 worker 守护 + 日志轮转、0.6 embedding 双检锁、1.5 run_worker sleep 语义——详见 7.5。
- ~~§5.2 的进程收敛（FastAPI 托管前端静态产物）、迁移 runbook 文档化：**未动**~~ **两项均已完成**：RUNBOOK.md（2026-11-10，备份/恢复/换机/故障排查全路径）；进程收敛（#105，backend 托管 frontend/dist，项目同时首次建 git 仓库推送 not-happy/mem-xy）。§5.2 其余项现状：3（watchdog 计划任务）、4（默认绑 127.0.0.1）已落地；5 的日志轮转已做（20MB 滚动）、**原文表保留期清理未做**；6（plan 显式记录单机约束）**未做**。
- ~~Phase 1 未动~~ **Phase 1（1.1–1.5）已全部收官**：1.1（评测集 96 条）+ 1.2（runner + 基线 hit@5=0.904 / MRR@10=0.686）见 7.10；1.3（网关探测）见 7.11；~~1.4 待人工判定~~ **1.4 已完成（#108，见 7.12）：净漏判率 38.2%（13/34）、宽口径 47.1%**。

### 7.5 第二轮实施记录（2026-11-09，Phase 0 收尾 + 全量验证）

**代码改动**（评审项 → 文件）：

| 评审项 | 文件 | 改动 |
|--------|------|------|
| B1（0.2） | `backend/app/pipeline/router.py` | `route_session` 三段式重构：读（短连接）→ LLM（无事务收集 `_Outcome`）→ 写（单短事务）。8 分钟 idle-in-transaction 消灭；`conn=` 参数保留为测试/CLI 兼容路径；`route_all` 不再包外层事务 |
| B1 同款 | `backend/app/pipeline/review.py` | `apply_decision` 的 encode/living_doc 查询移到事务外（`_prepare_vectors`），事务内只写 |
| A3（0.4） | `router.py` + `alembic/versions/0005_pending_dedup.py` | `pending_items.dedup_hash`（sha256(prompt_ver\|source_msg\|kind\|content)）+ 部分唯一索引（**已应用真库**）；`_queue_for_review` / `_insert_parse_failure` 带 `ON CONFLICT … DO NOTHING` |
| A4（0.4） | `backend/app/queue/jobs.py` + `worker.py` | `complete/fail/renew_lease` 带 `started_at` 所有权令牌；租约被回收后旧 worker 的写入全部落空（fail 返回 `"stolen"`） |
| A2（0.3） | `backend/app/ingest/filters.py` | thinking+text 混块不再静默丢 text：有信号词拼接入库，无信号词保 text，配 WARN 哨兵 |
| A2（0.3） | `backend/app/ingest/transcripts.py` | 尾行无 `\n` 先试解析——完整 JSON 照采，半行才跳过 |
| B2（0.6） | `backend/app/core/embedding.py` | `load()` 整体持锁（双检锁），并发冷启动只加载一份模型；`loading` property 改无锁读（否则 /healthz 被阻塞 15–33s） |
| B5（0.5） | `watchdog.ps1`（新增）+ `service.ps1` + `workers_ctl.py` + `api/workers.py` | 计划任务守护（install-watchdog，已注册并触发验证）+ `POST /v1/workers/ensure` 幂等补齐 + worker-N.log/service 日志启动轮转（20MB→.old）+ `Start-Launcher` 改追加不再截断 |
| 1.5 | `jobs.py` + `commands/extract.py` | `run_worker` 删掉每 job 后 sleep（自限 ~1 job/s），只在 claim 不到时 sleep idle |

**测试**：新增 12 个回归用例（transcripts 尾行 ×2、filters 混块 ×2、jobs 所有权 ×3、embedding 并发 ×1、router 自管事务/dedup ×4），另改造/适配 3 个（filters 混块旧用例随行为更新、proposals `_blocked_by` 签名适配、living_doc 断言改前缀过滤修复基线失败）。全量 `pytest tests -q`：**474 passed**（连跑 3 遍稳定）。

**评审报告勘误**（引用时注意）：
- 路径笔误：router 在 `app/pipeline/router.py`（非 `app/router/router.py`）；review 在 `app/pipeline/review.py`（非 `api/review.py`）。
- A3 半不实：`raw_observations` **已有** `UNIQUE(source_msg, content_hash)`（0001 schema 就有），缺幂等键的只有 `pending_items`。

**实施中的新发现**（评审没覆盖，详见 decisions-log #96/#97）：
- 部分唯一索引的 `ON CONFLICT (col) DO NOTHING` 必须带索引同款 `WHERE` 谓词，否则 `InvalidColumnReference`。
- **pytest 的 jobs 队列测试与常驻 worker 进程抢 job**：`claim` 是全局竞争、无 org 前缀可隔离，`clean_jobs` 的 `DELETE FROM jobs` 还会删掉生产队列——#95 的分区方案对 jobs 表无效。本轮验证是先 `workers_ctl.stop_all()` 再跑测试、跑完 `ensure(2)` 恢复。~~根治（独立测试库）待决策。~~ **已根治（2026-11-10，decisions-log #103）**：pytest 全套跑在独立测试库 `memxy_test`（conftest 收集期 DROP+CREATE+迁移+补种，`MEM_DSN` 指过去），与生产数据物理隔离——跑测试不再停 worker，`clean_jobs` 全表删只作用于测试库，#95 分区降级为第二道防线。验收：worker 在线不停，全量连跑 3 遍 492/492/492 passed 零 flaky；生产库 `jobs` 行数全程不受测试触碰。唯一例外是 test_mcp_server（真端到端，写入经 HTTP 落在服务连的生产库），其清理 fixture 已改为直连 `conftest.SERVICE_DSN`，按 `MARK` 前缀定向删。

### 7.6 第三轮实施记录（2026-11-10，备份体系真机验证深水区）

7.3 第 2 条的 init / backup / verify / service.ps1 透传全部真机跑通，**过程修掉 backup.ps1 五个 bug**（详细取证见 decisions-log #99）：

| # | bug | 后果 | 修复 |
|---|-----|------|------|
| 1 | pg 工具发现只扫 PATH + C 盘 | 本机 PG 18 装在 D 盘，init 报缺工具 | `Get-PgProbeDirs`：postgres 进程 Path / postgresql% 服务注册路径 / 各固定盘枚举（版本降序） |
| 2 | `Get-Dsn` 漏 secrets.toml（dsn 实际在 `[db]` 段） | 落回无密码默认 DSN，pg_dump 挂交互式密码提示 600s | 解析顺序对齐 config.py（env > secrets.toml > config.toml > 默认）+ 所有 pg 调用 `--no-password` fail-fast |
| 3 | `ParseExact` 的 `Substring(5)` off-by-one（前缀 "memxy-" 是 6 字符） | 时间戳解析抛异常，保留策略失效 | `Substring($ArchivePrefix.Length)` |
| 4 | ★ `Apply-Retention` 的 `Group-Object` 没加 `-AsHashTable -AsString`（单组时返回 GroupInfo 对象，`.Keys` 索引全空） | **keep 集为空 → 删光当天全部备份（实删）** | 加 `-AsHashTable -AsString` + keep 集为空则拒删并红字告警（删除逻辑的 fail-safe） |
| 5 | `Invoke-Native` 的 stdout 进管道污染 return；throw 信息泄漏 DSN 明文密码 | pg_restore 收到 7z 输出当文件名；错误日志带密码 | `\| Out-Host` 隔离输出；参数脱敏（`password=`/`-p` 打码）后再抛 |

**验证结果**：
- `init`：依赖检查（含 D 盘工具发现）、密码生成写入 secrets.toml、已存在跳过幂等——通过。
- `backup` ×2：pg_dump 6.6MB → 7z AES-256 分卷 → 明文删除 → 保留策略两组共存零误删 → 未绑仓库正确跳过 push——通过。
- `verify`：解密 → 临时库 → pg_restore → 六表对账全 OK（conversation_log 5976/5976、raw_observations 31/31、pending_items 124/124、living_doc 4/4、jobs 64/64、memories 17≤18）→ finally DROP——通过。
- `service.ps1` 透传：`restore` 缺 `-DbName` 友好报错、`verify-backup` 全流程 exit 0——通过。

**方法论教训**（呼应 §四 Phase 0 的定位）：430 行只过语法解析器、从未执行过的脚本藏了 5 个 bug，其中保留策略那个直接毁数据——"代码写完"与"备份闭环"之间隔着真机全链路 + 恢复演练，这正是 0.1 当初把 verify 定为闭环条件的原因。

**剩余待用户**：~~GitHub 私仓 + `init -GitRemote` 绑定后验 push~~、~~加密密码抄送物理安全处~~——**均已完成（见下）**；`install-task` 被 SASE 封死（见下），每日备份改 backend 内置调度（用户已决定不申请白名单，内置调度为长期方案，见 7.8）。

### 7.7 GitHub 绑定验证与 SASE 拦截（2026-11-10 后续）

**L2 离机副本闭环**：用户建好私有仓 `not-happy/memxy-backup`，`init -GitRemote` 绑定后真验 push 成功——远端 orphan 分支 `backup` 单提交 force-push（3 分卷 + manifest.json 含逐文件 sha256，与本地 `sha256sum` 核对一致）；匿名 API 404 确认仓库私有；密码已由用户抄送物理安全处。

**新发现的硬约束（★ 环境级，非代码 bug）**：`install-task` 注册的每日备份任务触发即失败（LastTaskResult=1），同款命令行交互跑却成功。诊断任务取证（任务上下文逐个跑 `--version`）：**SASE 应用控制策略禁止 pg_dump/psql 等 PG 客户端工具在无人值守上下文执行**（Win32 1260「管理员用策略规则限制了访问」，纯 exe×上下文策略而非行为检测）；7z/git/python/pwsh 均放行——这也解释了 watchdog 任务一直正常（只跑 pwsh + HTTP）。

**应对（decisions-log #100）**：
- 每日备份改为 **backend 内置调度线程**（`backend/app/backup_daily.py`，lifespan 启动）：`backup.daily_at`（默认 "12:30"，`off` 关，env `MEM_BACKUP_DAILY_AT` 覆盖）之后首次检查若无今日备份文件即补跑（幂等）；失败指数退避（600s×2^n 封顶 4h）+ ERROR 大声日志；执行链 python → powershell.exe → backup.ps1 → pg_dump，与手动成功的交互链同构。
- memxy-daily-backup 计划任务已卸载（已知必失败的自动任务 = 每天红字误报）；install-task 代码保留并顺手修掉"任务失败零输出"（Action 改为全流重定向到 backup-task.log）。
- 已知边界：机器重启后 backend 由 watchdog 任务链拉起，届时 pg_dump 可能被同策略拦（有 ERROR 日志 + 交互重启即恢复）；~~根治 = SASE 白名单申请（待用户）~~ **用户已决定不申请白名单（2026-11-10），内置调度即长期方案**；若重启边界真实造成困扰，备选加固 = postgres 服务进程代跑 pg_dump（`COPY TO PROGRAM`，执行上下文与调用方解耦），见 #101。
- 单测 18 个全过；~~真实链路验证：调度自动触发 → 新分卷 + push 成功~~ **勘误（见 7.8）**：调度首跑（11:46）实际是**失败**的——但败因不是 SASE，而是 backup.ps1 两个 PS 5.1 环境 bug；调度机制本身（准点触发、pg_dump/7z/保留策略在 backend 链全通）验证成立。修复后同款链路 exit 0。

### 7.8 内置调度真跑暴露的两个 PS 5.1 bug 与修复（2026-11-10 第三轮）

7.7 的调度验证当时记成"成功"，实际 backend 内首跑失败（exit 1）。取证后确认**方案选型没错、败在 backup.ps1 从未在 powershell 5.1 真跑过**（此前手动验证全用 pwsh 7，"双兼容"只过了 AST 解析器）：

| # | bug | 现场 | 修复 |
|---|-----|------|------|
| 1 | `$ErrorActionPreference='Stop'` 下 native 命令做 stderr 重定向（`2>&1`/`2>$null`），每行 stderr 被包成 NativeCommandError 直接 throw；**git push 成功时也往 stderr 写进度** → push 明明成功（远端已更新）脚本却误报失败 | backend 调度链复现：exit 1 + 远端 commit 已更新 | 新增 `Invoke-NativeMuted`（临时放宽 EAP，合并输出为字符串，成败查 `$LASTEXITCODE`），替换全部 12 处 native stderr 重定向 |
| 2 | `Get-FileHash` 在 backend 派生的 powershell 子进程里 CommandNotFound（交互链无法复现，疑模块自动加载环境差异） | 11:46 调度首跑挂在 manifest 计算 | 换 `Get-FileSha256`（.NET `SHA256::Create()` 直算，零模块依赖） |

配套修正：`run_backup_once` 的 ERROR 文案去掉"必是 SASE 拦截"的错误断言（本次失败恰不是）；`install-task` 警告与模块 docstring 按"不申请白名单、内置调度为长期方案"更新口径。

**验证**：backend 同款链路（python → powershell.exe 5.1 → backup.ps1 backup）`run_backup_once()` 返回 True，push [OK]；`test_backup_daily` 18 passed；backend 已按默认 `backup.daily_at=12:30` 重启，调度线程在跑。

**顺带记录**：保留策略实际语义是"保留的日/周内**全部**备份都留"（当日多份共存），比帮助文本"各留最新"更宽——方向是多留不是多删（安全），未改。

### 7.9 第四轮实施记录（2026-11-10，#97 根治：pytest 独立测试库）

7.5 遗留的「根治（独立测试库）待决策」经用户批准后实施完毕（详细取证见 decisions-log #103）：

| 改动 | 内容 |
|---|---|
| `backend/conftest.py` | 收集期重建测试库：连 postgres 维护库 `DROP DATABASE IF EXISTS memxy_test WITH (FORCE)` + `CREATE` → alembic upgrade head（子进程跑，隔离 fileConfig 对 caplog 的干扰）→ `seed.seed_all(with_embeddings=False)` 补种 → `MEM_DSN` 指向测试库。**安全闸**：只有 dbname==`memxy_test` 才重建，`MEM_TEST_DSN` 指定其他库只补迁移；初始化失败大声抛错，绝不降级回生产库 |
| `tests/test_mcp_server.py` | `db` fixture 不再用 app.core.db 全局池（现指向测试库），改 psycopg 直连 `conftest.SERVICE_DSN`（覆盖 MEM_DSN 前存下的生产 DSN）建局部 facade——端到端写入经 HTTP 落在服务库，清理必须对准同一个库 |

**两个坑**：① 种子数据（orgs/endpoints/match_rules/doc_sections 22 栏位）由 bootstrap 写入、**不在迁移里**，只跑迁移则外键全炸——首跑 69 failed，补种后降到 3；② 剩余 3 failed 即上述"库分裂"：清理对着测试库做，生产库累积 7 条 `pytest-mcp-` 残留（`assert 7 == 1`、409 Conflict），fixture 修复后首跑自动清光。

**验收（worker PID 27132/21736 全程在线未停）**：全量 pytest 连跑 3 遍 **492/492/492 passed 零 flaky**（旧方案复现率 ~1/10–1/20 且失败点漂移）；生产库 `jobs`=117 稳定不再被测试触碰（`clean_jobs` 的全表 DELETE 现在只作用测试库）；`pending_items`/`raw_observations` 零 MARK 残留。跑测试从此**不需要** `stop_all`/`ensure` 前置步骤。#95 的 org 前缀分区降级为第二道防线。

### 7.10 第五轮实施记录（2026-11-10，Phase 1.1 + 1.2：评测集与首条基线）

C1（"评测集空缺是方法论层面最大的缺陷"）就此关闭：`eval_recall_cases` 从 0 行到 **96 条**（正例 94 / 否定例 2），runner 跑通并落 `eval_runs #1`。详细取证见 decisions-log #104。

| 组件 | 内容 |
|---|---|
| `alembic 0006` | `eval_recall_cases.must_not_ids BIGINT[]`——否定例的显式语义（串台、已删记忆复活），已应用生产库 |
| `app/evaluation.py` | 指标纯函数 + 候选清单行协议（markdown 可人编辑）+ 幂等导入 + `run_eval`（`write_log=False` 不污染 recall_log；预热后计时） |
| `app/commands/eval.py` | `--gen-candidates` / `--import` / `--run` / `--show` |
| `app/prompts/eval_gen.py` | 出题 prompt：换说法、不照抄特征词、答案唯一指向 |
| metrics + `Metrics.jsx` | 跑过 runner 后从"待测"变为显示真实 hit@5 / MRR / p95 与跑批时间 |

**出题与审题由两个不同模型完成**（出题走 dida 网关 qwen，审题逐条对照记忆全文），避免模型给自己当裁判。审题改 4 处：1 条带错误前提改写、1 条口吻修正、2 条 expect 改多 id（#2368/#2381 内容重叠，命中任一都算对）。

**基线**（真 bge-m3 + 真生产库）：

| 指标 | 值 |
|---|---|
| hit@5 | **0.904** |
| MRR@10 | **0.686** |
| p50 / p95 延迟 | 115ms / **147ms**（预算 400ms，余量 2.7 倍） |
| 否定例泄漏 | **0 / 2**（§6.1 的 org/project 过滤器在真实召回路径上有效） |

★ **比较基线前先看语料规模**：跑评测时常驻 worker 正在抽取，当天新入库 48 条（active 36 → 59）。9 条未命中里 3 条是被同日新记忆挤出 top-5，不是召回退化。跨时间比较必须先对齐语料规模，否则会把"库长大了"读成"算法变了"。

**未命中的两类模式**（按纪律本轮不动任何召回参数，留给 Phase 3 用同一评测集 A/B）：① 抽象/口语化 query 打不中术语类记忆；② 同项目同主题多条记忆排序区分度弱（#2380 几乎每条 case 都进 top-5，成了"中心记忆"）。两者都印证 C4"实际是 dense 单通道"——keywords 通道本该在这两类上补位。

**测试**：新增 `tests/test_evaluation.py` 22 个（指标手算、文件协议往返与坏块报错、导入幂等、runner 端到端 + 断言 recall_log 零污染）。全量 **514 passed**。

**未做**：CI 化（本项目无 CI，见 #2418）；1.2 的性能预算回归测试（需真模型，宜做独立脚本而非常规 pytest）；~~1.3 网关三项探测~~（已完成，见 7.11）；~~1.4 规矩漏判率抽样~~（已完成，见 7.12）。

### 7.11 第六轮实施记录（2026-11-10，1.3 网关三项探测 + llm.py 空响应防御）

三项探测全部完成，Phase 1 只剩 1.4。详细取证见 decisions-log #106。

| 探测 | 工具 | 结论 |
|---|---|---|
| 上下文上限 | `bench/bench_gateway_limits.py`（新） | ≥124643 tok 可用，未探到上限；配置 `max_input_tokens` 32000（占位值，无人消费）→ 实测回填 120000 |
| 并发/限流 | `bench/bench_gateway_concurrency.py` | 14 次调用零 429——无硬限流；并行 2 稳定甜点，3~4 偶发瞬时排队（复测完全并行）。**Phase 3 回填可开 4 worker**，排期按并行 2 保守估 |
| 长 prompt 延迟曲线 | 同上限脚本数据 | prefill ≈34ms/k tok（125k→4.2s）平缓近线性；对照 route avg 61s——延迟大头在 thinking/生成，token_budget 不必为延迟压缩 |

**附带发现与修复**：
- 网关有 prompt cache（`cache_read_input_tokens` 命中重复前缀）：route/extract 的固定 system prompt 天然享折扣，无需改代码；探测脚本统计口径改为三项之和。
- ★ 真实缺陷已修：`llm.py _post` 对"HTTP 200 但 body 空/非 JSON"抛裸 JSONDecodeError——job 队列不当可重试、且绕过 `_record_usage`（监控盲点：llm_usage 历史失败形态里从无此项，正因为漏记）。修复为转 LLMError 可重试，+2 个 MockTransport 测试；全量 522 passed。

### 7.12 第七轮实施记录（2026-11-11，1.4 判定完成——Phase 1 收官）

34 条候选全量判定完毕（AI 代判、用户授权，每条理由已写回 `rule-miss-sample.md`；「已成文」档逐条对照 living_doc 全部 4 条全文核实）。详细取证见 decisions-log #108。

| 口径 | 结果 |
|---|---|
| 规矩（净漏判） | 13 条 |
| 已成文（曾漏判已补救） | 3 条（#32 生造术语 / #111 分支流程 / #598 禁跑真实 UAT） |
| 零碎（非漏判） | 18 条 |
| 剔除 | 0 条 |
| **净漏判率** | **38.2%**（13/34） |
| **宽口径（曾漏判）** | **47.1%**（16/34） |

**读法注意**：候选池是规范性措辞预筛的可疑池，38.2% 是池内密度而非全库漏判率；「漏判」= 没进 living_doc 的 100% 注入保障，内容仍可被向量召回，不是丢失。

**净漏判 13 条的模式**：dida 组织流程规矩 7 条（README 版本记录、版本号规则、mvn 操作、提交范围×2、DDL 发版顺序、文案简短）+ dingding 项目约定 5 条（允许 Lombok、禁嵌套滚动、关卡不变式、不做站外推送、CSS 变量）+ 用户协作规矩 1 条（#2477 先讨论确认后动手）。共性：**用户明确说过/纠正过的项目级流程与风格约定**被 route 判成了 fact/preference——这正是 §10 预设动作的触发条件，修正方向是 route prompt 加 few-shot 区分「用户确立的规矩 vs 技术事实」（不加阈值），方案待用户确认后实施。

**顺带发现**：#2441 与 #2506 近重复（同一提交范围约定存了两条），提取判重存在缺口，留待观察。
