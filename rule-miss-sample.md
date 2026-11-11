# 规矩漏判率抽样清单

> 生成：2026-11-11 11:46 · 候选池 34 条全量（plan §10 设计样本量 30，池接近样本量时全查）
> 口径：active 记忆正文含规范性措辞（必须/禁止/不要/一律/每次/不允许/应该），与 Metrics 页候选池同源。
> memories 表只存自动入库的零碎记忆；规矩本该走人审进 living_doc（100% 注入），
> 落进这里 = 静默漏判。

## 怎么判（改每条末尾的「判定: 待定」）
- `规矩` = 长期规范性要求 → 净漏判
- `已成文` = 是规矩但 living_doc 已覆盖 → 曾漏判已补救，单列
- `零碎` = 一次性事实/偏好陈述 → 非漏判
- `剔除` = 该条本身不该存在 → 不计入分母

改完跑：`python -m app.commands.rulemiss --score`

## #32 · preference/user · 全库 · 2026-10-28
- content: 用户不喜欢我自造"XX门"这类标签式说法（如"人审门""数量门""确认门"），听着奇怪且不专业。描述流程检查点时用平实直接的词：人工审核（人审）、触发条件/阈值、提案确认等，不要为了结构感生造术语。
- why: 2026-10-28 用户明确反馈："为什么你总喜欢说XX门？是专业术语？很奇怪听着"
- 来源: （无溯源）
- 判定: 待定

## #64 · fact/project · dida · 2026-10-28
- content: WANO/GDN 供应商部分退票规则 10333：partial refund 后行程必须保留至少 1 名成人，否则 orderChange 返回 10333 "Refund is not allowed because the remaining passengers would be only children or infants"。V2.0 UAT Excel #45（Partial Refund 1ADT）设计矛盾——它要求用 #11 订单（1ADT+1CHD）退唯一 ADT，必被 10333 拒绝；已在 WanoV2MandatoryCallFlowTest.case45 改为单 case 自包含：先新订 AK KUL-BWN 单程 2ADT+1CHD 出票，再退 pax1（ADT）保留 1ADT+1CHD。
- why: UAT case45 实测踩坑：Excel 用例与供应商业务规则冲突，避免以后按 Excel 原文重蹈覆辙
- 来源: （无溯源）
- 判定: 待定

## #111 · preference/project · dida · 2026-11-02
- content: dida 系仓库（dida 组织，含 gds-agg 系列）git 工作流硬性规则：开发新功能禁止直接在 dev 分支上改代码，必须先从最新的 origin/dev（先 fetch）创建 feature-* 命名风格的新分支（惯例为 feature-xxx，如 feature-ticket-status-unpaid-check、feature-supplier-error-code-mapping），在该分支开发，完成后提交并推送到远程同名分支；后续通过 PR/合并回 dev。
- why: 只在需要新建分支时才用得上的情境性约定，不需每次会话无条件提醒
- 来源: （无溯源）
- 判定: 待定

## #112 · preference/project · dida · 2026-11-02
- content: dida 系仓库（gds-agg、gds-agg-processor-* 系列、gds-common、flight-supplier 后端仓库，以及 gds-sabre 配套前端仓库等）的发版/升级项目版本号惯例：每次发版必须同步更新 README.md 的版本记录并升级版本号。README.md 更新格式：在文件顶部（首行项目说明之后）新增版本条目，格式「## V<版本号> <作者> <日期>」（processor 系与 gds-agg 如「## V1.0.x 许勇 YYYY-MM-DD」；gds-common 的署名惯例不同、见 #628），正文含 Feature / Fixed / Configuration 三段、条目之间用 *** 分隔，Configuration 段放对应飞书 story 链接（无内容或链接未知写「无」并提示用户补充，不要编造）；版本条目按时间倒序、最新在最上；每条描述保持单行精简、过长拆成短句多行。各 gds-agg-processor-* 仓库各自维护版本记录（格式参照 gds-agg 主仓库）；新建 processor 仓库时脚手架 flight-quickstart 模板残留的 V0.0.x 版本历史与本项目无关，需整体替换。
- why: 跨多个仓库的团队约定，不在当前项目范围内，但属于该组织内做事必须遵守的流程偏好
- 来源: （无溯源）
- 判定: 待定

