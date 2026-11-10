# eval_recall_cases 候选清单
<!-- 生成：2026-11-10 13:58 | python -m app.commands.eval --gen-candidates -->
<!-- 人工确认（改 query / 删块 / 补块）后导入： -->
<!--   python -m app.commands.eval --import eval-candidates.md -->
<!-- 字段：query 必填；expect=期望命中的记忆 id；must_not=绝不该出现的 id（否定例）； -->
<!-- org/project = 召回时的上下文（可空）；HTML 注释行导入时忽略。 -->

## case
- query: 写流程说明的时候，那些卡点环节的用词有什么讲究？
- expect: 32
- must_not: 
- org: 
- project: 
- label: 记忆#32 换说法
<!-- 原记忆 #32：用户不喜欢我自造"XX门"这类标签式说法（如"人审门""数量门""确认门"），听着奇怪且不专业。描述流程检查点时用平实直接的词：人工审核（人审）、触发条件/阈值、提案确认等，不要为了结构感生造术语。 -->

## case
- query: 我是不是跟你说过别自己造那种带后缀的标签式说法？
- expect: 32
- must_not: 
- org: 
- project: 
- label: 记忆#32 换说法
<!-- 原记忆 #32：用户不喜欢我自造"XX门"这类标签式说法（如"人审门""数量门""确认门"），听着奇怪且不专业。描述流程检查点时用平实直接的词：人工审核（人审）、触发条件/阈值、提案确认等，不要为了结构感生造术语。 -->

## case
- query: mem-xy 里那些旧叫法现在统一改成什么了，有没有对照关系？
- expect: 33
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#33 换说法
<!-- 原记忆 #33：mem-xy 术语约定（2026-10-28 用户拍板，要求平实措辞、不用自造标签）：活文档→成文规范（living_doc 表）、门控→阈值（置信度语境）/503 拦截（API 语境）、哑缓存→本地 -->

## case
- query: 之前一些内部叫法现在不用了，规范的叫法是什么来着？
- expect: 33
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#33 换说法
<!-- 原记忆 #33：mem-xy 术语约定（2026-10-28 用户拍板，要求平实措辞、不用自造标签）：活文档→成文规范（living_doc 表）、门控→阈值（置信度语境）/503 拦截（API 语境）、哑缓存→本地 -->
<!-- 审题改动：原题「为什么路由那个提示词文件里的老说法没跟着一起换掉」带错误前提，#33 答不了，换掉 -->

## case
- query: 做部分退票的时候，行程里对乘客构成有什么硬性要求？
- expect: 64
- must_not: 
- org: dida
- project: 
- label: 记忆#64 换说法
<!-- 原记忆 #64：WANO/GDN 供应商部分退票规则 10333：partial refund 后行程必须保留至少 1 名成人，否则 orderChange 返回 10333 "Refund is not allow -->

## case
- query: 如果退完之后只剩小孩和婴儿，接口会怎么反应？
- expect: 64
- must_not: 
- org: dida
- project: 
- label: 记忆#64 换说法
<!-- 原记忆 #64：WANO/GDN 供应商部分退票规则 10333：partial refund 后行程必须保留至少 1 名成人，否则 orderChange 返回 10333 "Refund is not allow -->

## case
- query: dida 这边仓库开发新功能，分支流程应该怎么走？
- expect: 111
- must_not: 
- org: dida
- project: 
- label: 记忆#111 换说法
<!-- 原记忆 #111：dida 系仓库（dida 组织，含 gds-agg 系列）git 工作流硬性规则：开发新功能禁止直接在 dev 分支上改代码，必须先从最新的 origin/dev（先 fetch）创建 featur -->

## case
- query: 能不能直接在自己的开发主干分支上改代码？
- expect: 111
- must_not: 
- org: dida
- project: 
- label: 记忆#111 换说法
<!-- 原记忆 #111：dida 系仓库（dida 组织，含 gds-agg 系列）git 工作流硬性规则：开发新功能禁止直接在 dev 分支上改代码，必须先从最新的 origin/dev（先 fetch）创建 featur -->

## case
- query: 发版的时候项目文档要怎么同步更新，条目格式长什么样？
- expect: 112
- must_not: 
- org: dida
- project: 
- label: 记忆#112 换说法
<!-- 原记忆 #112：dida 系仓库（gds-agg、gds-agg-processor-* 系列、gds-common、flight-supplier 后端仓库，以及 gds-sabre 配套前端仓库等）的发版/升级项 -->

## case
- query: 版本记录写在文件的什么位置，顺序有要求吗？
- expect: 112
- must_not: 
- org: dida
- project: 
- label: 记忆#112 换说法
<!-- 原记忆 #112：dida 系仓库（gds-agg、gds-agg-processor-* 系列、gds-common、flight-supplier 后端仓库，以及 gds-sabre 配套前端仓库等）的发版/升级项 -->

