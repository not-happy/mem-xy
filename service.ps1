# mem-xy - Start/Stop/Restart/Status/Log Script (参考 dingding/service.ps1)
# ★ 进程收敛（2026-11-10）：日常 UI 由 backend 托管（frontend/dist 构建产物，
#   http://127.0.0.1:8787/），start/stop/restart 默认只管 backend；
#   dev frontend（npm run dev, 5273）仅在改前端代码时单独启停。
# Usage:
#   .\service.ps1 start            - 后台启动 backend（含 UI 托管）
#   .\service.ps1 start frontend   - 启动 Vite 开发服务器（改前端代码时用，热更新）
#   .\service.ps1 start worker     - 单独启动异步 worker（extract --queue-all 之后用）
#   .\service.ps1 stop             - 停止 backend（及在跑的 dev frontend / worker）
#   .\service.ps1 stop worker      - 单独停止 worker
#   .\service.ps1 restart          - 重启 backend
#   .\service.ps1 status           - 查看运行状态（含 embedding 模型就绪状态）
#   .\service.ps1 log              - 实时看所有日志（Ctrl+C 退出）
#   .\service.ps1 log backend      - 只看 backend 日志（frontend / worker 同理）
#   .\service.ps1 backup           - 立即备份数据库（详见 .\backup.ps1 help）
#   .\service.ps1 verify-backup    - 恢复演练：最新备份 → 临时库 → 对账行数
#   .\service.ps1 restore -DbName <db> [-Force]  - 真实恢复（委托 backup.ps1）
#   .\service.ps1 install-watchdog   - 注册计划任务：每 5 分钟探活并自动拉起 backend/worker
#   .\service.ps1 uninstall-watchdog - 移除上述计划任务
#   .\service.ps1 watchdog           - 手动跑一次 watchdog（调试用）

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "log", "status", "help", "backup", "restore",
                 "verify-backup", "watchdog", "install-watchdog", "uninstall-watchdog")]
    [string]$Action = "help",

    [Parameter(Position = 1)]
    [ValidateSet("all", "backend", "frontend", "worker")]
    [string]$Target = "all",

    # 透传给 backup.ps1 的剩余参数（如 restore -DbName memxy -Force）
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$root = $PSScriptRoot
$serviceStateFile = "$root\.service-pids"
$backendLog = "$root\backend.log"
$frontendLog = "$root\frontend.log"
$workerLog = "$root\worker.log"
$backendPort = 8787
# ★ 5273 专属端口（5173 是 Vite 默认、归 dingding 项目），与 vite.config.js 的 strictPort 配套
$frontendPort = 5273
# bge-m3 冷启动实测 15~33s（会漂），留足余量
$modelReadyTimeoutSeconds = 120

function Get-PortOwners {
    param([int]$Port)

    @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Test-PidAlive {
    param($ProcessId)

    if (!$ProcessId) { return $false }
    return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Stop-TrackedProcess {
    param(
        [int]$ProcessId,
        [string]$Label
    )

    if (!(Test-PidAlive $ProcessId)) { return $false }

    taskkill /F /T /PID $ProcessId 2>$null | Out-Null
    if (Test-PidAlive $ProcessId) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (Test-PidAlive $ProcessId) {
        Write-Host "Failed to stop $Label (PID: $ProcessId)" -ForegroundColor Red
        return $false
    }
    Write-Host "Stopped $Label (PID: $ProcessId)" -ForegroundColor Green
    return $true
}

function Wait-ForPortOwner {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $owners = Get-PortOwners -Port $Port
        if ($owners.Count -gt 0) { return $owners[0] }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    return $null
}

function Get-HealthStatusCode {
    # /healthz：模型就绪 200，加载中 503（model_loading），服务没起 → $null
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
            -Uri "http://127.0.0.1:$backendPort/healthz"
        return [int]$response.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return $null
    }
}

function Get-DistFreshness {
    # 前端构建产物新鲜度（进程收敛后 UI = frontend/dist，由 backend 托管）：
    # "missing" = 没构建过；"stale" = src 里有比 dist 新的改动（UI 是旧版）；"ok" = 新鲜
    param([string]$DistIndex = "$root\frontend\dist\index.html")

    if (!(Test-Path $DistIndex)) { return "missing" }
    $distTime = (Get-Item $DistIndex).LastWriteTime
    $newer = Get-ChildItem "$root\frontend\src" -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt $distTime } | Select-Object -First 1
    if ($newer) { return "stale" }
    return "ok"
}