## #113 · preference/project · dida · 2026-11-02
- content: dida 系仓库升级项目版本号的选择规则：新版本号只需高于当前 dev 分支（origin/dev）pom 里的版本即可（通常 dev 版本 +1），无需关注其它 feature 分支占用的版本号——各分支合并回 dev 时自行顺延解决，不要为避让其它分支而跳号。
- why: 用户在 gds-agg-processor-dida 升级版本时纠正：我因 feature-ticket-status-unpaid-check 分支占用了 1.0.45 而跳到 1.0.46，用户明确版本只需比 dev 高即可，最终定为 1.0.45
- 来源: （无溯源）
- 判定: 待定

## #598 · preference/project · dida · 2026-11-04
- content: dida 系工程（gds-agg / gds-agg-processor-wano 等）的 UAT 测试 case：Claude 不得主动执行会产生真实供应商调用的测试（下单/出票/退票/改期等写操作 case，以及 mvn test 跑 flow 测试类）。这类 case 一律由用户本人在 IDE 手动执行；Claude 只负责写代码、编译验证（test-compile）、分析用户跑出的日志。2026-11-04 曾因 Claude 后台跑 case45 真实下单（PNR G1SFNB）被用户明确制止。
- why: 用户明确指令「你不要执行真实uat case」；真实 UAT 调用会产生不可逆订单副作用
- 来源: （无溯源）
- 判定: 待定

## #605 · preference/project · dida · 2026-11-04
- content: dida 系工程 Maven 操作规矩：mvn install 一律带 -DskipTests 跳过测试（安装快照只做编译打包）；测试只在验证新改动时单个执行（mvn test -Dtest=具体测试类，配合 -Dsurefire.failIfNoSpecifiedTests=false），不要全量跑测试。背景：2026-11-04 全量/批量跑测试曾误触发真实 UAT 下单（PNR G1SFNB），与记忆 #598（不主动执行真实 UAT case）配套。
- why: 用户明确指令「以后 install 的时候跳过测试，测试只单个执行（新改动的验证）」
- 来源: （无溯源）
- 判定: 待定

## #2353 · fact/project · personal/personal/mem-xy · 2026-11-09
- content: mem-xy 项目的 worker 进程是由后端 lifespan 拉起的，不在 service.ps1 的 state 文件里；要干净地停止/管理 worker 必须走 workers_ctl（或 POST /v1/workers/ensure 恢复），不能依赖 service.ps1 的 state。
- why: 本轮排查中踩到的运维陷阱，涉及 worker 管理时才会用到，不需无条件注入
- 来源: session 5813b12c-3e88-45f3-b54f-43016aa42a90 · 2026-11-09
- 判定: 待定

## #2356 · fact/project · personal · 2026-11-10
- content: Windows 计划任务注册的跨版本坑：Register-ScheduledTask 的 -RepetitionDuration ([TimeSpan]::MaxValue) 只在 Windows PowerShell 5.1 有效，在 pwsh 7 下会序列化成 P99999999DT23H59M59S 被任务计划程序以「值超出范围」拒绝；可靠做法是拼任务 XML 后用 schtasks /Create /XML 注册，XML 的 <Repetition> 只写 <Interval> 而省略 <Duration> 即为真正的无限期重复，且该 XML 文件必须是 UTF-16 LE 编码（UTF-8 带 BOM 会被 schtasks 拒绝）
- why: 平台级技术约束，只在写计划任务注册代码时才用得上，不需每会话无条件注入
- 来源: session 5813b12c-3e88-45f3-b54f-43016aa42a90 · 2026-11-10
- 判定: 待定