## case
- query: 定新版本号的时候该参照哪个分支上的版本？
- expect: 113
- must_not: 
- org: dida
- project: 
- label: 记忆#113 换说法
<!-- 原记忆 #113：dida 系仓库升级项目版本号的选择规则：新版本号只需高于当前 dev 分支（origin/dev）pom 里的版本即可（通常 dev 版本 +1），无需关注其它 feature 分支占用的版本号—— -->

## case
- query: 别的开发分支已经占了某个版本号，我要不要避开、跳号？
- expect: 113
- must_not: 
- org: dida
- project: 
- label: 记忆#113 换说法
<!-- 原记忆 #113：dida 系仓库升级项目版本号的选择规则：新版本号只需高于当前 dev 分支（origin/dev）pom 里的版本即可（通常 dev 版本 +1），无需关注其它 feature 分支占用的版本号—— -->

## case
- query: 会真实触发供应商下单出票的那些用例，你能帮我跑吗？
- expect: 598
- must_not: 
- org: dida
- project: 
- label: 记忆#598 换说法
<!-- 原记忆 #598：dida 系工程（gds-agg / gds-agg-processor-wano 等）的 UAT 测试 case：Claude 不得主动执行会产生真实供应商调用的测试（下单/出票/退票/改期等写操作 -->

## case
- query: UAT 里涉及写操作的测试由谁来执行，你负责哪部分？
- expect: 598
- must_not: 
- org: dida
- project: 
- label: 记忆#598 换说法
<!-- 原记忆 #598：dida 系工程（gds-agg / gds-agg-processor-wano 等）的 UAT 测试 case：Claude 不得主动执行会产生真实供应商调用的测试（下单/出票/退票/改期等写操作 -->

## case
- query: 执行安装打包命令的时候，参数上有什么必须遵守的规矩？
- expect: 605
- must_not: 
- org: dida
- project: 
- label: 记忆#605 换说法
<!-- 原记忆 #605：dida 系工程 Maven 操作规矩：mvn install 一律带 -DskipTests 跳过测试（安装快照只做编译打包）；测试只在验证新改动时单个执行（mvn test -Dtest=具体测试 -->

## case
- query: 我只想验证某一个测试类，命令该怎么写？
- expect: 605
- must_not: 
- org: dida
- project: 
- label: 记忆#605 换说法
<!-- 原记忆 #605：dida 系工程 Maven 操作规矩：mvn install 一律带 -DskipTests 跳过测试（安装快照只做编译打包）；测试只在验证新改动时单个执行（mvn test -Dtest=具体测试 -->

## case
- query: memxy 后台那几个做异步抽取的进程，怎么才能彻底停干净？
- expect: 2353
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2353 换说法
<!-- 原记忆 #2353：mem-xy 项目的 worker 进程是由后端 lifespan 拉起的，不在 service.ps1 的 state 文件里；要干净地停止/管理 worker 必须走 workers_ctl（或  -->

## case
- query: 我用启停脚本关服务，为什么那些后台任务进程好像不受它管？
- expect: 2353
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2353 换说法
<!-- 原记忆 #2353：mem-xy 项目的 worker 进程是由后端 lifespan 拉起的，不在 service.ps1 的 state 文件里；要干净地停止/管理 worker 必须走 workers_ctl（或  -->

## case
- query: memxy 现在挂了能自己活过来吗，是怎么配的？
- expect: 2354
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2354 换说法
<!-- 原记忆 #2354：mem-xy 项目运维现状与待办（截至 L2 备份落地后）：(1) 看门狗已注册——通过 service.ps1 的 install-watchdog 建为 Windows 计划任务 memxy-wa -->

## case
- query: 那个自愈的定时任务每次跑起来会不会弹个黑窗口，怎么做到静默的？
- expect: 2354
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2354 换说法
<!-- 原记忆 #2354：mem-xy 项目运维现状与待办（截至 L2 备份落地后）：(1) 看门狗已注册——通过 service.ps1 的 install-watchdog 建为 Windows 计划任务 memxy-wa -->

## case
- query: memxy 里关键词那块匹配的召回效果，还打算再调一版吗？
- expect: 2355
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2355 换说法
<!-- 原记忆 #2355：mem-xy 项目已明确不采纳或暂缓的改动：keywords 子串匹配调优在评测集建成前冻结不做；_error/_guard 跨模块私有引用整理不做（会波及 12 个文件的既有风格）；EXPERT-R -->

## case
- query: 各模块之间直接引用下划线开头的那些内部方法，这个坏味道最后处理了没？
- expect: 2355
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2355 换说法
<!-- 原记忆 #2355：mem-xy 项目已明确不采纳或暂缓的改动：keywords 子串匹配调优在评测集建成前冻结不做；_error/_guard 跨模块私有引用整理不做（会波及 12 个文件的既有风格）；EXPERT-R -->

