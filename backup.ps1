# mem-xy - 备份/恢复/校验脚本（单机部署三层防线，见 EXPERT-REVIEW.md §五）
#
# 用法:
#   .\backup.ps1 init [-GitRemote <url>]   - 首次初始化：检查依赖、生成加密密码、建备份目录、(可选)绑定 GitHub 备份仓库
#   .\backup.ps1 backup                    - 每日备份：pg_dump → 7z AES-256 加密分卷(50MB) → 保留策略(7日+4周) → 同步 GitHub
#   .\backup.ps1 verify                    - 恢复演练：最新备份解密 → 恢复到临时库 → 对账关键表行数 → 删临时库
#   .\backup.ps1 restore -DbName <db> [-DumpFile <path>] [-Force]
#                                          - 真实恢复：默认恢复到指定库；恢复到生产库名必须 -Force
#   .\backup.ps1 status                    - 查看：最近备份、磁盘占用、GitHub 同步状态、计划任务状态
#   .\backup.ps1 install-task [-At 12:30]  - 注册 Windows 每日计划任务（★本机被 SASE 拦：
#                          计划任务上下文禁跑 pg_dump/psql，日常用 backend 内置调度，见 decisions-log #100）
#   .\backup.ps1 uninstall-task            - 删除计划任务
#
# 安全设计（评审 §5.1）:
#   - 离开本机的只有密文：7z AES-256（-mhe=on 连文件名也加密），密码存 ~/.memxy/secrets.toml [backup]，
#     或环境变量 MEM_BACKUP_PASSWORD。密码丢失 = 备份全灭，抄一份放物理安全的地方。
#   - 无密码/无 7z 时允许生成本地明文 .dump，但**拒绝同步 GitHub**（不推明文是硬闸）。
#   - ~/.memxy/config.toml + secrets.toml 一并打进加密卷：新机器恢复不缺配置。
#   - GitHub 备份仓库是独立 orphan 分支、force-push 单提交，历史不膨胀；
#     push 失败大声报警（静默失效是头号敌人）。
#
# 依赖: pg_dump/psql/pg_restore（PATH / 运行中 postgres 进程 / postgresql* 服务注册路径 / 各盘 Program Files\PostgreSQL）、7z（可选，加密必需）、git（可选，同步必需）
# 配置: 环境变量 MEM_HOME（默认 ~/.memxy）、MEM_DSN、MEM_BACKUP_DIR（默认 $MEM_HOME\backups）、
#       MEM_BACKUP_PASSWORD、MEM_BACKUP_KEEP_DAILY(7)、MEM_BACKUP_KEEP_WEEKLY(4)

param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "backup", "verify", "restore", "status", "install-task", "uninstall-task", "help")]
    [string]$Action = "help",

    [string]$GitRemote = "",
    [string]$DumpFile = "",
    [string]$DbName = "",
    [string]$At = "12:30",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- 基础配置

$MemHome = if ($env:MEM_HOME) { $env:MEM_HOME } else { Join-Path $env:USERPROFILE ".memxy" }
$BackupDir = if ($env:MEM_BACKUP_DIR) { $env:MEM_BACKUP_DIR } else { Join-Path $MemHome "backups" }
$RepoDir = Join-Path $MemHome "backup-repo"
$TaskName = "memxy-daily-backup"
$KeepDaily = if ($env:MEM_BACKUP_KEEP_DAILY) { [int]$env:MEM_BACKUP_KEEP_DAILY } else { 7 }
$KeepWeekly = if ($env:MEM_BACKUP_KEEP_WEEKLY) { [int]$env:MEM_BACKUP_KEEP_WEEKLY } else { 4 }
$DefaultDsn = "host=127.0.0.1 port=5432 dbname=memxy user=postgres"
$ArchivePrefix = "memxy-"

function Get-DsnFromToml {
    # 从单个 toml 文件里取第一个 `dsn = "..."`（[db] 段）
    param([string]$Path)
    if (Test-Path $Path) {
        foreach ($line in (Get-Content $Path)) {
            if ($line.Trim() -match '^dsn\s*=\s*"(.+)"\s*$') { return $Matches[1] }
        }
    }
    return $null
}

function Get-Dsn {
    # 与 backend/app/core/config.py 同序：env > secrets.toml > config.toml > 内置默认。
    # ★ 本机 dsn（含密码）实际在 secrets.toml [db]——之前漏了 secrets.toml，
    #   pg_dump 拿到无密码的默认 DSN 后挂在交互式密码提示上（2026-11-10 首跑实踩）。
    if ($env:MEM_DSN) { return $env:MEM_DSN }
    foreach ($f in @("secrets.toml", "config.toml")) {
        $found = Get-DsnFromToml (Join-Path $MemHome $f)
        if ($found) { return $found }
    }
    return $DefaultDsn
}

function Get-DbNameFromDsn {
    param([string]$Dsn)
    if ($Dsn -match 'dbname=(\S+)') { return $Matches[1] }
    return "memxy"
}

