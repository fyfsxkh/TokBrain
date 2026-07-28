$ErrorActionPreference = "Stop"
$WorkspacePath = Split-Path -Parent $PSScriptRoot
$StatePath = Join-Path $WorkspacePath "data\runtime.json"

function Get-ListeningProcessId([int]$Port) {
    $Pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    foreach ($Line in (& netstat.exe -ano -p tcp)) {
        if ($Line -match $Pattern) {
            return [int]$Matches[1]
        }
    }
    return $null
}

$ManagedProcessIds = @()
if (Test-Path -LiteralPath $StatePath) {
    $RuntimeState = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
    $ManagedProcessIds += @(
        $RuntimeState.backend_pid,
        $RuntimeState.frontend_pid,
        $RuntimeState.backend_launcher_pid,
        $RuntimeState.frontend_launcher_pid
    )
    # 旧版启动器记录的是很快退出的包装进程。仅在存在本项目运行状态时
    # 纳入真实监听进程，避免误停其他占用 3000/8000 端口的软件。
    $ManagedProcessIds += @(Get-ListeningProcessId 8000)
    $ManagedProcessIds += @(Get-ListeningProcessId 3000)
} else {
    Write-Host "没有找到本项目的运行状态；未停止任何进程。"
    exit 0
}

foreach ($ManagedProcessId in @($ManagedProcessIds | Where-Object { $_ } | Select-Object -Unique)) {
    if ($ManagedProcessId -and (Get-Process -Id $ManagedProcessId -ErrorAction SilentlyContinue)) {
        try {
            & taskkill.exe /PID $ManagedProcessId /T /F 2>$null | Out-Null
        } catch {}
        if (Get-Process -Id $ManagedProcessId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $ManagedProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

Start-Sleep -Milliseconds 400
$RemainingListeners = @(
    Get-ListeningProcessId 8000
    Get-ListeningProcessId 3000
) | Where-Object { $_ } | Select-Object -Unique
if ($RemainingListeners) {
    throw "未能停止 TokBrain 进程 $($RemainingListeners -join ', ')。请以管理员身份运行‘停止.cmd’，或重启 Windows 后再启动。"
}

Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
Write-Host "服务已停止（包括旧版启动器遗留的监听进程）。"
