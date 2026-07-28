$ErrorActionPreference = "Stop"
$WorkspacePath = $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Some launchers expose both PATH and Path. Windows PowerShell's Start-Process
# builds a case-insensitive environment dictionary and otherwise aborts before
# either local service is started.
$ProcessPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
[Environment]::SetEnvironmentVariable("Path", $null, "Process")
[Environment]::SetEnvironmentVariable("PATH", $ProcessPath, "Process")

$BackendPython = $null
$PythonCandidates = @(
    (Join-Path $WorkspacePath ".venv\Scripts\python.exe"),
    (Join-Path $WorkspacePath ".venv-codex-backup\Scripts\python.exe")
)
$FrontendPath = Join-Path $WorkspacePath "frontend"
$DataPath = Join-Path $WorkspacePath "data"
$LogPath = Join-Path $DataPath "logs"
$LocalUrl = "http://127.0.0.1:3000"

foreach ($Candidate in $PythonCandidates) {
    if (-not (Test-Path -LiteralPath $Candidate)) { continue }
    try {
        & $Candidate -c "from app.services.f2_links import ensure_f2_runtime; ensure_f2_runtime(); import fastapi, sqlalchemy, f2, multipart" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $BackendPython = $Candidate
            break
        }
    } catch {}
}
if (-not $BackendPython) {
    throw "尚未安装完整依赖（含 F2 与文件上传组件），请先运行 .\scripts\setup.ps1"
}
if (-not (Test-Path -LiteralPath (Join-Path $FrontendPath "node_modules"))) {
    throw "前端依赖不存在，请先运行 .\scripts\setup.ps1"
}
New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
New-Item -ItemType Directory -Force -Path $LogPath | Out-Null

function Get-ListeningProcessId([int]$Port) {
    $Pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    foreach ($Line in (& netstat.exe -ano -p tcp)) {
        if ($Line -match $Pattern) {
            return [int]$Matches[1]
        }
    }
    return $null
}

foreach ($RequiredPort in @(8000, 3000)) {
    $ExistingPid = Get-ListeningProcessId $RequiredPort
    if ($ExistingPid) {
        throw "端口 $RequiredPort 仍被进程 $ExistingPid 占用。请先双击‘停止.cmd’，若仍失败请重启 Windows 后再启动。"
    }
}

# Next.js development output can survive an interrupted Windows shutdown with
# a stale app-path manifest. In that state the home page still works while
# dynamic routes such as /works/[id] incorrectly return the framework 404.
# This directory contains generated files only; rebuilding it keeps the route
# table in sync without touching user data or production build output.
$FrontendDevCachePath = Join-Path $FrontendPath ".next\dev"
if (Test-Path -LiteralPath $FrontendDevCachePath) {
    Remove-Item -LiteralPath $FrontendDevCachePath -Recurse -Force
}

$Backend = Start-Process -FilePath $BackendPython -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" -WorkingDirectory $WorkspacePath -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogPath "backend.log") -RedirectStandardError (Join-Path $LogPath "backend-error.log")
try {
    $NpmCommand = (Get-Command npm.cmd -ErrorAction Stop).Source
    $Frontend = Start-Process -FilePath $NpmCommand -ArgumentList "run","dev" -WorkingDirectory $FrontendPath -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogPath "frontend.log") -RedirectStandardError (Join-Path $LogPath "frontend-error.log")
} catch {
    Stop-Process -Id $Backend.Id -Force -ErrorAction SilentlyContinue
    throw
}

$Ready = $false
for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
    try {
        $BackendResponse = Invoke-RestMethod -TimeoutSec 1 "http://127.0.0.1:8000/health"
        $FrontendResponse = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 "http://127.0.0.1:3000"
        if ($BackendResponse.status -eq "healthy" -and $BackendResponse.api_contract -eq 4 -and $FrontendResponse.StatusCode -eq 200) {
            $Ready = $true
            break
        }
    } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $Ready) {
    foreach ($ManagedProcessId in @($Backend.Id, $Frontend.Id, (Get-ListeningProcessId 8000), (Get-ListeningProcessId 3000))) {
        if (-not $ManagedProcessId) { continue }
        try {
            & taskkill.exe /PID $ManagedProcessId /T /F 2>$null | Out-Null
        } catch {
            # 进程可能在就绪检查或清理期间自行退出；这已经达到停止目的。
        }
    }
    Remove-Item -LiteralPath (Join-Path $WorkspacePath "data\runtime.json") -Force -ErrorAction SilentlyContinue
    throw "服务未能在 20 秒内就绪；请在 PowerShell 中手动运行后端和前端以查看错误。"
}
$BackendListenerPid = Get-ListeningProcessId 8000
$FrontendListenerPid = Get-ListeningProcessId 3000
if (-not $BackendListenerPid -or -not $FrontendListenerPid) {
    throw "服务已响应，但无法识别监听进程。请运行‘停止.cmd’后重试。"
}
$RuntimeState = @{
    backend_pid = $BackendListenerPid
    frontend_pid = $FrontendListenerPid
    backend_launcher_pid = $Backend.Id
    frontend_launcher_pid = $Frontend.Id
    api_contract = 4
}
$RuntimeState | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $WorkspacePath "data\runtime.json") -Encoding UTF8
Write-Host "TokBrain 已启动：$LocalUrl。关闭服务请双击‘停止.cmd’。" -ForegroundColor Green
if ($env:TOKBRAIN_SKIP_LOCAL_UI_OPEN -ne "1") {
    try {
        Start-Process -FilePath $LocalUrl
    } catch {
        Write-Warning "本地网页未能自动打开，请手动访问 $LocalUrl"
    }
}