function Get-DsnWithDb {
    # 把连接串里的 dbname 换掉（没有就追加），用于临时库操作
    param([string]$Dsn, [string]$NewDb)
    if ($Dsn -match 'dbname=\S+') { return ($Dsn -replace 'dbname=\S+', "dbname=$NewDb") }
    return "$Dsn dbname=$NewDb"
}

function Read-BackupPassword {
    if ($env:MEM_BACKUP_PASSWORD) { return $env:MEM_BACKUP_PASSWORD }
    $secFile = Join-Path $MemHome "secrets.toml"
    if (!(Test-Path $secFile)) { return $null }
    $section = ""
    foreach ($line in (Get-Content $secFile)) {
        $t = $line.Trim()
        if ($t -match '^\[(.+)\]$') { $section = $Matches[1]; continue }
        if ($section -eq "backup" -and $t -match '^password\s*=\s*"(.*)"\s*$') { return $Matches[1] }
    }
    return $null
}

function Find-Tool {
    param([string]$Name, [string[]]$ProbeDirs)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in $ProbeDirs) {
        $p = Join-Path $dir "$Name.exe"
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Get-PgProbeDirs {
    # PG 客户端工具的 bin 目录候选，准的在前：
    #   ① 运行中的 postgres 进程所在 bin（顺带保证客户端与服务端同版本）
    #   ② postgresql* Windows 服务注册的 postgres.exe 路径
    #   ③ 各固定盘 \<盘>:\Program Files\PostgreSQL\<版本>\bin（版本降序）
    # ★ 本机 PG 18 实测装在 D 盘——只扫 C:\Program Files 会全部报"缺失"
    #   （2026-11-10 init 首跑实踩），所以必须从进程/服务反查，不能靠盘符枚举碰运气。
    $dirs = @()
    $procPath = (Get-Process postgres -ErrorAction SilentlyContinue |
        Where-Object { $_.Path } | Select-Object -First 1).Path
    if ($procPath) { $dirs += (Split-Path $procPath) }
    foreach ($svc in (Get-CimInstance Win32_Service -Filter "Name LIKE 'postgresql%'" -ErrorAction SilentlyContinue)) {
        if ($svc.PathName -match '"?([A-Za-z]:\\[^"]+?\\postgres\.exe)"?') {
            $dirs += (Split-Path $Matches[1])
        }
    }
    foreach ($driveRoot in (Get-PSDrive -PSProvider FileSystem).Root) {
        $base = Join-Path $driveRoot "Program Files\PostgreSQL"
        if (Test-Path $base) {
            foreach ($v in (Get-ChildItem $base -Directory | Sort-Object Name -Descending)) {
                $dirs += (Join-Path $v.FullName "bin")
            }
        }
    }
    return @($dirs | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique)
}

function Get-PgTool {
    param([string]$Name)
    $tool = Find-Tool -Name $Name -ProbeDirs (Get-PgProbeDirs)
    if (!$tool) { throw "找不到 $Name：请安装 PostgreSQL 客户端或把它加进 PATH" }
    return $tool
}

function Get-7z {
    $cmd = Get-Command "7z" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $p = "C:\Program Files\7-Zip\7z.exe"
    if (Test-Path $p) { return $p }
    return $null
}

function Invoke-Native {
    # 统一跑外部命令：失败大声抛。
    # ★ 输出必须 Out-Host：native 命令的 stdout 会进 PowerShell 管道，
    #   Expand-LatestArchive 的 return 曾被 7z 全量输出污染，pg_restore 拿到
    #   一整坨文本当文件名（2026-11-10 verify 首跑实踩）。
    # ★ 错误信息脱敏 DSN 的 password= 与 7z 的 -p 参数：这条 throw 会进终端
    #   和计划任务日志（同函数旧注释只防了 7z，漏了 pg 工具——DSN 里就有密码）。
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    & $Exe @Arguments | Out-Host
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        $safe = ($Arguments | ForEach-Object {
            $_ -replace '(password=)\S+', '$1***' -replace '^-p\S+', '-p***'
        }) -join ' '
        throw "$What 失败 (exit $code): $Exe $safe"
    }
}