## #2421 · fact/project · dida/dida/gds-sabre · 2026-11-10
- content: 在 dida/gds-sabre 中，GdsSabreProfileMapper.xml 的 resultMap 用 <constructor> 按位置映射，新增字段时必须与 GdsSabreProfile 的全参构造函数参数逐位对齐，且顺序要与 DDL 里 AFTER 子句指定的位置一致（当前为 44 个 arg）
- why: 按位置映射的隐式约束，改字段时极易错位，属于非显而易见的坑，但只在改这块代码时才需要
- 来源: session 2e274f04-0ef1-4bf6-a440-4cdd2cf77de6 · 2026-11-10
- 判定: 待定

## #2422 · fact/project · dida/dida/gds-sabre · 2026-11-10
- content: dida gds-sabre 配套前端仓库的 version.json 是前端版本变更提示标识，由 src/layout/index.vue 每 30 秒轮询；历史上每次发版靠手工扰动几个字符来变更，没有生成算法
- why: 机制不写代码里看不出来（无算法、纯手工扰动），下次发版还会用到
- 来源: session 2e274f04-0ef1-4bf6-a440-4cdd2cf77de6 · 2026-11-10
- 判定: 待定

## #2424 · preference/user · personal/personal/dingding · 2026-11-10
- content: 在 dingding 项目（AI 研发工作台 workbench）中，用户希望发布任务的交互像聊天一样：一句话/一段话描述需求即可，不要让用户在表单里选择或填写很多字段
- why: 用户主动提出的产品交互偏好，属该项目内的设计取向，后续做发布页时会被召回
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2426 · fact/project · personal/personal/dingding · 2026-11-10
- content: personal/dingding 项目中 TaskCreateRequest/spec 这套结构化数据是给机器执行用的任务契约，必须保留：(1) 字段是面向机器消费的路由参数而非给人填写——repo 决定建哪个仓库的 worktree，targetModules 决定验证关卡跑 mvn -pl m1,m2 -am 还是整仓构建，dbChange/riskLevel 决定审批策略与风险标记，acceptanceCriteria 进入引擎 prompt 和验收关卡；(2) spec 承担三个不可去掉的职责——作为自修回环（VERIFYING→失败→自动重试）的验证锚点、作为 L3 自动批准的授权基础（依赖 riskLevel/scope 与 L1-L4 信任阶梯）、以及花钱动代码的审计凭证；(3) 需要改变的不是 spec 本身而是发布交互：人不该手工填表单，而应说一句话意图、由 AI 生成草稿、人审阅确认。对照：Claude Code / VSCode agent 能纯聊天是因为人实时在环、可随时纠偏，而 dingding 流水线没人可问、问了就卡 WAIT_HUMAN。
- why: 用户质疑 spec 的必要性，AI 给出的结论是后续交互设计的前提约束
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2427 · pointer/project · personal/personal/dingding · 2026-11-10
- content: 在 personal/dingding 项目中，新增 AI 功能（如发布任务的 spec 提取）应复用现有模型调用基础设施，不要另建调用链：workbench 侧已有 ModelConfig、ChatService 和 Playground 提供模型调用与 SSE 流式能力；新增模型调用的标准做法是用 ModelConfigService.getDefault() 取默认模型配置（apiBase/apiKey/modelId），再用 LlmClientFactory.getClient(provider) 按 provider 路由拿客户端（当前唯一实现是 AnthropicClient），然后调用 client.streamChat(req) 拿到 Flux<LlmEvent> 并聚合其中的 TEXT_DELTA 文本；现成参考实现在 PlanExecuteStrategy。
- why: 项目内调用大模型的统一入口和范式，新功能要复用，读单个文件不容易一次看全
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2428 · fact/project · personal/personal/dingding · 2026-11-10
- content: dingding 项目中 SpecExtractorService 负责的 spec 抽取（POST /api/tasks/draft-spec）只把自由描述展开成表单字段：description 必须原样透传用户原文、不允许模型改写（因为任务引擎使用的是原始需求描述）；对 LLM 抽取出的字段采用输入净化策略而非 fail-fast——repo 不在配置白名单中则置 null（留空由用户选择），riskLevel 不在枚举内则回落默认值 MEDIUM，均不抛异常打断发布流程；这与代码内部枚举转换应 fail-fast 抛异常的规则相区别，因为 LLM 输出属于不可信外部输入。
- why: 跨会话容易被误改的设计约束（不改写原文、白名单兜底）
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2437 · fact/project · dida · 2026-11-10
- content: dida 的 gds-sabre 体系中，业务开关「出票零佣金兜底」的中文叫法已全量改名为「Commission设置0」（前端 label、各仓库 README、代码注释、flight-db DDL COMMENT 共 11 处），但标识符一律保留不变：数据库列名 zero_commission_enabled、Java 字段 zeroCommissionEnabled、方法 isZeroCommissionEnabled、测试方法 composeWithZeroCommissionEnabled*；gds-sabre README 中 V1.2.35 已上线版本的历史条目仍保留旧叫法「零佣金兜底」
- why: 中文术语与代码标识符从此不一致，日后看到界面上的「Commission设置0」需要知道对应字段是 zeroCommissionEnabled
- 来源: session 2e274f04-0ef1-4bf6-a440-4cdd2cf77de6 · 2026-11-10
- 判定: 待定