function Write-UiHint {
    # backend 起来后提示 UI 状态；构建缺失/过期只提醒不阻塞（API 不受影响）
    switch (Get-DistFreshness) {
        "ok" {
            Write-Host "UI      : http://127.0.0.1:$backendPort/ （backend 托管 frontend/dist）" -ForegroundColor Green
        }
        "missing" {
            Write-Host "UI 未构建（frontend/dist 不存在）：cd frontend && npm run build" -ForegroundColor Yellow
            Write-Host "  API 不受影响；开发模式可用 .\service.ps1 start frontend（端口 $frontendPort）" -ForegroundColor Yellow
        }
        "stale" {
            Write-Host "UI 构建产物已过期（frontend/src 有更新改动）：cd frontend && npm run build" -ForegroundColor Yellow
            Write-Host "  当前 http://127.0.0.1:$backendPort/ 显示的是旧版界面" -ForegroundColor Yellow
        }
    }
}

function Wait-ForModelReady {
    param([int]$TimeoutSeconds)

    Write-Host "Waiting for embedding model (bge-m3, 冷启动约 15~33s)..." -ForegroundColor Cyan
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $code = Get-HealthStatusCode
        if ($code -eq 200) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Read-State {
    if (Test-Path $serviceStateFile) {
        try { return (Get-Content $serviceStateFile -Raw | ConvertFrom-Json) } catch {}
    }
    return [pscustomobject]@{
        BackendLauncher  = $null
        FrontendLauncher = $null
        WorkerLauncher   = $null
        BackendPortOwner = $null
        FrontendPortOwner = $null
    }
}

function Write-State {
    param($State)

    $hasAny = $State.BackendLauncher -or $State.FrontendLauncher -or $State.WorkerLauncher
    if ($hasAny) {
        $State | ConvertTo-Json | Set-Content $serviceStateFile
    } elseif (Test-Path $serviceStateFile) {
        Remove-Item $serviceStateFile -Force
    }
}

function Rotate-LogFile {
    # ★ 以前是 `> log` 每次启动直接截断——上一轮运行的故障证据被销毁
    #   （EXPERT-REVIEW B5）。改为：超过 20MB 才滚动成 .old（保留一代），否则追加。
    param(
        [string]$Path,
        [int]$MaxBytes = 20MB
    )

    if ((Test-Path $Path) -and ((Get-Item $Path).Length -gt $MaxBytes)) {
        Move-Item -Force $Path "$Path.old"
    }
}

function Start-Launcher {
    param(
        [string]$WorkDir,
        [string]$CommandLine,
        [string]$LogFile
    )

    Rotate-LogFile -Path $LogFile
    Start-Process -WindowStyle Hidden -PassThru -FilePath "cmd" `
        -ArgumentList "/c cd /d $WorkDir && $CommandLine >> `"$LogFile`" 2>&1"
}

function Start-Backend {
    $state = Read-State
    if ((Get-PortOwners -Port $backendPort).Count -gt 0 -or (Test-PidAlive $state.BackendLauncher)) {
        Write-Host "Backend 已在运行（端口 $backendPort 被占用）。先执行 .\service.ps1 stop backend" -ForegroundColor Yellow
        return
    }

    Write-Host "Starting backend (python -m app.commands.serve)..." -ForegroundColor Cyan
    $launcher = Start-Launcher -WorkDir "$root\backend" `
        -CommandLine "python -m app.commands.serve" -LogFile $backendLog

    $owner = Wait-ForPortOwner -Port $backendPort
    if (!$owner) {
        Write-Host "Backend 端口 $backendPort 未起来，查看 .\service.ps1 log backend" -ForegroundColor Red
        return
    }

    $state = Read-State
    $state.BackendLauncher = $launcher.Id
    $state.BackendPortOwner = $owner
    Write-State $state

    if (Wait-ForModelReady -TimeoutSeconds $modelReadyTimeoutSeconds) {
        Write-Host "Backend ready (model warm)  ->  http://127.0.0.1:$backendPort" -ForegroundColor Green
    } else {
        Write-Host "Backend HTTP 已起，但模型 $modelReadyTimeoutSeconds s 内未就绪（503 model_loading），" -ForegroundColor Yellow
        Write-Host "可用 .\service.ps1 status 复查，或看 .\service.ps1 log backend" -ForegroundColor Yellow
    }
    Write-UiHint
}

function Start-Frontend {
    # ★ 仅开发模式：日常 UI 由 backend 托管（frontend/dist），不需要常驻 dev 服务器。
    #   改前端代码时才用它（热更新，/v1 与 /healthz 代理到 $backendPort）。
    $state = Read-State
    if ((Get-PortOwners -Port $frontendPort).Count -gt 0 -or (Test-PidAlive $state.FrontendLauncher)) {
        Write-Host "Frontend(dev) 已在运行（端口 $frontendPort 被占用）。先执行 .\service.ps1 stop frontend" -ForegroundColor Yellow
        return
    }

    Write-Host "Starting frontend dev server (npm run dev，仅开发模式)..." -ForegroundColor Cyan
    $launcher = Start-Launcher -WorkDir "$root\frontend" `
        -CommandLine "npm run dev" -LogFile $frontendLog

    $owner = Wait-ForPortOwner -Port $frontendPort
    $state = Read-State
    $state.FrontendLauncher = $launcher.Id
    $state.FrontendPortOwner = $owner
    Write-State $state

    if ($owner) {
        Write-Host "Frontend(dev) ready  ->  http://localhost:$frontendPort （改完记得 npm run build 更新托管产物）" -ForegroundColor Green
    } else {
        Write-Host "Frontend 端口 $frontendPort 未起来，查看 .\service.ps1 log frontend" -ForegroundColor Red
    }
}

function Start-Worker {
    $state = Read-State
    if (Test-PidAlive $state.WorkerLauncher) {
        Write-Host "Worker 已在运行 (PID: $($state.WorkerLauncher))。先执行 .\service.ps1 stop worker" -ForegroundColor Yellow
        return
    }

    Write-Host "Starting worker (python -m app.worker)..." -ForegroundColor Cyan
    $launcher = Start-Launcher -WorkDir "$root\backend" `
        -CommandLine "python -m app.worker" -LogFile $workerLog

    Start-Sleep -Seconds 1
    if (Test-PidAlive $launcher.Id) {
        $state = Read-State
        $state.WorkerLauncher = $launcher.Id
        Write-State $state
        Write-Host "Worker ready (PID: $($launcher.Id))，日志 worker.log" -ForegroundColor Green
    } else {
        Write-Host "Worker 启动即退出，查看 .\service.ps1 log worker" -ForegroundColor Red
    }
}

function Start-Services {
    param([string]$Which)

    switch ($Which) {
        "all" {
            # ★ 进程收敛后 "all" = backend（UI 已托管在内），不再拉起常驻 dev frontend
            Start-Backend
            if ((Get-PortOwners -Port $frontendPort).Count -gt 0) {
                Write-Host "（dev frontend 仍在 $frontendPort 跑着，不影响托管 UI；不用可 stop frontend）" -ForegroundColor DarkGray
            }
        }
        "backend"  { Start-Backend }
        "frontend" { Start-Frontend }
        "worker"   { Start-Worker }
    }
}

function Stop-OneService {
    param(
        [string]$Which,
        $State
    )

    $stopped = $false
    if ($Which -eq "backend" -or $Which -eq "all") {
        $stopped = (Stop-TrackedProcess $State.BackendLauncher "Backend launcher") -or $stopped
        $stopped = (Stop-TrackedProcess $State.BackendPortOwner "Backend service") -or $stopped
        foreach ($processId in (Get-PortOwners -Port $backendPort)) {
            $stopped = (Stop-TrackedProcess $processId "Backend port $backendPort") -or $stopped
        }
        $State.BackendLauncher = $null; $State.BackendPortOwner = $null
    }
    if ($Which -eq "frontend" -or $Which -eq "all") {
        $stopped = (Stop-TrackedProcess $State.FrontendLauncher "Frontend launcher") -or $stopped
        $stopped = (Stop-TrackedProcess $State.FrontendPortOwner "Frontend service") -or $stopped
        foreach ($processId in (Get-PortOwners -Port $frontendPort)) {
            $stopped = (Stop-TrackedProcess $processId "Frontend port $frontendPort") -or $stopped
        }
        $State.FrontendLauncher = $null; $State.FrontendPortOwner = $null
    }
    if ($Which -eq "worker" -or $Which -eq "all") {
        $stopped = (Stop-TrackedProcess $State.WorkerLauncher "Worker") -or $stopped
        $State.WorkerLauncher = $null
    }
    return $stopped
}

function Stop-Services {
    param([string]$Which)

    $state = Read-State
    $stopped = Stop-OneService -Which $Which -State $state
    Write-State $state

    if ($stopped) {
        Write-Host "Stopped ($Which)." -ForegroundColor Green
    } else {
        Write-Host "No running services found ($Which)." -ForegroundColor Yellow
    }
}

function Show-Status {
    $state = Read-State

    $backendOwners = Get-PortOwners -Port $backendPort
    $frontendOwners = Get-PortOwners -Port $frontendPort
    $backendUp = $backendOwners.Count -gt 0
    $frontendUp = $frontendOwners.Count -gt 0
    $workerUp = Test-PidAlive $state.WorkerLauncher

    Write-Host ("Backend : {0} | Port {1} owner: {2}" -f `
        $(if ($backendUp) { "Running" } else { "Stopped" }), $backendPort, `
        $(if ($backendUp) { $backendOwners -join ", " } else { "none" })) `
        -ForegroundColor $(if ($backendUp) { "Green" } else { "Red" })

    if ($backendUp) {
        $code = Get-HealthStatusCode
        $model = if ($code -eq 200) { "warm (healthz 200)" }
                 elseif ($code -eq 503) { "loading (healthz 503)" }
                 else { "unknown (healthz $code)" }
        Write-Host "          embedding model: $model  ->  http://127.0.0.1:$backendPort" -ForegroundColor DarkGray
        $ui = Get-DistFreshness
        $uiText = switch ($ui) {
            "ok"      { "http://127.0.0.1:$backendPort/ （frontend/dist 新鲜）" }
            "stale"   { "http://127.0.0.1:$backendPort/ （★ dist 已过期，界面是旧版：cd frontend && npm run build）" }
            "missing" { "未构建（cd frontend && npm run build；开发用 start frontend）" }
        }
        Write-Host ("          UI (托管): {0}" -f $uiText) `
            -ForegroundColor $(if ($ui -eq "ok") { "DarkGray" } else { "Yellow" })
    }

    # dev frontend 不是日常进程：停着是常态，不用红色报警
    Write-Host ("Frontend(dev): {0} | Port {1} owner: {2}" -f `
        $(if ($frontendUp) { "Running" } else { "Stopped" }), $frontendPort, `
        $(if ($frontendUp) { $frontendOwners -join ", " } else { "none" })) `
        -ForegroundColor $(if ($frontendUp) { "Green" } else { "DarkGray" })
    if ($frontendUp) {
        Write-Host "          ->  http://localhost:$frontendPort （开发模式；日常 UI 用上面托管地址）" -ForegroundColor DarkGray
    } else {
        Write-Host "          日常无需启动（UI 由 backend 托管）；改前端代码时 .\service.ps1 start frontend" -ForegroundColor DarkGray
    }

    Write-Host ("Worker  : {0}{1}" -f `
        $(if ($workerUp) { "Running" } else { "Stopped" }), `
        $(if ($workerUp) { " (PID: $($state.WorkerLauncher))" } else { "" })) `
        -ForegroundColor $(if ($workerUp) { "Green" } else { "Red" })
}

function Show-Log {
    param([string]$Which)

    $files = @()
    foreach ($entry in @(
        @{ Name = "backend"; File = $backendLog },
        @{ Name = "frontend"; File = $frontendLog },
        @{ Name = "worker"; File = $workerLog }
    )) {
        if ($Which -eq "all" -or $Which -eq $entry.Name) {
            if (Test-Path $entry.File) { $files += $entry.File }
            else { Write-Host "$($entry.Name).log not found" -ForegroundColor Yellow }
        }
    }

    if ($files.Count -eq 0) {
        Write-Host "No log files found. Start services first." -ForegroundColor Yellow
        return
    }

    Write-Host "Tailing logs (Ctrl+C to exit)..." -ForegroundColor Cyan
    Write-Host ""
    Get-Content $files -Tail 50 -Wait
}

function Install-Watchdog {
    # ★ Windows 原生守护（EXPERT-REVIEW §5.2.3）：计划任务每 5 分钟跑 watchdog.ps1，
    #   探 /healthz + 补齐 worker。不引入 NSSM/容器——单机 Windows 下运维成本最低。
    # ★ 用 XML + schtasks 而不是 Register-ScheduledTask：后者表达不了"无限期重复"——
    #   -RepetitionDuration ([TimeSpan]::MaxValue) 只在 PS 5.1 能过，pwsh 7 会序列化成
    #   P99999999DT23H59M59S 被任务计划程序拒绝（超出范围的值，2026-11-09 实踩）。
    #   XML 里 <Repetition> 省略 <Duration> 即真正的无限期；且 Exec 的
    #   Command/Arguments 分属两个元素，没有 /TR 一行串的引号转义坑。
    $taskName = "memxy-watchdog"
    $psExe = (Get-Process -Id $PID).Path
    $startAt = (Get-Date).AddMinutes(1).ToString("yyyy-MM-ddTHH:mm:ss")
    $userId = "$env:USERDOMAIN\$env:USERNAME"
    # ★ 静默启动（2026-11-10 实踩：任务直接跑 pwsh 时 -WindowStyle Hidden 也会
    #   先闪现控制台窗口再隐藏，每 5 分钟打断用户一次）。解法：任务跑无窗口的
    #   wscript.exe（GUI 子系统），由 vbs 用 WshShell.Run(..., 0, True) 隐藏 pwsh
    #   窗口、等待完成并把退出码传回 LastTaskResult；//B 抑制 vbs 自身错误弹窗。
    $vbsPath = "$root\watchdog-silent.vbs"
    $vbs = @"
' mem-xy watchdog silent launcher (generated by service.ps1 install-watchdog; do not edit)
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("""$psExe"" -NoProfile -ExecutionPolicy Bypass -File ""$root\watchdog.ps1""", 0, True)
"@
    Set-Content -Path $vbsPath -Value $vbs -Encoding Ascii
    $wscript = Join-Path $env:SystemRoot "System32\wscript.exe"
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>mem-xy: 每 5 分钟探活 backend/worker，挂了自动拉起（service.ps1 install-watchdog）</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>$startAt</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$userId</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$wscript</Command>
      <Arguments>//B "$vbsPath"</Arguments>
      <WorkingDirectory>$root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
    $xmlPath = Join-Path $env:TEMP "memxy-watchdog-task.xml"
    # schtasks /XML 最稳的是任务计划程序自家导出格式：UTF-16 LE + encoding="UTF-16"
    # （UTF-8 带 BOM 会被拒："无法切换编码"，2026-11-09 实踩）
    [System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.Encoding]::Unicode)
    schtasks.exe /Create /TN $taskName /XML $xmlPath /F | Out-Null
    $exit = $LASTEXITCODE
    Remove-Item $xmlPath -Force -ErrorAction SilentlyContinue
    if ($exit -ne 0) {
        Write-Host "注册计划任务失败（schtasks 退出码 $exit）" -ForegroundColor Red
        return
    }
    Write-Host "已注册计划任务 $taskName（每 5 分钟，无限期；仅当前用户登录时运行）。" -ForegroundColor Green
    Write-Host "手动验证：schtasks /Run /TN $taskName，然后 schtasks /Query /TN $taskName /V /FO LIST" -ForegroundColor DarkGray
}

function Uninstall-Watchdog {
    $taskName = "memxy-watchdog"
    schtasks.exe /Query /TN $taskName *> $null
    if ($LASTEXITCODE -eq 0) {
        schtasks.exe /Delete /TN $taskName /F | Out-Null
        Remove-Item "$root\watchdog-silent.vbs" -Force -ErrorAction SilentlyContinue
        Write-Host "已移除计划任务 $taskName" -ForegroundColor Green
    } else {
        Write-Host "计划任务 $taskName 不存在" -ForegroundColor Yellow
    }
}

function Show-Help {
    Write-Host ""
    Write-Host "  mem-xy 服务管理脚本" -ForegroundColor Cyan
    Write-Host "  ====================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  用法: .\service.ps1 <命令> [对象]" -ForegroundColor White
    Write-Host ""
    Write-Host "  命令:" -ForegroundColor Yellow
    Write-Host "    start              后台启动 backend（含 UI 托管，无弹窗）"
    Write-Host "    stop               停止 backend + dev frontend + worker"
    Write-Host "    restart            重启 backend（UI 随之更新到当前 dist）"
    Write-Host "    status             查看运行状态（含 embedding 模型就绪、UI 构建新鲜度）"
    Write-Host "    log                实时查看所有日志（Ctrl+C 退出）"
    Write-Host "    backup             立即备份数据库（pg_dump → 加密分卷 → GitHub 离机副本）"
    Write-Host "    verify-backup      恢复演练（最新备份恢复到临时库并对账行数）"
    Write-Host "    restore            真实恢复（restore -DbName <db> [-Force]；init/status/install-task 见 .\backup.ps1）"
    Write-Host "    install-watchdog   注册计划任务：每 5 分钟探活，backend/worker 挂了自动拉起"
    Write-Host "    uninstall-watchdog 移除 watchdog 计划任务"
    Write-Host "    watchdog           手动跑一次 watchdog（调试用）"
    Write-Host "    help               显示本帮助信息"
    Write-Host ""
    Write-Host "  对象（可选，默认 all）:" -ForegroundColor Yellow
    Write-Host "    backend            python -m app.commands.serve（端口 $backendPort，模型冷启动 15~33s）"
    Write-Host "                       ★ 日常 UI 由它托管：http://127.0.0.1:$backendPort/（frontend/dist）"
    Write-Host "    frontend           npm run dev（端口 $frontendPort，热更新开发模式；仅改前端代码时用）"
    Write-Host "    worker             python -m app.worker（异步抽取队列，无端口，按需单独启停）"
    Write-Host ""
    Write-Host "  示例:" -ForegroundColor Yellow
    Write-Host "    .\service.ps1 start worker    # extract --queue-all 之后让 worker 后台慢慢抽"
    Write-Host "    .\service.ps1 log backend     # 只看后端日志"
    Write-Host "    cd frontend; npm run build    # 改完前端后重新构建，restart backend 生效"
    Write-Host ""
    Write-Host "  日志文件: backend.log / frontend.log / worker.log（项目根目录）" -ForegroundColor Yellow
    Write-Host ""
}

switch ($Action) {
    "start"   { Start-Services $Target }
    "stop"    { Stop-Services $Target }
    "restart" { Stop-Services $Target; Start-Services $Target }
    "log"     { Show-Log $Target }
    "status"  { Show-Status }
    "backup"       { & "$root\backup.ps1" backup @ExtraArgs; exit $LASTEXITCODE }
    "verify-backup" { & "$root\backup.ps1" verify @ExtraArgs; exit $LASTEXITCODE }
    "restore"      { & "$root\backup.ps1" restore @ExtraArgs; exit $LASTEXITCODE }
    "watchdog"           { & "$root\watchdog.ps1" @ExtraArgs; exit $LASTEXITCODE }
    "install-watchdog"   { Install-Watchdog }
    "uninstall-watchdog" { Uninstall-Watchdog }
    "help"    { Show-Help }
}
