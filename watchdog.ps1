# mem-xy watchdog — 探活并拉起静默挂掉的部件（EXPERT-REVIEW B5 / §5.2.3）
#
# 背景：worker 崩了之后无人拉起，直到下次后端重启——2026-11-02 真实发生过
#       （批准积压数日、UI 无报错）。守护用 Windows 原生计划任务，不引入 NSSM/容器。
#
# 用法：
#   .\watchdog.ps1                    # 手动跑一次（调试用；正式跑法见下）
#   .\service.ps1 install-watchdog    # 注册计划任务，每 5 分钟自动跑本脚本
#
# 逻辑：
#   1. 探 http://127.0.0.1:8787/healthz —— 有 HTTP 响应（200/503）即后端活着；
#      连不上 = 挂了 → service.ps1 start backend（lifespan 会自动补齐 worker）
#   2. 后端活着 → POST /v1/workers/ensure 把 worker 补齐到 target（幂等，已达标不动）
#   3. 只在发生动作/出错时写 watchdog.log（追加），正常运行零输出

param(
    [int]$BackendPort = 8787,
    [int]$WorkerTarget = 2
)

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$watchdogLog = "$root\watchdog.log"

function Write-WatchdogLog {
    param([string]$Message)
    Add-Content -Path $watchdogLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
}

# 1. 探活
$alive = $false
try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 `
        -Uri "http://127.0.0.1:$BackendPort/healthz" | Out-Null
    $alive = $true
} catch {
    # 503 model_loading 之类也有 Response —— 进程活着，只是模型没就绪
    if ($_.Exception.Response) { $alive = $true }
}

if (!$alive) {
    Write-WatchdogLog "backend 无响应，执行 service.ps1 start backend"
    & "$root\service.ps1" start backend
    exit $LASTEXITCODE
}

# 2. 补齐 worker（幂等）
try {
    $body = @{ target = $WorkerTarget } | ConvertTo-Json
    $result = Invoke-RestMethod -Method Post -TimeoutSec 15 `
        -Uri "http://127.0.0.1:$BackendPort/v1/workers/ensure" `
        -ContentType "application/json" -Body $body
    $started = @($result.workers | Where-Object { $_.alive }).Count
    if ($started -lt $WorkerTarget) {
        Write-WatchdogLog "ensure 后存活 worker=$started < target=$WorkerTarget"
    }
} catch {
    # ensure 失败不退出报错：5 分钟后计划任务会再试
    Write-WatchdogLog "ensure 调用失败: $($_.Exception.Message)"
    exit 1
}
exit 0