## case
- query: 在 PowerShell 7 里注册一个每几分钟跑一次、永不到期的计划任务，老是报「值超出范围」，怎么绕过去？
- expect: 2356
- must_not: 
- org: personal
- project: 
- label: 记忆#2356 换说法
<!-- 原记忆 #2356：Windows 计划任务注册的跨版本坑：Register-ScheduledTask 的 -RepetitionDuration ([TimeSpan]::MaxValue) 只在 Windows P -->

## case
- query: 同一个注册脚本在老的 Windows PowerShell 上没问题，换 pwsh 就挂了，可能是哪儿的差异？
- expect: 2356
- must_not: 
- org: personal
- project: 
- label: 记忆#2356 换说法
<!-- 原记忆 #2356：Windows 计划任务注册的跨版本坑：Register-ScheduledTask 的 -RepetitionDuration ([TimeSpan]::MaxValue) 只在 Windows P -->

## case
- query: memxy 备份的加密口令是我自己定还是脚本自动生成？生成后存在哪儿？
- expect: 2357
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2357 换说法
<!-- 原记忆 #2357：mem-xy 的备份体系由 backup.ps1 管理：执行 backup.ps1 init 会生成 32 字节随机密码写入 ~/.memxy/secrets.toml 的 [backup] 段并创建 -->

## case
- query: 跑备份初始化之前，本机得先装好哪些工具？
- expect: 2357
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2357 换说法
<!-- 原记忆 #2357：mem-xy 的备份体系由 backup.ps1 管理：执行 backup.ps1 init 会生成 32 字节随机密码写入 ~/.memxy/secrets.toml 的 [backup] 段并创建 -->

## case
- query: tiket 这条线的验价，是真的去调对方的验价接口了吗？
- expect: 2358
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2358 换说法
<!-- 原记忆 #2358：gds-agg-processor-tiket 项目中，verify 查询验价不调用供应商的 verify 接口，而是在 TiketVerifyServiceImpl 里重新调 search（re-s -->

## case
- query: 为什么往返行程的验价明显比单程慢一倍左右？
- expect: 2358
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2358 换说法
<!-- 原记忆 #2358：gds-agg-processor-tiket 项目中，verify 查询验价不调用供应商的 verify 接口，而是在 TiketVerifyServiceImpl 里重新调 search（re-s -->

## case
- query: gds-agg-processor-tiket 调外部接口的超时时间在哪配？我在代码库里翻遍了没找到。
- expect: 2359
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2359 换说法
<!-- 原记忆 #2359：gds-agg-processor-tiket 项目本地的 yml/yaml/properties 中没有任何 timeout/feign/ribbon 配置，Feign 超时由 Nacos 上的 f -->

## case
- query: 这个项目的超时能不能在本地配置里改一下？
- expect: 2359
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2359 换说法
<!-- 原记忆 #2359：gds-agg-processor-tiket 项目本地的 yml/yaml/properties 中没有任何 timeout/feign/ribbon 配置，Feign 超时由 Nacos 上的 f -->

## case
- query: 想知道验价过程中每一次调供应商到底花了多少毫秒，看哪儿的数据最靠谱？
- expect: 2360
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2360 换说法
<!-- 原记忆 #2360：排查 dida/gds-agg-processor-tiket 中 verify/search 的实测耗时：精确的单次供应商调用耗时以 SLS TraceLog 里 Stopwatch 的 elaps -->

## case
- query: 本地日志文件里好几家供应商的记录混在一起，怎么只捞出 tiket 验价的那些行？
- expect: 2360
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2360 换说法
<!-- 原记忆 #2360：排查 dida/gds-agg-processor-tiket 中 verify/search 的实测耗时：精确的单次供应商调用耗时以 SLS TraceLog 里 Stopwatch 的 elaps -->

## case
- query: 我在出票聚合那个 processor 仓库里翻遍了都没找到 logback 配置，是不是我拉错分支了？
- expect: 2361
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2361 换说法
<!-- 原记忆 #2361：gds-agg-processor-tiket 工程本身没有日志配置（logback/log4j），它运行时嵌在 gds-agg-book-service 里，日志配置和日志文件路径都在 gds-ag -->

## case
- query: 这个工程自己不打日志吗，为什么本地跑起来看不到任何输出文件？
- expect: 2361
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2361 换说法
<!-- 原记忆 #2361：gds-agg-processor-tiket 工程本身没有日志配置（logback/log4j），它运行时嵌在 gds-agg-book-service 里，日志配置和日志文件路径都在 gds-ag -->

