$ErrorActionPreference = "Stop"

$WorkspacePath = $PSScriptRoot
$FrontendPath = Join-Path $WorkspacePath "frontend"
$DataPath = Join-Path $WorkspacePath "data"
$LogPath = Join-Path $DataPath "logs"
$StatePath = Join-Path $DataPath "runtime.json"
$BackendPython = Join-Path $WorkspacePath ".venv\Scripts\python.exe"
$FrontendPort = 3000
$FrontendHost = "127.0.0.1"
$LocalUrl = "http://${FrontendHost}:$FrontendPort"
Set-Location $WorkspacePath

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:NODE_ENV = "production"

# Some launchers expose both PATH and Path. Windows PowerShell's Start-Process
# builds a case-insensitive environment dictionary and otherwise aborts before
# either local service is started.
$ProcessPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
[Environment]::SetEnvironmentVariable("Path", $null, "Process")
[Environment]::SetEnvironmentVariable("PATH", $ProcessPath, "Process")

function Get-TextSha256 {
    param([AllowEmptyString()][string]$Value)

    $Algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Value)
        return -join ($Algorithm.ComputeHash($Bytes) | ForEach-Object {
            $_.ToString("x2")
        })
    }
    finally {
        $Algorithm.Dispose()
    }
}

function Get-ProcessIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $Process) {
        return $null
    }
    try {
        $ExecutablePath = [string]$Process.Path
        $CreationTime = $Process.StartTime.ToUniversalTime()
    }
    catch {
        return $null
    }
    if (-not $ExecutablePath) {
        return $null
    }
    $CommandLineSha256 = $null
    try {
        $CimProcess = Get-CimInstance -ClassName Win32_Process -Filter (
            "ProcessId = $ProcessId"
        ) -ErrorAction SilentlyContinue
        if ($CimProcess -and $CimProcess.CommandLine) {
            $CommandLineSha256 = Get-TextSha256 ([string]$CimProcess.CommandLine)
        }
    }
    catch {
        # Standard-user or managed environments can deny WMI command-line
        # access. Creation time + executable path still prevent PID reuse.
    }
    return [ordered]@{
        pid = [int]$Process.Id
        creation_time_utc = $CreationTime.ToString("o")
        executable_path = [System.IO.Path]::GetFullPath($ExecutablePath)
        command_line_sha256 = $CommandLineSha256
    }
}