function Invoke-NativeMuted {
    # 跑外部命令并把 stdout+stderr 合并成字符串返回；成败由调用方查
    # $LASTEXITCODE（函数返回后仍可读）。
    # ★ PS 5.1 坑（2026-11-10 实踩，pwsh 7 无此行为——"双兼容"必须两个都真跑）：
    #   $ErrorActionPreference='Stop' 下，native 命令只要做 stderr 重定向
    #   （2>&1 / 2>$null），每行 stderr 都会被包成 NativeCommandError 直接 throw。
    #   git push 成功时也往 stderr 写进度，于是 push 明明成功、脚本却误报失败
    #   退出 1（backend 内置调度首跑实锤：远端已更新而任务记 ERROR）。
    #   规避：临时放宽 EAP，stderr 进管道合并而不是变成异常。
    param([string]$Exe, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        return (& $Exe @Arguments 2>&1 | Out-String)
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Get-FileSha256 {
    # ★ 不用 Get-FileHash cmdlet：backend 服务进程派生的 powershell 子进程里
    #   偶发 CommandNotFound（2026-11-10 实踩；交互链无法复现，疑模块自动加载
    #   环境差异）。改 .NET 直算：零模块依赖、任何 PS 宿主一致，且更快。
    param([string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $fs = [System.IO.File]::OpenRead($Path)
        try {
            return ([System.BitConverter]::ToString($sha.ComputeHash($fs)) -replace '-', '').ToLowerInvariant()
        } finally { $fs.Dispose() }
    } finally { $sha.Dispose() }
}

function Get-ArchiveStem {
    # memxy-20261109-123000(.7z.001|.dump) → memxy-20261109-123000
    param([string]$FileName)
    if ($FileName -match "^(memxy-\d{8}-\d{6})") { return $Matches[1] }
    return $null
}

function Get-BackupInventory {
    # 按 stem 归组当前备份目录里的所有备份集
    if (!(Test-Path $BackupDir)) { return @() }
    $groups = @{}
    foreach ($f in (Get-ChildItem $BackupDir -File | Where-Object { $_.Name -like "$ArchivePrefix*" })) {
        $stem = Get-ArchiveStem $f.Name
        if (!$stem) { continue }
        if (!$groups.ContainsKey($stem)) { $groups[$stem] = @() }
        $groups[$stem] += $f
    }
    $out = @()
    foreach ($stem in $groups.Keys) {
        # ★ Substring 用前缀长度而不是写死数字："memxy-" 是 6 个字符，
        #   写 5 会切出 "-20261110-…" 让 ParseExact 炸掉（2026-11-10 首跑实踩）。
        $ts = [datetime]::ParseExact($stem.Substring($ArchivePrefix.Length), "yyyyMMdd-HHmmss", $null)
        $encrypted = [bool]($groups[$stem] | Where-Object { $_.Name -like "*.7z.*" })
        $out += [pscustomobject]@{
            Stem = $stem; Time = $ts; Encrypted = $encrypted
            Files = $groups[$stem]
            Bytes = ($groups[$stem] | Measure-Object Length -Sum).Sum
        }
    }
    return ($out | Sort-Object Time -Descending)
}

function Expand-LatestArchive {
    # 把某个备份集解出明文 .dump 到临时目录，返回 dump 路径（调用方负责删临时目录）
    param($Entry, [string]$TmpRoot)
    $tmp = Join-Path $TmpRoot $Entry.Stem
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    if ($Entry.Encrypted) {
        $sevenZip = Get-7z
        if (!$sevenZip) { throw "备份是加密的但找不到 7z，无法解密" }
        $pwd_ = Read-BackupPassword
        if (!$pwd_) { throw "备份是加密的但没有密码（secrets.toml [backup] password 或 MEM_BACKUP_PASSWORD）" }
        $first = ($Entry.Files | Where-Object { $_.Name -like "*.7z.001" } | Select-Object -First 1)
        if (!$first) { $first = ($Entry.Files | Select-Object -First 1) }
        Invoke-Native $sevenZip @("x", "-p$pwd_", "-y", "-o$tmp", $first.FullName) "解密 $($Entry.Stem)"
    } else {
        Copy-Item ($Entry.Files | Select-Object -First 1).FullName $tmp
    }
    $dump = Get-ChildItem $tmp -Filter "*.dump" | Select-Object -First 1
    if (!$dump) { throw "备份集 $($Entry.Stem) 里没找到 .dump 文件" }
    return $dump.FullName
}

function Invoke-PsqlQuery {
    param([string]$Dsn, [string]$Sql)
    $psql = Get-PgTool "psql"
    $result = Invoke-NativeMuted $psql @("--no-password", "--dbname=$Dsn", "-At", "-c", $Sql)
    if ($LASTEXITCODE -ne 0) { throw "psql 执行失败: $Sql`n$result" }
    return $result.Trim()
}

# ---------------------------------------------------------------- init

function Do-Init {
    Write-Host "== mem-xy 备份初始化 ==" -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    Write-Host "备份目录: $BackupDir" -ForegroundColor Gray

    $pgDirs = Get-PgProbeDirs
    foreach ($t in @("pg_dump", "psql", "pg_restore")) {
        $found = Find-Tool -Name $t -ProbeDirs $pgDirs
        if ($found) { Write-Host "  [OK] $t -> $found" -ForegroundColor Green }
        else { Write-Host "  [缺失] $t（必装，否则无法备份）" -ForegroundColor Red }
    }
    $sevenZip = Get-7z
    if ($sevenZip) { Write-Host "  [OK] 7z -> $sevenZip" -ForegroundColor Green }
    else { Write-Host "  [缺失] 7z（加密分卷必需；winget install 7zip.7zip）" -ForegroundColor Yellow }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) { Write-Host "  [OK] git -> $($git.Source)" -ForegroundColor Green }
    else { Write-Host "  [缺失] git（GitHub 离机副本必需）" -ForegroundColor Yellow }

    # 加密密码：没有就生成一个并写入 ~/.memxy/secrets.toml [backup]
    $pwd_ = Read-BackupPassword
    if ($pwd_) {
        Write-Host "  [OK] 加密密码已存在（secrets.toml [backup]）" -ForegroundColor Green
    } else {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $newPwd = [Convert]::ToBase64String($bytes)
        $secFile = Join-Path $MemHome "secrets.toml"
        $existing = if (Test-Path $secFile) { Get-Content $secFile -Raw } else { "" }
        if ($existing -match '(?m)^\[backup\]') {
            Write-Host "  [跳过] secrets.toml 已有 [backup] 段但没解析到 password，请手工检查" -ForegroundColor Yellow
        } else {
            Add-Content -Path $secFile -Value "`n[backup]`npassword = `"$newPwd`""
            Write-Host "  [OK] 已生成加密密码写入 $secFile [backup]" -ForegroundColor Green
            Write-Host "  ★ 密码丢失 = 备份全灭。抄一份放物理安全的地方（不要进任何 git 仓库）。" -ForegroundColor Yellow
        }
    }

    if ($GitRemote) { Set-GitRemote $GitRemote }
    elseif (Test-Path (Join-Path $RepoDir ".git")) {
        $remote = (Invoke-NativeMuted git @("-C", $RepoDir, "remote", "get-url", "origin")).Trim()
        if ($LASTEXITCODE -ne 0) { $remote = "" }
        if ($remote) { Write-Host "  [OK] GitHub 备份仓库: $remote" -ForegroundColor Green }
    } else {
        Write-Host ""
        Write-Host "  下一步（离机副本）：在 GitHub 建一个**空的私有仓库**（不要和 mem-xy 代码仓库混用），然后：" -ForegroundColor Yellow
        Write-Host "    .\backup.ps1 init -GitRemote git@github.com:<you>\memxy-backup.git" -ForegroundColor Yellow
    }
}

function Set-GitRemote {
    param([string]$Remote)
    if (!(Get-Command git -ErrorAction SilentlyContinue)) { throw "未安装 git，无法绑定备份仓库" }
    if (!(Test-Path $RepoDir)) {
        New-Item -ItemType Directory -Path $RepoDir -Force | Out-Null
        Invoke-Native git @("-C", $RepoDir, "init") "git init"
        $null = Invoke-NativeMuted git @("-C", $RepoDir, "checkout", "--orphan", "backup")
        $null = Invoke-NativeMuted git @("-C", $RepoDir, "rm", "-rf", ".")   # 空仓无文件可删，fatal 可容忍
    }
    $existing = (Invoke-NativeMuted git @("-C", $RepoDir, "remote", "get-url", "origin")).Trim()
    if ($LASTEXITCODE -eq 0 -and $existing) { Invoke-Native git @("-C", $RepoDir, "remote", "set-url", "origin", $Remote) "git remote set-url" }
    else { Invoke-Native git @("-C", $RepoDir, "remote", "add", "origin", $Remote) "git remote add" }
    Write-Host "  [OK] GitHub 备份仓库已绑定: $Remote（分支 backup，force-push 单提交）" -ForegroundColor Green
}

# ---------------------------------------------------------------- backup

function Do-Backup {
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    $dsn = Get-Dsn
    $dbName = Get-DbNameFromDsn $dsn
    Write-Host "== 备份 $dbName ($ts) ==" -ForegroundColor Cyan

    # 1. pg_dump（自定义压缩格式）
    $pgDump = Get-PgTool "pg_dump"
    $dumpPath = Join-Path $BackupDir "$ArchivePrefix$ts.dump"
    Write-Host "  [1/4] pg_dump -Fc ..." -ForegroundColor Gray
    # --no-password：DSN 缺密码时立刻报错，绝不挂在交互式提示上（计划任务里挂 = 无声僵死）
    Invoke-Native $pgDump @("-Fc", "--no-password", "--dbname=$dsn", "-f", $dumpPath) "pg_dump"
    $dumpSize = (Get-Item $dumpPath).Length
    if ($dumpSize -le 0) { throw "pg_dump 产出空文件" }
    Write-Host ("        {0:N1} MB" -f ($dumpSize / 1MB)) -ForegroundColor Gray

    # 2. 加密分卷（7z AES-256，50MB/卷，避开 GitHub 100MB 硬限）；~/.memxy 配置一并入卷
    $sevenZip = Get-7z
    $pwd_ = Read-BackupPassword
    $memFiles = @()
    foreach ($f in @("config.toml", "secrets.toml")) {
        $p = Join-Path $MemHome $f
        if (Test-Path $p) { $memFiles += $p }
    }
    $archiveStem = Join-Path $BackupDir "$ArchivePrefix$ts"
    if ($sevenZip -and $pwd_) {
        Write-Host "  [2/4] 7z AES-256 加密分卷 (50MB) ..." -ForegroundColor Gray
        Invoke-Native $sevenZip (@("a", "-t7z", "-mhe=on", "-p$pwd_", "-v50m", "$archiveStem.7z", $dumpPath) + $memFiles) "7z 加密"
        Remove-Item $dumpPath -Force   # 明文只在加密成功后删除
        $parts = @(Get-ChildItem $BackupDir -Filter "$ArchivePrefix$ts.7z.*")
        Write-Host ("        {0} 个分卷，共 {1:N1} MB（明文 dump 已删除）" -f $parts.Count, (($parts | Measure-Object Length -Sum).Sum / 1MB)) -ForegroundColor Gray
    } else {
        Write-Host "  [2/4] ★ 无 7z 或无密码：保留本地明文 .dump，本次**不会同步 GitHub**（不推明文是硬闸）" -ForegroundColor Yellow
    }

    # 3. 保留策略：最近 KeepDaily 天各留最新 + 最近 KeepWeekly 周各留最新
    Write-Host "  [3/4] 保留策略 (日 $KeepDaily / 周 $KeepWeekly) ..." -ForegroundColor Gray
    Apply-Retention

    # 4. GitHub 离机副本（force-push orphan 分支，历史恒为 1 个提交）
    Write-Host "  [4/4] 同步离机副本 ..." -ForegroundColor Gray
    Sync-Offsite

    Write-Host "== 备份完成 ==" -ForegroundColor Green
}

function Apply-Retention {
    $inventory = @(Get-BackupInventory)
    if ($inventory.Count -eq 0) { return }
    $keep = @{}
    # ★ 必须 -AsHashTable：Group-Object 单组时返回的是**一个 GroupInfo 对象**而非哈希表，
    #   .Keys 为空 → keep 集为空 → 下面把全部备份当"过期"删光（2026-11-10 首跑实踩，
    #   当天两组备份全灭）。多组时返回 GroupInfo[]，字符串索引同样不对。
    $byDay = $inventory | Group-Object { $_.Time.ToString("yyyyMMdd") } -AsHashTable -AsString
    $dayKeys = @($byDay.Keys | Sort-Object -Descending | Select-Object -First $KeepDaily)
    foreach ($k in $dayKeys) { foreach ($e in $byDay[$k]) { $keep[$e.Stem] = $true } }
    $byWeek = $inventory | Group-Object {
        $c = [System.Globalization.CultureInfo]::InvariantCulture
        $w = $c.Calendar.GetWeekOfYear($_.Time, [System.Globalization.CalendarWeekRule]::FirstFourDayWeek, [DayOfWeek]::Monday)
        "{0:0000}-{1:00}" -f $_.Time.Year, $w
    } -AsHashTable -AsString
    $weekKeys = @($byWeek.Keys | Sort-Object -Descending | Select-Object -First $KeepWeekly)
    foreach ($k in $weekKeys) { foreach ($e in $byWeek[$k]) { $keep[$e.Stem] = $true } }

    # ★ fail-safe：保留集为空而库存非空是绝不可能的正常状态（只能是代码 bug），
    #   此时宁可不删——保留策略出错的代价是"多占磁盘"，误删的代价是"备份全灭"。
    if ($keep.Count -eq 0) {
        Write-Host "        [跳过] keep 集异常为空，本次不清理（保留策略有 bug，请排查）" -ForegroundColor Red
        return
    }

    $removed = 0
    foreach ($e in $inventory) {
        if ($keep.ContainsKey($e.Stem)) { continue }
        foreach ($f in $e.Files) { Remove-Item $f.FullName -Force }
        $removed++
    }
    if ($removed -gt 0) { Write-Host "        清理过期备份 $removed 组" -ForegroundColor Gray }
}

function Sync-Offsite {
    $inventory = @(Get-BackupInventory)
    if ($inventory.Count -eq 0) { return }

    # 有明文（未加密）备份集时拒绝同步：离机副本必须是密文
    $plain = @($inventory | Where-Object { !$_.Encrypted })
    $hasRemote = $false
    if (Test-Path (Join-Path $RepoDir ".git")) {
        $null = Invoke-NativeMuted git @("-C", $RepoDir, "remote", "get-url", "origin")
        $hasRemote = ($LASTEXITCODE -eq 0)
    }

    if (!$hasRemote) {
        Write-Host "        [跳过] 未绑定 GitHub 备份仓库（.\backup.ps1 init -GitRemote <url>）" -ForegroundColor Yellow
        if ($plain.Count -gt 0) { Write-Host "        ★ 本地存在 $($plain.Count) 组明文备份，绑定远端后它们也不会被推送" -ForegroundColor Yellow }
        return
    }
    if ($plain.Count -gt 0 -and $inventory.Count -eq $plain.Count) {
        Write-Host "        [拒绝] 所有本地备份都是明文，不同步（装 7z + 配密码后重新 backup）" -ForegroundColor Red
        return
    }

    if (!(Test-Path $RepoDir)) { New-Item -ItemType Directory -Path $RepoDir -Force | Out-Null }
    # 清掉上一轮的备份文件（保留 .git），复制当前保留集
    foreach ($old in (Get-ChildItem $RepoDir -File -ErrorAction SilentlyContinue)) { Remove-Item $old.FullName -Force }
    $manifest = @()
    foreach ($e in ($inventory | Where-Object { $_.Encrypted })) {
        foreach ($f in $e.Files) {
            Copy-Item $f.FullName (Join-Path $RepoDir $f.Name)
            $manifest += [pscustomobject]@{
                name = $f.Name; bytes = $f.Length
                sha256 = (Get-FileSha256 $f.FullName)
                time = $e.Time.ToString("o")
            }
        }
    }
    $manifestObj = [pscustomobject]@{
        generated = (Get-Date).ToString("o")
        host = $env:COMPUTERNAME
        policy = "daily=$KeepDaily weekly=$KeepWeekly, 7z AES-256 50MB parts"
        files = $manifest
    }
    $manifestObj | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $RepoDir "manifest.json") -Encoding UTF8

    $status = (Invoke-NativeMuted git @("-C", $RepoDir, "status", "--porcelain")).Trim()
    $null = Invoke-NativeMuted git @("-C", $RepoDir, "log", "-1")
    $hasCommits = ($LASTEXITCODE -eq 0)
    if ($status -eq "" -and $hasCommits) {
        Write-Host "        [OK] 远端已是最新，无需推送" -ForegroundColor Green
        return
    }
    Invoke-Native git @("-C", $RepoDir, "add", "-A") "git add"
    & git -C $RepoDir -c user.name="memxy-backup" -c user.email="memxy@localhost" `
        commit -m "backup $(Get-Date -Format 'yyyyMMdd-HHmmss')" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git commit 失败" }
    $pushOut = Invoke-NativeMuted git @("-C", $RepoDir, "push", "--force", "origin", "backup")
    if ($LASTEXITCODE -ne 0) {
        # push 失败绝不静默：本地备份仍然有效，但离机副本已过期
        Write-Host "        ★★ GitHub push 失败！本地备份有效，但离机副本未更新。" -ForegroundColor Red
        Write-Host ($pushOut.Trim()) -ForegroundColor Red
        Write-Host "        ★★ 检查网络/凭据后手动重推: git -C `"$RepoDir`" push --force origin backup" -ForegroundColor Red
        throw "离机副本同步失败（本地备份已完成）"
    }
    Write-Host "        [OK] 已推送 GitHub（$($manifest.Count) 个文件，单提交 force-push）" -ForegroundColor Green
}

# ---------------------------------------------------------------- verify

function Do-Verify {
    $inventory = @(Get-BackupInventory)
    if ($inventory.Count -eq 0) { throw "备份目录 $BackupDir 里没有任何备份" }
    $entry = $inventory[0]
    Write-Host "== 恢复演练：$($entry.Stem)（$($entry.Time)）==" -ForegroundColor Cyan

    $dsn = Get-Dsn
    $scratch = "memxy_verify_$(Get-Date -Format 'yyyyMMddHHmmss')"
    $scratchDsn = Get-DsnWithDb $dsn $scratch
    $tmpRoot = Join-Path $env:TEMP "memxy-verify-$(Get-Random)"
    try {
        $dumpPath = Expand-LatestArchive -Entry $entry -TmpRoot $tmpRoot
        Write-Host "  [1/4] 解密完成: $dumpPath" -ForegroundColor Gray

        Invoke-PsqlQuery $dsn "CREATE DATABASE $scratch" | Out-Null
        Write-Host "  [2/4] 临时库 $scratch 已建" -ForegroundColor Gray

        $pgRestore = Get-PgTool "pg_restore"
        Invoke-Native $pgRestore @("--dbname=$scratchDsn", "--no-password", "--no-owner", "--no-privileges", $dumpPath) "pg_restore"
        Write-Host "  [3/4] pg_restore 成功" -ForegroundColor Gray

        Write-Host "  [4/4] 关键表对账（恢复库 vs 生产库）:" -ForegroundColor Gray
        $tables = @("conversation_log", "memories", "raw_observations", "pending_items", "living_doc", "jobs")
        $bad = 0
        foreach ($t in $tables) {
            $restored = try { Invoke-PsqlQuery $scratchDsn "SELECT count(*) FROM $t" } catch { "<表缺失>" }
            $live = try { Invoke-PsqlQuery $dsn "SELECT count(*) FROM $t" } catch { "?" }
            $ok = ($restored -match '^\d+$') -and ($live -notmatch '^\d+$' -or [int]$restored -le [int]$live) -and ($live -notmatch '^\d+$' -or [int]$restored -gt 0 -or [int]$live -eq 0)
            if (!$ok) { $bad++ }
            Write-Host ("        {0,-20} restored={1,-8} live={2,-8} {3}" -f $t, $restored, $live, $(if ($ok) { "OK" } else { "★异常" })) `
                -ForegroundColor $(if ($ok) { "Gray" } else { "Red" })
        }
        if ($bad -gt 0) { throw "对账发现 $bad 张表异常，备份不可信！" }
        Write-Host "== 演练通过：备份可恢复 ==" -ForegroundColor Green
    } finally {
        try { Invoke-PsqlQuery $dsn "DROP DATABASE IF EXISTS $scratch" | Out-Null } catch {}
        if (Test-Path $tmpRoot) { Remove-Item $tmpRoot -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

# ---------------------------------------------------------------- restore

function Do-Restore {
    $dsn = Get-Dsn
    $prodDb = Get-DbNameFromDsn $dsn
    if (!$DbName) {
        Write-Host "必须指定目标库: .\backup.ps1 restore -DbName <新库名>" -ForegroundColor Yellow
        Write-Host "恢复到生产库 ($prodDb) 需要 -DbName $prodDb -Force，且建议先 .\service.ps1 stop" -ForegroundColor Yellow
        return
    }
    if ($DbName -eq $prodDb -and !$Force) {
        throw "目标是生产库 $prodDb：确认已停服务后加 -Force 重跑（.\service.ps1 stop）"
    }

    if ($DumpFile) {
        $dumpPath = $DumpFile
        if (!(Test-Path $dumpPath)) { throw "找不到文件: $DumpFile" }
    } else {
        $inventory = @(Get-BackupInventory)
        if ($inventory.Count -eq 0) { throw "备份目录 $BackupDir 里没有任何备份" }
        $entry = $inventory[0]
        Write-Host "使用最新备份: $($entry.Stem)" -ForegroundColor Gray
        $tmpRoot = Join-Path $env:TEMP "memxy-restore-$(Get-Random)"
        $dumpPath = Expand-LatestArchive -Entry $entry -TmpRoot $tmpRoot
        Write-Host "★ 临时明文 dump: $dumpPath（恢复完记得删）" -ForegroundColor Yellow
    }

    $targetDsn = Get-DsnWithDb $dsn $DbName
    $exists = Invoke-PsqlQuery $dsn "SELECT 1 FROM pg_database WHERE datname='$DbName'"
    if ($exists -ne "1") {
        Invoke-PsqlQuery $dsn "CREATE DATABASE $DbName" | Out-Null
        Write-Host "已创建库 $DbName" -ForegroundColor Gray
    } elseif ($Force) {
        Write-Host "库 $DbName 已存在，--clean 覆盖其中对象 ..." -ForegroundColor Yellow
    } else {
        throw "库 $DbName 已存在；覆盖请加 -Force"
    }

    $pgRestore = Get-PgTool "pg_restore"
    $restoreArgs = @("--dbname=$targetDsn", "--no-password", "--no-owner", "--no-privileges")
    if ($Force) { $restoreArgs += @("--clean", "--if-exists") }
    Invoke-Native $pgRestore ($restoreArgs + $dumpPath) "pg_restore"

    # 恢复后冒烟：关键表行数
    foreach ($t in @("conversation_log", "memories", "living_doc")) {
        $n = try { Invoke-PsqlQuery $targetDsn "SELECT count(*) FROM $t" } catch { "?" }
        Write-Host ("  {0,-20} {1}" -f $t, $n) -ForegroundColor Gray
    }
    Write-Host "== 恢复完成：$DbName ==" -ForegroundColor Green
    if ($DbName -eq $prodDb) {
        Write-Host "生产库已恢复。启动服务并确认 /healthz 200:  .\service.ps1 start" -ForegroundColor Cyan
    }
}

# ---------------------------------------------------------------- status / task

function Do-Status {
    Write-Host "== mem-xy 备份状态 ==" -ForegroundColor Cyan
    Write-Host "备份目录: $BackupDir" -ForegroundColor Gray
    $inventory = @(Get-BackupInventory)
    if ($inventory.Count -eq 0) {
        Write-Host "  （没有任何备份，先跑 .\backup.ps1 init && .\backup.ps1 backup）" -ForegroundColor Yellow
    } else {
        $total = ($inventory | ForEach-Object { $_.Bytes } | Measure-Object -Sum).Sum
        Write-Host ("  共 {0} 组，占用 {1:N1} MB；最近 5 组:" -f $inventory.Count, ($total / 1MB)) -ForegroundColor Gray
        foreach ($e in ($inventory | Select-Object -First 5)) {
            Write-Host ("    {0}  {1,8:N1} MB  {2}" -f $e.Stem, ($e.Bytes / 1MB), $(if ($e.Encrypted) { "加密" } else { "★明文" })) `
                -ForegroundColor $(if ($e.Encrypted) { "Gray" } else { "Yellow" })
        }
    }
    if (Test-Path (Join-Path $RepoDir ".git")) {
        $remote = (Invoke-NativeMuted git @("-C", $RepoDir, "remote", "get-url", "origin")).Trim()
        if ($LASTEXITCODE -ne 0) { $remote = "" }
        $last = (Invoke-NativeMuted git @("-C", $RepoDir, "log", "-1", "--format=%ci %s")).Trim()
        if ($LASTEXITCODE -ne 0) { $last = "" }
        Write-Host "离机副本: $remote" -ForegroundColor Gray
        Write-Host "  最近提交: $last" -ForegroundColor Gray
    } else {
        Write-Host "离机副本: ★ 未绑定 GitHub 仓库（.\backup.ps1 init -GitRemote <url>）" -ForegroundColor Yellow
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host ("计划任务: {0} | 上次运行: {1} 结果: {2} | 下次: {3}" -f $task.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime) -ForegroundColor Gray
        if ($info.LastTaskResult -ne 0 -and $info.LastRunTime) {
            Write-Host "  ★ 上次备份任务退出码非 0，请手动跑 .\backup.ps1 backup 看原因" -ForegroundColor Red
        }
    } else {
        Write-Host "计划任务: ★ 未注册（.\backup.ps1 install-task）" -ForegroundColor Yellow
    }
}

function Do-InstallTask {
    $exe = (Get-Process -Id $PID).Path
    $scriptPath = $MyInvocation.ScriptName
    if (!$scriptPath) { $scriptPath = $PSCommandPath }
    $scriptDir = Split-Path $scriptPath
    $taskLog = Join-Path $scriptDir "backup-task.log"
    # ★ 全流重定向到 backup-task.log：计划任务运行时否则零输出可见，
    #   LastTaskResult 非 0 无从查因（静默失效头号敌人，2026-11-10 实踩：
    #   注册后手动触发返回 1，同款命令行手动跑却成功，没日志无法定位）。
    #   用 -Command "& '<script>' backup"：脚本未捕获 throw 时 pwsh 进程以
    #   退出码 1 结束，LastTaskResult 如实反映；成功自然退 0。
    #   日志量约 15 行/次 × 每日一次 ≈ 500KB/年，追加不轮转（可接受）。
    $arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"& '$scriptPath' backup *>> '$taskLog'`""
    $action = New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $scriptDir
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Force | Out-Null
    Write-Host "已注册每日计划任务 '$TaskName'（$At，用 $exe）" -ForegroundColor Green
    Write-Host "★ 警告：本机 SASE 策略禁止计划任务上下文跑 pg_dump/psql（2026-11-10 实测，" -ForegroundColor Yellow
    Write-Host "  decisions-log #100）——该模式在本机必然失败，仅作环境差异保留；每日备份由" -ForegroundColor Yellow
    Write-Host "  backend 内置调度负责（backup.daily_at，默认 12:30，长期方案，见 #101）。" -ForegroundColor Yellow
    Write-Host "任务日志: $taskLog（追加式，失败原因看这里）" -ForegroundColor Gray
    Write-Host "验证: .\backup.ps1 status；手动触发一次: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Gray
}

function Do-UninstallTask {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已删除计划任务 '$TaskName'" -ForegroundColor Green
}

function Show-BackupHelp {
    Write-Host ""
    Write-Host "  mem-xy 备份脚本（单机三层防线：本地每日 dump → 加密分卷 → GitHub 离机副本）" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  .\backup.ps1 init [-GitRemote <url>]   首次初始化（依赖检查 + 生成加密密码 + 绑定备份仓库）"
    Write-Host "  .\backup.ps1 backup                    立即备份（计划任务每日自动跑的就是它）"
    Write-Host "  .\backup.ps1 verify                    恢复演练：最新备份 → 临时库 → 对账行数（建议每季度）"
    Write-Host "  .\backup.ps1 restore -DbName <db> [-DumpFile <path>] [-Force]"
    Write-Host "  .\backup.ps1 status                    备份/同步/计划任务状态"
    Write-Host "  .\backup.ps1 install-task [-At 12:30]  注册每日计划任务（★本机 SASE 拦 pg_dump，"
    Write-Host "                                         日常由 backend 内置调度负责，见 #100）"
    Write-Host "  .\backup.ps1 uninstall-task"
    Write-Host ""
    Write-Host "  新机器迁移: 装 PG+pgvector/7z/git → 建空库 memxy → 拷回 ~/.memxy（或从备份卷解出）→" -ForegroundColor Yellow
    Write-Host "              .\backup.ps1 restore -DbName memxy -Force → .\service.ps1 start" -ForegroundColor Yellow
    Write-Host ""
}

switch ($Action) {
    "init"          { Do-Init }
    "backup"        { Do-Backup }
    "verify"        { Do-Verify }
    "restore"       { Do-Restore }
    "status"        { Do-Status }
    "install-task"  { Do-InstallTask }
    "uninstall-task" { Do-UninstallTask }
    "help"          { Show-BackupHelp }
}