## case
- query: 验价的本地日志里怎么没看到耗时字段，我想统计调供应商花了多久要怎么办？
- expect: 2362
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2362 换说法
<!-- 原记忆 #2362：dida/gds-agg-processor-tiket 的 tiket verify 本地日志没有 cost/耗时字段：callSearch（depart）与 callSearchReturn（re -->

## case
- query: 同一趟验价里，去程那次外部请求的时长是从哪两条记录算出来的？
- expect: 2362
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2362 换说法
<!-- 原记忆 #2362：dida/gds-agg-processor-tiket 的 tiket verify 本地日志没有 cost/耗时字段：callSearch（depart）与 callSearchReturn（re -->

## case
- query: 之前统计的单程验价平均要跑多久来着，样本量大概多少？
- expect: 2363
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2363 换说法
<!-- 原记忆 #2363：dida/gds-agg-processor-tiket 的 verify depart 阶段（单程仅一次供应商 search HTTP 调用）实测耗时偏高：2026-11-10 的 gds-agg- -->

## case
- query: 验价这块慢，到底是慢在我们自己还是慢在对方接口？
- expect: 2363
- must_not: 
- org: dida
- project: dida/gds-agg-processor-tiket
- label: 记忆#2363 换说法
<!-- 原记忆 #2363：dida/gds-agg-processor-tiket 的 verify depart 阶段（单程仅一次供应商 search HTTP 调用）实测耗时偏高：2026-11-10 的 gds-agg- -->

## case
- query: mem-xy 的每天数据库备份现在靠什么触发的，还是那个系统定时任务吗？
- expect: 2368
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2368 换说法
<!-- 原记忆 #2368：mem-xy 项目的每日数据库自动备份不再依赖 Windows 计划任务，改由 backend 常驻进程内置调度线程 app/backup_daily.py 实现（默认每天 12:30 触发、当日幂等 -->

## case
- query: 机器重启之后备份一直报 ERROR，这是什么原因，怎么临时救回来？
- expect: 2368, 2381
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2368 换说法
<!-- 审题改动：#2368 与 #2381 都记了"重启后备份 ERROR + 交互恢复"，命中任一都算对 -->
<!-- 原记忆 #2368：mem-xy 项目的每日数据库自动备份不再依赖 Windows 计划任务，改由 backend 常驻进程内置调度线程 app/backup_daily.py 实现（默认每天 12:30 触发、当日幂等 -->

## case
- query: 钉钉那个项目的最终愿景是什么样的？
- expect: 2369
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2369 换说法
<!-- 原记忆 #2369：personal/dingding 项目的长期目标：实现 AI 全自动化流水线——用户在钉钉发布任务后，AI 自动完成需求规格化、开发、验证、评审、部署上线，并提供一个统一的指挥中心 UI 用于操作和 -->

## case
- query: 以后我在群里甩一个需求过去，期望它自己能一路做到哪一步？
- expect: 2369
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2369 换说法
<!-- 原记忆 #2369：personal/dingding 项目的长期目标：实现 AI 全自动化流水线——用户在钉钉发布任务后，AI 自动完成需求规格化、开发、验证、评审、部署上线，并提供一个统一的指挥中心 UI 用于操作和 -->

## case
- query: 钉钉这套 AI 平台目前做到什么程度了，都有哪些能力？
- expect: 2370
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2370 换说法
<!-- 原记忆 #2370：personal/dingding 现状：已有 AI 平台雏形（Spring Boot 后端 + Vue3 前端），具备自研 AgentRuntime（ReAct + Plan-Execute）、An -->

## case
- query: 现在的 agent 能不能直接帮我改代码、打包然后发上线？
- expect: 2370
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2370 换说法
<!-- 原记忆 #2370：personal/dingding 现状：已有 AI 平台雏形（Spring Boot 后端 + Vue3 前端），具备自研 AgentRuntime（ReAct + Plan-Execute）、An -->

## case
- query: 真要搞全自动开发，具体写代码那一环打算交给什么来跑？
- expect: 2371
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2371 换说法
<!-- 原记忆 #2371：personal/dingding 全自动化落地方案（AI 提出、用户尚未确认优先级）：workbench 只做控制面（任务编排/状态机/审计/UI），开发类任务的执行面复用 Claude Code  -->

## case
- query: 好几个任务同时在改代码，怎么避免互相把仓库搞脏？
- expect: 2371
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2371 换说法
<!-- 原记忆 #2371：personal/dingding 全自动化落地方案（AI 提出、用户尚未确认优先级）：workbench 只做控制面（任务编排/状态机/审计/UI），开发类任务的执行面复用 Claude Code  -->

## case
- query: 想手动把看门狗跑一遍看看效果，在 mem-xy 里敲什么命令？
- expect: 2372
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2372 换说法
<!-- 原记忆 #2372：mem-xy 中管理 memxy-watchdog 的命令位于 service.ps1：`.\service.ps1 watchdog` 立即跑一次用于调试，`install-watchdog` 注册 -->