function Test-ProcessIdentity {
    param([Parameter(Mandatory = $true)]$Identity)

    if (-not $Identity.pid -or -not $Identity.creation_time_utc -or
        -not $Identity.executable_path) {
        return $false
    }
    $Current = Get-ProcessIdentity -ProcessId ([int]$Identity.pid)
    if (-not $Current) {
        return $false
    }
    $ExpectedCreation = [datetime]::Parse(
        [string]$Identity.creation_time_utc,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind
    ).ToUniversalTime()
    $CurrentCreation = [datetime]::Parse(
        [string]$Current.creation_time_utc,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind
    ).ToUniversalTime()
    return (
        [math]::Abs(($CurrentCreation - $ExpectedCreation).TotalSeconds) -lt 1 -and
        [string]::Equals(
            [string]$Current.executable_path,
            [string]$Identity.executable_path,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and (
            -not $Identity.command_line_sha256 -or
            -not $Current.command_line_sha256 -or
            [string]::Equals(
                [string]$Current.command_line_sha256,
                [string]$Identity.command_line_sha256,
                [System.StringComparison]::Ordinal
            )
        )
    )
}

function Stop-OwnedProcessTree {
    param($Identity)

    if (-not $Identity -or -not (Test-ProcessIdentity -Identity $Identity)) {
        return
    }
    try {
        & taskkill.exe /PID ([int]$Identity.pid) /T /F 2>$null | Out-Null
    }
    catch {
        # The process can exit between identity validation and termination.
    }
}

function Get-ListeningProcessId {
    param([Parameter(Mandatory = $true)][int]$Port)

    $Pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    foreach ($Line in (& netstat.exe -ano -p tcp)) {
        if ($Line -match $Pattern) {
            return [int]$Matches[1]
        }
    }
    return $null
}

function Repair-StaleRuntimeState {
    param(
        [Parameter(Mandatory = $true)][int]$CurrentBackendPort,
        [Parameter(Mandatory = $true)][int]$CurrentFrontendPort
    )

    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return
    }

    $PortsToCheck = [System.Collections.Generic.HashSet[int]]::new()
    [void]$PortsToCheck.Add($CurrentBackendPort)
    [void]$PortsToCheck.Add($CurrentFrontendPort)
    try {
        $RecordedState = Get-Content -Raw -Encoding UTF8 -LiteralPath $StatePath |
            ConvertFrom-Json
        foreach ($PropertyName in @("backend_port", "frontend_port")) {
            $Property = $RecordedState.PSObject.Properties[$PropertyName]
            if ($Property -and $null -ne $Property.Value) {
                $RecordedPort = [int]$Property.Value
                if ($RecordedPort -ge 1 -and $RecordedPort -le 65535) {
                    [void]$PortsToCheck.Add($RecordedPort)
                }
            }
        }
        if (-not $RecordedState.PSObject.Properties["state_version"]) {
            [void]$PortsToCheck.Add(8000)
            [void]$PortsToCheck.Add(3000)
        }
    }
    catch {
        Write-Warning "data\runtime.json 无法解析；仅在确认当前服务端口空闲后清理。"
    }

    foreach ($Port in $PortsToCheck) {
        if (Get-ListeningProcessId -Port $Port) {
            return
        }
    }

    Remove-Item -LiteralPath $StatePath -Force
    Write-Host "检测到已失效的运行状态；确认相关端口空闲后已自动清理。" `
        -ForegroundColor Yellow
}

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $Directory = Split-Path -Parent $Path
    $TemporaryPath = Join-Path $Directory (
        ".runtime-$([guid]::NewGuid().ToString('N')).tmp"
    )
    try {
        $Json = $Value | ConvertTo-Json -Depth 8
        $Utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($TemporaryPath, $Json, $Utf8WithoutBom)
        if (Test-Path -LiteralPath $Path) {
            [System.IO.File]::Replace($TemporaryPath, $Path, $null, $true)
        }
        else {
            [System.IO.File]::Move($TemporaryPath, $Path)
        }
    }
    finally {
        Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $BackendPython -PathType Leaf)) {
    throw "尚未安装 Python 环境，请先运行 .\scripts\setup.ps1。"
}

$RuntimeProbeScript = @'
import importlib.metadata as metadata
import json
from pathlib import Path

from app.config import settings
from app.main import API_CONTRACT_VERSION, APP_VERSION
from app.services.f2_links import F2_VENDOR_DIR, ensure_f2_runtime

ensure_f2_runtime()
import f2

vendor = F2_VENDOR_DIR.resolve()
origin = Path(f2.__file__).resolve()
if vendor not in origin.parents:
    raise SystemExit(f"F2 loaded outside isolated vendor directory: {origin}")
versions = {
    distribution.version
    for distribution in metadata.distributions(path=[str(vendor)])
    if (distribution.metadata.get("Name") or "").lower() == "f2"
}
if versions != {"0.0.1.7"}:
    raise SystemExit(f"Unexpected isolated F2 version: {sorted(versions)}")
print(json.dumps({
    "app_version": APP_VERSION,
    "api_contract": API_CONTRACT_VERSION,
    "backend_host": settings.app_host,
    "backend_port": settings.app_port,
    "f2_origin": str(origin),
}))
'@
$PreviousErrorActionPreference = $ErrorActionPreference
try {
    # Windows PowerShell 5.1 can strip quotes from a multiline native `-c`
    # argument. Passing the probe over stdin preserves the Python source.
    $ErrorActionPreference = "Continue"
    $RuntimeProbeText = @(
        $RuntimeProbeScript | & $BackendPython - 2>&1
    )
    $RuntimeProbeExitCode = $LASTEXITCODE
}
catch {
    $RuntimeProbeText = @()
    $RuntimeProbeExitCode = 1
}
finally {
    $ErrorActionPreference = $PreviousErrorActionPreference
}
$RuntimeProbeLine = [string]($RuntimeProbeText | Select-Object -Last 1)
if ($RuntimeProbeExitCode -ne 0 -or -not $RuntimeProbeLine) {
    $RuntimeProbeDiagnostic = [string](
        $RuntimeProbeText | Where-Object { [string]$_ } | Select-Object -Last 1
    )
    if (-not $RuntimeProbeDiagnostic) {
        $RuntimeProbeDiagnostic = "探针没有返回详细信息"
    }
    throw (
        "Python 依赖、F2 隔离环境或应用配置校验失败：" +
        "$RuntimeProbeDiagnostic。请重新运行 .\scripts\setup.ps1。"
    )
}
try {
    $RuntimeProbe = $RuntimeProbeLine | ConvertFrom-Json
    [int]$ExpectedApiContract = $RuntimeProbe.api_contract
    [int]$BackendPort = $RuntimeProbe.backend_port
    [string]$BackendHost = $RuntimeProbe.backend_host
    [string]$AppVersion = $RuntimeProbe.app_version
}
catch {
    throw "无法读取后端运行配置，请检查 Python 环境与 app.main。"
}
if ($BackendPort -lt 1 -or $BackendPort -gt 65535 -or $BackendPort -eq $FrontendPort) {
    throw "APP_PORT 必须为 1-65535 且不能与前端端口 $FrontendPort 相同。"
}
$BackendUrl = "http://${BackendHost}:$BackendPort"
$env:NEXT_PUBLIC_API_URL = $BackendUrl

if (-not (Test-Path -LiteralPath (Join-Path $FrontendPath "node_modules"))) {
    throw "前端依赖不存在，请先运行 .\scripts\setup.ps1。"
}
$FrontendBuildIdPath = Join-Path $FrontendPath ".next\BUILD_ID"
$FrontendBuildStatePath = Join-Path $FrontendPath ".next\tokbrain-build.json"
if (-not (Test-Path -LiteralPath $FrontendBuildIdPath -PathType Leaf)) {
    throw "前端生产构建不存在，请重新运行 .\scripts\setup.ps1。"
}
if (Test-Path -LiteralPath $FrontendBuildStatePath -PathType Leaf) {
    try {
        $FrontendBuildState = Get-Content -Raw -Encoding UTF8 `
            -LiteralPath $FrontendBuildStatePath |
            ConvertFrom-Json
    }
    catch {
        throw "前端构建信息损坏，请重新运行 .\scripts\setup.ps1。"
    }
    if ([string]$FrontendBuildState.backend_url -ne $BackendUrl) {
        throw "APP_HOST/APP_PORT 已在安装后改变；请重新运行 .\scripts\setup.ps1 以更新前端构建。"
    }
}
elseif ($BackendUrl -ne "http://127.0.0.1:8000") {
    throw "自定义 APP_HOST/APP_PORT 缺少对应的前端构建信息；请重新运行 .\scripts\setup.ps1。"
}