## #2438 · fact/project · dida · 2026-11-10
- content: flight-supplier 的 GdsSabreProfile / GdsSabreProfileVo 这类 Model 是由 MyBatis Generator 从 flight-db 的表结构生成的，修改字段注释时必须同步修改 flight-db DDL 里的 COMMENT，否则日后重跑生成器会把注释覆盖回与库里不一致的旧文案
- why: 跨仓库的生成关系与注释同步约定，改这块字段时才会用到
- 来源: session 2e274f04-0ef1-4bf6-a440-4cdd2cf77de6 · 2026-11-10
- 判定: 待定

## #2441 · fact/project · dida/dida/gds-sabre · 2026-11-10
- content: flight-supplier-system 前端仓库中 build.sh 常出现纯文件权限位变更（mode change 100755 => 100644，numstat 0/0），这是 Windows checkout 的副作用，提交时应排除、不要 add；未跟踪的 package-lock.json 同样不应提交
- why: 每次在该前端仓库提交都会遇到的坑，需要精确 add 排除
- 来源: session 2e274f04-0ef1-4bf6-a440-4cdd2cf77de6 · 2026-11-10
- 判定: 待定

## #2442 · fact/project · dida · 2026-11-10
- content: flight-db 的 DDL 版本号（如 V1.0.133.0）若被其他人占用撞号会导致 Flyway 启动失败，因此上线时必须最先合并 flight-db 并执行 DDL，推送前要复核版本号未被占用
- why: 跨项目通用的发版风险约束
- 来源: session 2e274f04-0ef1-4bf6-a440-4cdd2cf77de6 · 2026-11-10
- 判定: 待定

## #2453 · fact/project · dida/dida/gds-sabre · 2026-11-10
- content: flight-supplier 的 MyBatis Mapper 采用 44 个 positional <arg> 的 constructor 映射，任何字段改名或增删后必须逐位校验 constructor 顺序与 column 是否对齐，错位不会报错而是静默失败；写校验脚本时要注意 constructor 块首位是 <idArg column="id"> 而不是 <arg>，只抓 <arg> 会导致整体错位一位
- why: 本次重命名中踩到的静默失败陷阱与脚本坑，属于经验性知识、单看代码不易察觉；只在改 flight-supplier 的 Mapper/字段时才相关，故为 atomic_fact 而非 observation。scope 取 project（宁窄勿宽），content 内点名 flight-supplier。
- 来源: session 2e274f04-0ef1-4bf6-a440-4cdd2cf77de6 · 2026-11-10
- 判定: 待定

