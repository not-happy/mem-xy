# mem-xy 运维手册（备份 / 恢复 / 换机 / 故障排查）

> 2026-11-10 依据真机验证过的行为编写（decisions-log #99–#102、EXPERT-REVIEW §五/§7）。
> 所有命令在**交互终端**（Git Bash 里调 powershell/pwsh，或直接 PowerShell）于项目根目录执行。
> 本机环境事实：PostgreSQL 18.6 + pgvector 0.8.7（`D:\Program Files\PostgreSQL\18\bin`）、
> Python 3.12、7z、git；数据目录 `~/.memxy`（MEM_HOME）。

## 0. 体系一览：数据在哪、谁在守护

**数据本体（两处，缺一不可）**

| 位置 | 内容 |
|---|---|
| PG 库 `memxy` | 全部记忆数据（conversation_log、memories、living_doc、jobs……） |
| `~/.memxy/` | `config.toml`（配置）、`secrets.toml`（DSN 密码、LLM token、**备份加密密码**）、`backups/`（备份分卷）、`backup-repo/`（GitHub 同步工作区） |

**三层防线**

1. 本地每日备份：`pg_dump -Fc` → 7z AES-256 加密分卷（50MB/卷，连文件名也加密）→ 保留最近 7 日 + 4 周 → 明文 dump 加密成功后即删。
2. GitHub 离机副本：私有仓 `not-happy/memxy-backup`，`backup` orphan 分支**单提交 force-push**（历史不膨胀），附 `manifest.json`（逐文件 sha256，可校验完整性）。只推密文——本地存在明文备份集时拒绝同步（硬闸）。
3. 加密卷里**一并打包 `config.toml` + `secrets.toml`**：新机器只靠备份卷 + 密码就能完整恢复。

**自动化（谁在定时干活）**

| 机制 | 节奏 | 说明 |
|---|---|---|
| backend 内置调度（`app/backup_daily.py`） | 每天 ≥ 12:30 触发，每 600s 检查 | 今天已有备份则幂等跳过（**跳过不写日志，静默是正常的**）；失败按 600s×2ⁿ 退避重试（封顶 4h）。★ 不用 Windows 计划任务跑备份：SASE 拦计划任务上下文的 pg_dump/psql（见 §6） |
| watchdog 计划任务（`memxy-watchdog`） | 每 5 分钟 | 探 `/healthz`，backend 挂了拉起；活着则 `POST /v1/workers/ensure` 补齐 worker 到 2。经 wscript+VBS 静默运行（无窗口）；只在有动作时写 `watchdog.log` |

**加密密码**：`~/.memxy/secrets.toml` 的 `[backup] password`（或环境变量 `MEM_BACKUP_PASSWORD`）。
**★ 密码丢失 = 所有备份全灭。**物理抄件放在安全处，不进任何 git 仓库。

## 1. 日常检查（通常不需要动手）

```powershell
.\service.ps1 status      # backend（含 UI 托管与构建新鲜度）/ worker 状态 + 模型就绪
.\backup.ps1 status       # 最近备份、磁盘占用、GitHub 同步状态
```

- **UI 地址：http://127.0.0.1:8787/**（backend 托管 `frontend/dist` 构建产物，无独立前端进程）。改了前端代码：`cd frontend && npm run build`，然后 `.\service.ps1 restart backend` 生效；开发调试可用 `.\service.ps1 start frontend`（5273 热更新）。
- 今日备份是否落地：`~/.memxy/backups/` 里应有 `memxy-<今天日期>-*.7z.001`（约 7MB/卷）。
- 日志：`backend.log` / `worker.log`（20MB 滚动）；`watchdog.log` 只在守护有动作时追加。
- 判断调度是否在跑：`backend.log` 启动时有一行「每日备份调度已启动（触发 ≥ 12:30 …）」。

## 2. 手动立即备份

```powershell
.\backup.ps1 backup       # 或 .\service.ps1 backup（等价透传）
```