## case
- query: 怎么把那个自恢复的定时任务从系统里删掉？
- expect: 2372
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2372 换说法
<!-- 原记忆 #2372：mem-xy 中管理 memxy-watchdog 的命令位于 service.ps1：`.\service.ps1 watchdog` 立即跑一次用于调试，`install-watchdog` 注册 -->

## case
- query: 钉钉那个个人项目，新表的时间列现在统一用啥类型？
- expect: 2373
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2373 换说法
<!-- 原记忆 #2373：在 personal/dingding 项目（C:/Users/yong.xu/own/dingding，PostgreSQL）中，数据库时间列统一使用 TIMESTAMP 类型（Java 实体用 L -->

## case
- query: Java 实体里跟数据库时间字段对应，应该声明成哪个类
- expect: 2373
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2373 换说法
<!-- 原记忆 #2373：在 personal/dingding 项目（C:/Users/yong.xu/own/dingding，PostgreSQL）中，数据库时间列统一使用 TIMESTAMP 类型（Java 实体用 L -->

## case
- query: 任务流水线那边状态一变，更新时间是谁负责写进去的
- expect: 2374
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2374 换说法
<!-- 原记忆 #2374：在 personal/dingding 项目的任务流水线模块中，updated_at 由 TaskStateMachine.transit() 这个唯一状态变更入口显式设置（PostgreSQL 没有 -->

## case
- query: PG 又没有 MySQL 那种字段自动刷时间的机制，我们这边咋处理的
- expect: 2374
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2374 换说法
<!-- 原记忆 #2374：在 personal/dingding 项目的任务流水线模块中，updated_at 由 TaskStateMachine.transit() 这个唯一状态变更入口显式设置（PostgreSQL 没有 -->

## case
- query: mem-xy 这边做完方案取舍之后，要不要留个记录，写到哪儿去
- expect: 2375
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2375 换说法
<!-- 原记忆 #2375：personal/mem-xy 项目把架构决策记录在仓库内的 decisions-log 中，按递增编号追加条目，做完方案取舍后需同步追加（例如 #102 记录 watchdog 静默方案及被否的 S -->

## case
- query: 架构决策日志的条目是怎么编号的
- expect: 2375
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2375 换说法
<!-- 原记忆 #2375：personal/mem-xy 项目把架构决策记录在仓库内的 decisions-log 中，按递增编号追加条目，做完方案取舍后需同步追加（例如 #102 记录 watchdog 静默方案及被否的 S -->

## case
- query: gds-sabre 要加个字段，迁移脚本该提到哪个仓库的哪个目录
- expect: 2376
- must_not: 
- org: dida
- project: dida/gds-sabre
- label: 记忆#2376 换说法
<!-- 原记忆 #2376：gds-sabre 使用的数据库表（如 gds_sabre_profile）的建表/加字段 DDL migration 脚本不在 gds-sabre 仓库内，而在同组织的 flight-db 仓库 d -->

## case
- query: sabre 那张 profile 表的建表语句，怎么在它自己代码库里翻不到
- expect: 2376
- must_not: 
- org: dida
- project: dida/gds-sabre
- label: 记忆#2376 换说法
<!-- 原记忆 #2376：gds-sabre 使用的数据库表（如 gds_sabre_profile）的建表/加字段 DDL migration 脚本不在 gds-sabre 仓库内，而在同组织的 flight-db 仓库 d -->

## case
- query: 有个开关想按每个 profile 单独控制，是走配置中心还是加表字段
- expect: 2377
- must_not: 
- org: dida
- project: dida/gds-sabre
- label: 记忆#2377 换说法
<!-- 原记忆 #2377：在 gds-sabre 项目中，需要按 profile 粒度控制的业务开关（例如是否把 commission 置为 0）倾向落在 gds_sabre_profile 表的新增字段上，而不是放在 nac -->

## case
- query: 把佣金清零这种业务行为，代码里倾向放在哪儿控制
- expect: 2377
- must_not: 
- org: dida
- project: dida/gds-sabre
- label: 记忆#2377 换说法
<!-- 原记忆 #2377：在 gds-sabre 项目中，需要按 profile 粒度控制的业务开关（例如是否把 commission 置为 0）倾向落在 gds_sabre_profile 表的新增字段上，而不是放在 nac -->

## case
- query: memxy 的自动备份，从导库到最后推上去，完整链路给我说一遍
- expect: 2378
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2378 换说法
<!-- 原记忆 #2378：mem-xy 项目的自动备份链路：backend 内的调度线程每天 12:30 之后首次检查（检查间隔 600s）触发备份，流程为 pg_dump → 7z AES-256 加密分卷 → 删除明文 → -->