New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
New-Item -ItemType Directory -Force -Path $LogPath | Out-Null

Repair-StaleRuntimeState -CurrentBackendPort $BackendPort `
    -CurrentFrontendPort $FrontendPort

foreach ($RequiredPort in @($BackendPort, $FrontendPort)) {
    $ExistingProcessId = Get-ListeningProcessId -Port $RequiredPort
    if ($ExistingProcessId) {
        throw "端口 $RequiredPort 已被进程 $ExistingProcessId 占用。停止脚本只会终止身份匹配的 TokBrain 实例，不会关闭其他软件。"
    }
}

$Backend = $null
$Frontend = $null
$BackendLauncherIdentity = $null
$FrontendLauncherIdentity = $null
$BackendListenerIdentity = $null
$FrontendListenerIdentity = $null
$StartupComplete = $false

try {
    $Backend = Start-Process -FilePath $BackendPython -ArgumentList (
        "-m", "uvicorn", "app.main:app", "--host", $BackendHost,
        "--port", [string]$BackendPort
    ) -WorkingDirectory $WorkspacePath -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogPath "backend.log") `
        -RedirectStandardError (Join-Path $LogPath "backend-error.log")
    $BackendLauncherIdentity = Get-ProcessIdentity -ProcessId $Backend.Id
    if (-not $BackendLauncherIdentity) {
        throw "后端启动后无法记录进程身份。"
    }

    $NpmCommand = (Get-Command npm.cmd -ErrorAction Stop).Source
    $Frontend = Start-Process -FilePath $NpmCommand -ArgumentList (
        "run", "start"
    ) -WorkingDirectory $FrontendPath -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogPath "frontend.log") `
        -RedirectStandardError (Join-Path $LogPath "frontend-error.log")
    $FrontendLauncherIdentity = Get-ProcessIdentity -ProcessId $Frontend.Id
    if (-not $FrontendLauncherIdentity) {
        throw "前端启动后无法记录进程身份。"
    }

    $Ready = $false
    $LastBackendStatus = "未响应"
    $LastBackendContract = "未知"
    $LastCoordinatorStatus = "未响应"
    $LastFrontendStatus = "未响应"
    $LastProbeError = ""
    for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
        try {
            $BackendResponse = Invoke-RestMethod -TimeoutSec 1 "$BackendUrl/health"
            $LastBackendStatus = [string]$BackendResponse.status
            $LastBackendContract = [string]$BackendResponse.api_contract
            $CoordinatorResponse = Invoke-RestMethod -TimeoutSec 1 `
                "$BackendUrl/api/system-health/probes/coordinators"
            $LastCoordinatorStatus = [string]$CoordinatorResponse.status
            $FrontendResponse = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 $LocalUrl
            $LastFrontendStatus = [string]$FrontendResponse.StatusCode
            if ($BackendResponse.status -eq "healthy" -and
                $BackendResponse.api_contract -eq $ExpectedApiContract -and
                $CoordinatorResponse.status -eq "healthy" -and
                $FrontendResponse.StatusCode -eq 200) {
                $Ready = $true
                break
            }
        }
        catch {
            $LastProbeError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $Ready) {
        $FailureSummary = (
            "后端状态=$LastBackendStatus，API contract=$LastBackendContract" +
            "（期望 $ExpectedApiContract），后台协调器=$LastCoordinatorStatus，" +
            "前端 HTTP=$LastFrontendStatus"
        )
        if ($LastProbeError) {
            $FailureSummary += "，最近探测错误=$LastProbeError"
        }
        throw "服务未能在 20 秒内就绪；$FailureSummary。请查看 data\logs\backend-error.log 与 data\logs\frontend-error.log。"
    }

    $BackendListenerProcessId = Get-ListeningProcessId -Port $BackendPort
    $FrontendListenerProcessId = Get-ListeningProcessId -Port $FrontendPort
    if (-not $BackendListenerProcessId -or -not $FrontendListenerProcessId) {
        throw "服务已响应，但无法记录监听进程身份。"
    }
    $BackendListenerIdentity = Get-ProcessIdentity -ProcessId $BackendListenerProcessId
    $FrontendListenerIdentity = Get-ProcessIdentity -ProcessId $FrontendListenerProcessId
    if (-not $BackendListenerIdentity -or -not $FrontendListenerIdentity) {
        throw "服务已响应，但监听进程身份不完整。"
    }

    $InstanceId = [guid]::NewGuid().ToString("D")
    $RuntimeState = [ordered]@{
        state_version = 2
        instance_id = $InstanceId
        workspace_path = [System.IO.Path]::GetFullPath($WorkspacePath)
        created_at_utc = [datetime]::UtcNow.ToString("o")
        app_version = $AppVersion
        api_contract = $ExpectedApiContract
        coordinator_status = $LastCoordinatorStatus
        backend_host = $BackendHost
        backend_port = $BackendPort
        frontend_host = $FrontendHost
        frontend_port = $FrontendPort
        backend_listener = $BackendListenerIdentity
        frontend_listener = $FrontendListenerIdentity
        backend_launcher = $BackendLauncherIdentity
        frontend_launcher = $FrontendLauncherIdentity
    }
    Write-AtomicJson -Path $StatePath -Value $RuntimeState
    $StartupComplete = $true
}
finally {
    if (-not $StartupComplete) {
        Stop-OwnedProcessTree -Identity $FrontendListenerIdentity
        Stop-OwnedProcessTree -Identity $FrontendLauncherIdentity
        Stop-OwnedProcessTree -Identity $BackendListenerIdentity
        Stop-OwnedProcessTree -Identity $BackendLauncherIdentity
    }
}

Write-Host "TokBrain $AppVersion 已启动：$LocalUrl。关闭服务请双击‘停止.cmd’。" -ForegroundColor Green
if ($env:TOKBRAIN_SKIP_LOCAL_UI_OPEN -ne "1") {
    try {
        Start-Process -FilePath $LocalUrl
    }
    catch {
        Write-Warning "本地网页未能自动打开，请手动访问 $LocalUrl。"
    }
}