成功标志：`[4/4]` 打出「[OK] 已推送 GitHub（N 个文件，单提交 force-push）」。

## 3. 恢复演练（建议每季度一次）

```powershell
.\backup.ps1 verify
```

流程全自动：最新加密卷解密 → 建临时库 `memxy_verify_*` → `pg_restore` → 六张关键表
（conversation_log / memories / raw_observations / pending_items / living_doc / jobs）
与生产库对账 → 删临时库、删临时明文。
对账语义：`restored ≤ live` 为正常（生产库在演练期间还在增长）；任何表 `restored > live`
或表缺失都会判「备份不可信」并报错。

## 4. 真实恢复

### 4.1 恢复生产库（数据损坏 / 误删）

```powershell
.\service.ps1 stop                                  # ① 停全部服务（backend 含 UI；worker/dev frontend 一并停）
.\backup.ps1 restore -DbName memxy -Force           # ② 最新备份覆盖生产库
.\service.ps1 start                                 # ③ 起服务，等模型就绪
python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=5).status)"   # ④ 应为 200
```

- `-Force` 是生产库覆盖的唯一开关；不带它脚本会拒绝并提示先停服务。
- 恢复用的临时明文 dump 路径会打印出来（`★ 临时明文 dump: ...`），**事后手动删除**。

### 4.2 恢复到另一个库（查历史数据，不动生产）

```powershell
.\backup.ps1 restore -DbName memxy_snapshot
```

不需要 `-Force`；库不存在会自动建。

### 4.3 从指定 dump 恢复（不用"最新"那份）

```powershell
# 若手上是加密卷，先解密（多卷会自动拼接，解 .001 即可）：
7z x memxy-20261110-120017.7z.001 -p<密码> -o<输出目录>
.\backup.ps1 restore -DbName memxy -DumpFile <输出目录>\memxy-20261110-120017.dump -Force
```

## 5. 换机迁移 / 灾难恢复（从零开始的新机器）

前提：拿到 **① 项目代码**（私有仓 `git clone https://github.com/not-happy/mem-xy.git`）和
**② 任一加密备份卷 + 加密密码**。备份卷来源：旧机 `~/.memxy/backups/`，或
`git clone -b backup git@github.com:not-happy/memxy-backup.git`（私有仓，需账号权限）。

```powershell
# ① 装依赖：PostgreSQL 18（含 pgvector 0.8.x）、7z、git、Python 3.12、Node.js
winget install 7zip.7zip Git.Git Python.Python.3.12
#    PG 用官方安装器装 18.x，再装 pgvector（版本 ≥ 0.8：召回依赖 hnsw.iterative_scan）

# ② 建空库（psql 连 postgres 维护库执行）
psql -U postgres -c "CREATE DATABASE memxy"

# ③ 解出配置与 dump（7z x 对多卷自动拼接）
7z x memxy-<最新>.7z.001 -p<密码> -o C:\restore-tmp
#    卷内含：memxy-*.dump、config.toml、secrets.toml

# ④ 配置归位：解出的两个 toml 拷到 ~/.memxy/
#    ★ 新机 PG 密码若与旧机不同，先改 secrets.toml 里 [db] dsn 的 password

# ⑤ 恢复数据
.\backup.ps1 restore -DbName memxy -DumpFile C:\restore-tmp\memxy-<最新>.dump -Force

# ⑥ 装运行依赖并启动
pip install -r backend\requirements.txt
cd frontend && npm install && npm run build && cd ..   # dist 是 UI 本体，必须构建
.\service.ps1 start            # 等 healthz 200（bge-m3 冷启动 15~33s）；UI = http://127.0.0.1:8787/
.\service.ps1 install-watchdog # 重新注册守护任务

# ⑦ 重绑 GitHub 离机副本并验证全链路
.\backup.ps1 init -GitRemote git@github.com:not-happy/memxy-backup.git
.\backup.ps1 backup            # 应产出新分卷并 push 成功
.\backup.ps1 verify            # 恢复演练通过 = 迁移完成
```