## #2462 · preference/project · personal/personal/dingding · 2026-11-10
- content: dida 系工程的部分规约（如 feign Response 必须 try-with-resources、禁用 Lombok）不适用于 dingding 项目，dingding 允许使用 Lombok @Data，dida 系代码风格仅作参考
- why: 情境性偏好：只在 dingding 项目内成立，避免把 dida 规约错误套用过来
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2469 · fact/project · personal/personal/dingding · 2026-11-10
- content: dingding 项目的调研任务在第一轮把报告写成 assets/REPORT-draft.md 而非约定的工作区根目录 REPORT.md，导致 DELIVERABLE 关卡判定“交付物缺失或为空”FAILED 并触发一轮 17 分钟的自动自修；改进方向是在调研 prompt 中硬性约束：报告必须且只能落在工作区根目录 REPORT.md，边调研边增量写，不接受 draft 文件名或子目录。
- why: 本次任务失败的根因与对应的 prompt 约束改进点，后续调 prompt 时会用到
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2477 · preference/user · 全库 · 2026-11-10
- content: 用户 yong.xu 的协作习惯：先与 AI 讨论设计方案、自己确认（如回复「OK」）之后才让 AI 动手实现；当他征询意见（例如「你觉得我的提议是否合适」）时，AI 应先给结论与方案，不要直接修改代码
- why: 关于用户本人的跨项目协作偏好，但本次是从对话行为推断而非用户明确声明，故存小本子按召回使用、置信度中等
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2479 · fact/project · personal/personal/dingding · 2026-11-10
- content: personal/dingding 工作台后端配置的目标仓库列表为 gds-agg、gds-agg-processor、gds-agg-processor-tiket、flight-db；CODE_CHANGE 类型任务发布时必须指定其中一个仓库，repo 为空时会返回 400 并提示 "Unconfigured repository: null (available: [...])"，不会创建任务
- why: 跨会话排查发布失败时需要知道后端支持哪些目标仓库，以及 repo 缺失的具体报错形态
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2480 · fact/project · personal/personal/dingding · 2026-11-10
- content: 在 personal/dingding 项目中，执行过程 tab 的 .engine-stream 是 display:flex + flex-direction:column 且带 max-height(640px) 的滚动容器，其子元素必须设置 flex-shrink:0，否则事件条数多、内容溢出时浏览器会纵向压缩子块（文字被压成半行），而不是让外层滚动
- why: 这是排查出的真实缺陷根因，代码里只有一条 flex-shrink:0 规则，不记录原因将来容易被当成冗余样式删掉而复发
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2481 · preference/project · personal/personal/dingding · 2026-11-10
- content: personal/dingding 项目的执行日志类 UI 不要做嵌套滚动：assistant 段落块、工具调用展开块、元信息展开块一律不设 max-height+overflow，全文直接展示，滚动只由外层 .engine-stream 承担（用户反馈过块内二级滚动条导致内容"只显示一半"）
- why: 用户抱怨"只显示了一半"后确立的界面约定，属于情境性偏好，只在改这个 tab 时用得上
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2482 · fact/project · personal/personal/dingding · 2026-11-10
- content: 在 personal/dingding 项目用 vite dev 验证 CSS 改动时，CSS 是独立的虚拟模块（style 模块），主模块请求里只有 JS，必须单独取 style 模块的内容才能确认样式是否编译生效
- why: 本轮实际踩到并绕过的验证方式，下次核对样式改动仍需照此操作，不是每次开工都要无条件提醒
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2490 · fact/project · personal/personal/dingding · 2026-11-10
- content: personal/dingding 项目的设计不变式：关卡（gates）必须由控制面在执行前显式确定，是控制面对执行面的硬约束，不能交由 LLM/引擎自行决定是否跑构建；策略可以组合，但控制面要校验不变式（例如任务动了现有仓库就必须包含构建关卡），spec 提取最多输出 kind 选预设、不直接输出策略
- why: 系统'全自动但有人类检查点'的核心安全前提，改动关卡/策略相关逻辑时必须遵守
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2498 · fact/project · personal/personal/dingding · 2026-11-10
- content: personal/dingding 的 Web UI 重构方向（交互终态提案，尚待用户确认）是把"操作员控制台"翻转为"助理前台"：以全局聊天指挥中心作为唯一常驻界面，首页承载北极星顶条、全局会话入口、今日简报、决策卡队列与进行中条；spec 全程对用户不可见、仅在卡片中露摘要，点详情才展开全量；发布页表单逻辑并入会话输入的结构化展开面板；旧任务看板降级为钻取/监控视图、详情页保留为取证与审计视图；提案收件箱、记忆评审、任务审批全部汇入同一个对话面，以解决待办分散三处的问题；每个 UI 切片必须跟在它背后的数据面之后落地（S1 信任层→首页骨架，S2 记忆层→简报"学到了什么"，S3 连接器→提案卡），避免出现无数据的空壳界面。
- why: 设计方向已成型但未获用户拍板，跨会话继续讨论时需要
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-10
- 判定: 待定