## case
- query: 备份包加密了吗，用什么方式，分不分卷
- expect: 2378
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2378 换说法
<!-- 原记忆 #2378：mem-xy 项目的自动备份链路：backend 内的调度线程每天 12:30 之后首次检查（检查间隔 600s）触发备份，流程为 pg_dump → 7z AES-256 加密分卷 → 删除明文 → -->

## case
- query: 同一天里备份会不会被重复触发跑好几遍
- expect: 2379
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2379 换说法
<!-- 原记忆 #2379：mem-xy 项目的备份调度是幂等的：触发时先检查 ~/.memxy/backups 里是否已存在当日日期的备份文件，有则跳过，一天只备份一次；备份失败会记录 ERROR 日志并按指数退避重试（600 -->

## case
- query: 备份失败了会怎么处理，重试间隔是固定的还是递增的
- expect: 2379
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2379 换说法
<!-- 原记忆 #2379：mem-xy 项目的备份调度是幂等的：触发时先检查 ~/.memxy/backups 里是否已存在当日日期的备份文件，有则跳过，一天只备份一次；备份失败会记录 ERROR 日志并按指数退避重试（600 -->

## case
- query: memxy 的 backend 和看门狗是装成系统服务开机自启的吗
- expect: 2380
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2380 换说法
<!-- 原记忆 #2380：mem-xy 项目的 backend 与 watchdog 都只在用户登录会话内运行（非系统服务），backend 挂掉由 watchdog 每 5 分钟检查并自动拉起；因此自动备份依赖机器开机且用户 -->

## case
- query: backend 进程崩了之后多久会被重新拉起来
- expect: 2380
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2380 换说法
<!-- 原记忆 #2380：mem-xy 项目的 backend 与 watchdog 都只在用户登录会话内运行（非系统服务），backend 挂掉由 watchdog 每 5 分钟检查并自动拉起；因此自动备份依赖机器开机且用户 -->

## case
- query: 机器重启之后第一次自动备份失败了，日志里有报错，我这边要做点什么吗
- expect: 2381, 2368
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2381 换说法
<!-- 审题改动：与 #2368 语义重叠，命中任一都算对 -->
<!-- 原记忆 #2381：mem-xy 项目已知风险点 #101（该边界尚未真机实测）：机器重启后 backend 由 watchdog 计划任务链拉起，那一次备份的 pg_dump 可能被 SASE 拦截并报 ERROR；恢 -->

## case
- query: 为什么计划任务自己把服务拉起来的时候，那次数据库导出会被安全代理拦掉
- expect: 2381
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2381 换说法
<!-- 原记忆 #2381：mem-xy 项目已知风险点 #101（该边界尚未真机实测）：机器重启后 backend 由 watchdog 计划任务链拉起，那一次备份的 pg_dump 可能被 SASE 拦截并报 ERROR；恢 -->

## case
- query: 跑整套测试总有零星几个挂，而且每次挂的地方还不一样，这是怎么回事
- expect: 2391
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2391 换说法
<!-- 原记忆 #2391：mem-xy 的 #97：pytest 与两个常驻 worker 共用生产库 jobs 表，claim 形成全局争抢，导致全量测试约 1/10–1/20 间歇性失败且失败点漂移；clean_jobs  -->

## case
- query: 在本地直接跑测试会不会把线上还没处理的任务队列给弄没了
- expect: 2391
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2391 换说法
<!-- 原记忆 #2391：mem-xy 的 #97：pytest 与两个常驻 worker 共用生产库 jobs 表，claim 形成全局争抢，导致全量测试约 1/10–1/20 间歇性失败且失败点漂移；clean_jobs  -->

## case
- query: 测试都搬到独立库了，按组织前缀切数据那套还有必要留着吗
- expect: 2392
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2392 换说法
<!-- 原记忆 #2392：mem-xy 的 #95 分区机制（org 前缀分区 + 内容前缀防火墙）在测试整体迁到独立库 memxy_test 之后，角色由主要隔离手段降级为第二道防线，保留用于防止有人直接对生产库跑测试 -->

## case
- query: 前缀隔离现在算主要防线还是兜底的
- expect: 2392
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2392 换说法
<!-- 原记忆 #2392：mem-xy 的 #95 分区机制（org 前缀分区 + 内容前缀防火墙）在测试整体迁到独立库 memxy_test 之后，角色由主要隔离手段降级为第二道防线，保留用于防止有人直接对生产库跑测试 -->

## case
- query: 止血那一期的活儿都收尾了吗，真机验过没有
- expect: 2417
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2417 换说法
<!-- 原记忆 #2417：mem-xy 项目 Phase 0（止血：每日备份+恢复演练、router 事务改造、ingest 丢数据、幂等、worker 守护、embedding 双检锁、run_worker sleep、#9 -->