说明：`pg_restore` 会连同 schema、alembic_version、pgvector 扩展一起恢复，**不需要**
再跑 `alembic upgrade head`（跑了也无害，幂等）。embedding 模型（bge-m3）无需手工
下载：backend 启动时自动经 hf-mirror 拉取并缓存到本机（`HF_ENDPOINT` 已内置指向
镜像；新机器首次启动因要下载约 2GB 权重，冷启动会明显长于平时的 15~33s）。

## 6. 已知边界与故障排查

| 症状 | 原因与处置 |
|---|---|
| 备份失败日志出现「管理员用策略规则限制」/ Win32 1260 | SASE 应用控制禁止**计划任务/无人值守上下文**跑 pg_dump/psql（交互终端放行）。这只影响"机器重启后 watchdog 拉起的 backend"里跑的备份调度——处置：交互终端 `.\service.ps1 restart backend`，之后调度恢复正常。详见 decisions-log #100/#101 |
| `每日备份失败 (exit 1)` 但不是策略拦截 | 看 backend.log 里该行**上方**的 backup.ps1 输出定位；修复后不用管，调度按指数退避自动重试 |
| `★★ GitHub push 失败` | 本地备份仍有效，只是离机副本过期。查网络/凭据后手动重推：`git -C ~/.memxy/backup-repo push --force origin backup` |
| 12:30 过了 backend.log 没动静 | 正常：今天已有备份时调度幂等跳过且不打日志。确认 `~/.memxy/backups/` 有今日分卷即可 |
| watchdog 弹窗闪现 | 不应发生（已改 wscript+VBS 静默，#102）。若复现：`schtasks /Query /TN memxy-watchdog /XML` 确认 Action 是 `wscript.exe //B ...vbs`，不是则 `.\service.ps1 install-watchdog` 重注册 |
| worker 停了（UI 无报错、批准积压） | watchdog 每 5 分钟会自动补齐；等不及就 `.\service.ps1 start worker` |
| 改了前端但界面没变化 | UI 是构建产物（backend 托管 `frontend/dist`）：`cd frontend && npm run build` 后 `.\service.ps1 restart backend`。`status` 与 start 提示里的「dist 已过期」就是在说这件事；浏览器强刷（Ctrl+F5）排除缓存 |
| PowerShell 5.1 下脚本报 NativeCommandError / Get-FileHash 找不到 | 历史 bug 已修（#99/#101：Invoke-NativeMuted + .NET SHA256 直算）。若在新环境复现，用 pwsh 7 跑并回报 |
| 恢复演练对账某表 `★异常` | 该备份不可信，**不要**用它恢复生产；改用更早的分卷（`backups/` 里按日期取）重跑 verify |

## 7. 命令速查

```powershell
# 服务（★ 日常 UI 由 backend 托管，start/stop/restart 默认只管 backend）
.\service.ps1 start | stop | restart | status        # backend（UI: http://127.0.0.1:8787/）
.\service.ps1 start frontend | stop frontend         # Vite 开发服务器（仅改前端代码时用）
.\service.ps1 start worker | stop worker             # 异步抽取 worker
.\service.ps1 log [backend|frontend|worker]          # 实时日志
cd frontend; npm run build                           # 前端改动后重新构建，restart backend 生效

# 备份
.\backup.ps1 backup | verify | status
.\backup.ps1 restore -DbName <库> [-DumpFile <dump>] [-Force]
.\backup.ps1 init [-GitRemote <url>]                 # 首次初始化 / 重绑 GitHub

# 守护
.\service.ps1 install-watchdog | uninstall-watchdog
.\service.ps1 watchdog                               # 手动跑一次（调试）
```

相关文档：架构与决策依据见 `EXPERT-REVIEW.md` §五；踩坑取证全录见 `decisions-log.md` #99–#102。