## #2505 · preference/project · dida · 2026-11-10
- content: dida 系仓库的 README 版本条目与实体类字段备注（Javadoc/DDL COMMENT 等）一律要求简短清晰，只描述改动本身，不附带业务行为的长解释。示例：「强制默认Commission」标签加 tooltip，README 只写「Sabre Profile 维护页「强制默认Commission」标签新增小问号提示」，不要把提示文案内容（开启后如没有匹配到政策返点将默认设置返点值0）也写进去。2026-11-10 用户在 flight-supplier-system V1.3.116 提交时明确纠正。
- why: 用户明确反馈「README和实体类字段备注都要求简短清晰，不要啰嗦」，并要求返工已提交的描述
- 来源: （无溯源）
- 判定: 待定

## #2506 · fact/project · dida/dida/flight-supplier-system · 2026-11-10
- content: dida/flight-supplier-system 前端仓库提交功能改动时，提交内容只应包含功能相关文件（如 README.md、package.json、public/version.json、改动的 .vue 文件），build.sh 与 package-lock.json 的本地改动不要带入提交
- why: AI 在汇报中确认了该仓库的提交文件范围约定，非从代码可直接推导，下次在该仓库提交时会用到
- 来源: session 0dd6ba3c-5eaa-4b6f-8269-5a6062ad8ac1 · 2026-11-10
- 判定: 待定

## #2515 · fact/project · personal/personal/mem-xy · 2026-11-10
- content: mem-xy 对接的 LLM 网关带 prompt cache：重复前缀计入 usage.cache_read_input_tokens，usage.input_tokens 只报未命中缓存的部分（64k 档实测只报 15870）。统计总输入 token 必须把 input_tokens、cache_read_input_tokens、cache_creation 三项相加，否则上下文上限判定会算错；route/extract 的固定 system prompt 天然享折扣，无需改代码。
- why: 探测中踩到的统计口径陷阱，容易再次算错，但只在涉及 usage/token 统计时相关
- 来源: session 5813b12c-3e88-45f3-b54f-43016aa42a90 · 2026-11-10
- 判定: 待定

## #2529 · fact/project · personal/personal/dingding · 2026-11-11
- content: personal/dingding 项目的通知能力目前只做站内通知，不支持站外推送（邮件、IM、钉钉外发等），设计新功能时不要引入站外推送渠道
- why: 用户明确划定的能力边界，属于范围否决项
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-11
- 判定: 待定

## #2531 · preference/project · personal/personal/dingding · 2026-11-11
- content: personal/dingding 项目要求同时支持亮色与暗色主题并可切换，暗色基于 Element Plus 暗色模式实现（引入 theme-chalk/dark/css-vars.css 并在 html 上加 dark 类），因此页面样式一律使用 var(--el-*) 变量，不要硬编码颜色值
- why: 用户决策「都做，可切换」，衍生出本项目样式必须走 CSS 变量的约定
- 来源: session c75d47e6-2bd8-4cd5-87e1-9e9dd20abc9e · 2026-11-11
- 判定: 待定