## case
- query: 接下来这个阶段主要想解决什么问题
- expect: 2417
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2417 换说法
<!-- 原记忆 #2417：mem-xy 项目 Phase 0（止血：每日备份+恢复演练、router 事务改造、ingest 丢数据、幂等、worker 守护、embedding 双检锁、run_worker sleep、#9 -->

## case
- query: 这个项目有 CI 吗，还是测试都在本地跑
- expect: 2418
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2418 换说法
<!-- 原记忆 #2418：mem-xy 项目是个人单机部署，没有 CI 流水线；评测/测试通过 pytest 或独立脚本在本地运行 -->

## case
- query: 评测和测试是怎么触发的，推代码之后会自动跑吗
- expect: 2418
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2418 换说法
<!-- 原记忆 #2418：mem-xy 项目是个人单机部署，没有 CI 流水线；评测/测试通过 pytest 或独立脚本在本地运行 -->

## case
- query: 那份一直在更新的项目说明文档，为什么不用文件存着
- expect: 2419
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2419 换说法
<!-- 原记忆 #2419：mem-xy 项目 decision #38 确立了"单一数据源"原则：living_doc 等持久化数据存数据库而非文件，文件方案已被否决 -->

## case
- query: 持久化的数据到底放库里还是放文件，我们之前定过结论吗
- expect: 2419
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2419 换说法
<!-- 原记忆 #2419：mem-xy 项目 decision #38 确立了"单一数据源"原则：living_doc 等持久化数据存数据库而非文件，文件方案已被否决 -->

## case
- query: 跑评测会不会把召回的审计日志写脏
- expect: 2420
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2420 换说法
<!-- 原记忆 #2420：mem-xy 的 recall() 函数预留了 write_log=False 参数，供评测 runner 调用以避免污染 recall_log 审计记录 -->

## case
- query: 评测脚本调召回的时候有没有办法让它别记日志
- expect: 2420
- must_not: 
- org: personal
- project: personal/mem-xy
- label: 记忆#2420 换说法
<!-- 原记忆 #2420：mem-xy 的 recall() 函数预留了 write_log=False 参数，供评测 runner 调用以避免污染 recall_log 审计记录 -->

## case
- query: 给 Sabre 那个用户资料加字段的时候，MyBatis 映射要改哪些地方，有什么坑
- expect: 2421
- must_not: 
- org: dida
- project: dida/gds-sabre
- label: 记忆#2421 换说法
<!-- 原记忆 #2421：在 dida/gds-sabre 中，GdsSabreProfileMapper.xml 的 resultMap 用 <constructor> 按位置映射，新增字段时必须与 GdsSabreProf -->

## case
- query: 为什么加完字段之后查出来的值跟列对不上了
- expect: 2421
- must_not: 
- org: dida
- project: dida/gds-sabre
- label: 记忆#2421 换说法
<!-- 原记忆 #2421：在 dida/gds-sabre 中，GdsSabreProfileMapper.xml 的 resultMap 用 <constructor> 按位置映射，新增字段时必须与 GdsSabreProf -->

## case
- query: sabre 配套的那个前端项目，是怎么知道线上已经发过新版、要提示用户刷新的？
- expect: 2422
- must_not: 
- org: dida
- project: dida/gds-sabre
- label: 记忆#2422 换说法
<!-- 原记忆 #2422：dida gds-sabre 配套前端仓库的 version.json 是前端版本变更提示标识，由 src/layout/index.vue 每 30 秒轮询；历史上每次发版靠手工扰动几个字符来变更， -->

## case
- query: 前端那个版本标识文件多久检查一次？在哪个地方做的轮询？
- expect: 2422
- must_not: 
- org: dida
- project: dida/gds-sabre
- label: 记忆#2422 换说法
<!-- 原记忆 #2422：dida gds-sabre 配套前端仓库的 version.json 是前端版本变更提示标识，由 src/layout/index.vue 每 30 秒轮询；历史上每次发版靠手工扰动几个字符来变更， -->

## case
- query: 要给 sabre 的客户档案加一个字段，前后端加起来一共得动哪几个库？
- expect: 2423
- must_not: 
- org: dida
- project: 
- label: 记忆#2423 换说法
<!-- 原记忆 #2423：dida gds-sabre 的 Sabre profile 字段类改动跨四个仓库：gds-sabre（GdsSabreProfileVo/GdsSabreProfile/Mapper.xml/Exa -->

## case
- query: profile 改动在 gds-sabre 里除了实体类还要同步改哪些文件？
- expect: 2423
- must_not: 
- org: dida
- project: 
- label: 记忆#2423 换说法
<!-- 原记忆 #2423：dida gds-sabre 的 Sabre profile 字段类改动跨四个仓库：gds-sabre（GdsSabreProfileVo/GdsSabreProfile/Mapper.xml/Exa -->

## case
- query: 钉钉这个研发工作台里，我之前说过发布任务希望是什么样的交互？
- expect: 2424
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2424 换说法
<!-- 原记忆 #2424：在 dingding 项目（AI 研发工作台 workbench）中，用户希望发布任务的交互像聊天一样：一句话/一段话描述需求即可，不要让用户在表单里选择或填写很多字段 -->

## case
- query: 为什么不想让用户在发布任务页填一堆字段？
- expect: 2424
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2424 换说法
<!-- 原记忆 #2424：在 dingding 项目（AI 研发工作台 workbench）中，用户希望发布任务的交互像聊天一样：一句话/一段话描述需求即可，不要让用户在表单里选择或填写很多字段 -->

## case
- query: 发布任务聊天化那套方案的整体流程是怎么设计的？
- expect: 2425
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2425 换说法
<!-- 原记忆 #2425：dingding 项目发布任务页的拟定改造方案：聊天式自由文本输入 → 一次轻量 LLM 调用生成结构化 spec → 以可编辑预填卡片展示 → 用户一键确认发布入队；原手动填写表单降级为默认收起的“ -->

## case
- query: 新增的那个解析草稿的接口叫什么，入参出参分别是什么？
- expect: 2425
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2425 换说法
<!-- 原记忆 #2425：dingding 项目发布任务页的拟定改造方案：聊天式自由文本输入 → 一次轻量 LLM 调用生成结构化 spec → 以可编辑预填卡片展示 → 用户一键确认发布入队；原手动填写表单降级为默认收起的“ -->

## case
- query: 任务那份结构化描述里的字段，是给人填的还是给程序用的？
- expect: 2426
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2426 换说法
<!-- 原记忆 #2426：dingding 项目中任务结构化 spec 的字段是面向机器消费的路由参数而非给人填写：repo 决定建哪个仓库的 worktree，targetModules 决定验证关卡跑 mvn -pl m1 -->

## case
- query: 指定模块那个字段会影响构建命令怎么拼吗？
- expect: 2426
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2426 换说法
<!-- 原记忆 #2426：dingding 项目中任务结构化 spec 的字段是面向机器消费的路由参数而非给人填写：repo 决定建哪个仓库的 worktree，targetModules 决定验证关卡跑 mvn -pl m1 -->

## case
- query: 工作台要加新的 AI 能力，模型调用和流式返回这块需要自己再造一套吗？
- expect: 2427
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2427 换说法
<!-- 原记忆 #2427：dingding 项目 workbench 已有 ModelConfig、ChatService 和 Playground 提供模型调用与 SSE 流式能力，新增 AI 功能（如发布任务的 spec  -->

## case
- query: 项目里现在有哪些现成的大模型相关基础设施可以直接拿来用？
- expect: 2427
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2427 换说法
<!-- 原记忆 #2427：dingding 项目 workbench 已有 ModelConfig、ChatService 和 Playground 提供模型调用与 SSE 流式能力，新增 AI 功能（如发布任务的 spec  -->

## case
- query: 大模型抽出来的字段值不合法，是直接报错中断发布吗？
- expect: 2428
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2428 换说法
<!-- 原记忆 #2428：dingding 项目对 LLM 抽取出的 spec 采用输入净化策略而非 fail-fast：仓库名不在配置清单中则留空由用户选择，riskLevel 不在枚举内则丢弃并按默认值处理，不抛异常打断发 -->

## case
- query: 模型给的仓库名不在我们配置的清单里，界面上会怎么表现？
- expect: 2428
- must_not: 
- org: personal
- project: personal/dingding
- label: 记忆#2428 换说法
<!-- 原记忆 #2428：dingding 项目对 LLM 抽取出的 spec 采用输入净化策略而非 fail-fast：仓库名不在配置清单中则留空由用户选择，riskLevel 不在枚举内则丢弃并按默认值处理，不抛异常打断发 -->

## case
- query: 做部分退票的时候，行程里对乘客构成有什么硬性要求？
- expect: 
- must_not: 64
- org: personal
- project: personal/mem-xy
- label: 跨org串台（dida 记忆不得在 personal 上下文出现）
<!-- 原记忆 #64（dida）：WANO/GDN 供应商部分退票规则 10333：partial refund 后行程必须保留至少 1 名成人，否则 orderChange 返回 10333  -->

## case
- query: mem-xy 里那些旧叫法现在统一改成什么了，有没有对照关系？
- expect: 
- must_not: 33
- org: dida
- project: 
- label: 跨org串台（personal 记忆不得在 dida 上下文出现）
<!-- 原记忆 #33（personal）：mem-xy 术语约定（2026-10-28 用户拍板，要求平实措辞、不用自造标签）：活文档→成文规范（living_doc 表）、门控→阈值（置信度语境）/5 -->
