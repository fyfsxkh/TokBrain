param(
    [switch]$WithDev,
    [switch]$WithoutBilling
)

$ErrorActionPreference = "Stop"

function Test-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    # Windows PowerShell 5.1 turns redirected native stderr into an ErrorRecord.
    # A missing py.exe runtime is an expected probe failure, not a script error.
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $FilePath @Arguments 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

function Get-NativeCommandOutput {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $Output = @(& $FilePath @Arguments 2>$null)
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return (($Output -join "`n").Trim())
    }
    catch {
        return $null
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

function Stop-Setup {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "安装未完成 / Setup did not complete." -ForegroundColor Red
    Write-Host $Message -ForegroundColor Yellow
    Write-Host ""
    Write-Host "完成上述操作后，请再次双击 安装.cmd。" -ForegroundColor Cyan
    exit 1
}

function Invoke-NativeStep {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        Stop-Setup $FailureMessage
    }
}

function Invoke-PythonStdinStep {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$Script,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    # Windows PowerShell 5.1 can strip quotes from multiline native `-c`
    # arguments. Passing source through stdin preserves it exactly.
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $Script | & $FilePath - @Arguments
        $ExitCode = $LASTEXITCODE
    }
    catch {
        $ExitCode = 1
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($ExitCode -ne 0) {
        Stop-Setup $FailureMessage
    }
}

function Remove-GeneratedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$WorkspacePath
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $ResolvedWorkspace = [System.IO.Path]::GetFullPath($WorkspacePath).TrimEnd('\')
    $ResolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $ResolvedPath.StartsWith(
        "$ResolvedWorkspace\.venv-",
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to remove unexpected generated directory: $ResolvedPath"
    }
    Remove-Item -LiteralPath $ResolvedPath -Recurse -Force
}

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $Directory = Split-Path -Parent $Path
    $TemporaryPath = Join-Path $Directory (
        ".tokbrain-build-$([guid]::NewGuid().ToString('N')).tmp"
    )
    try {
        $Json = $Value | ConvertTo-Json -Depth 5
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

$WorkspacePath = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
Set-Location $WorkspacePath
$VenvPath = Join-Path $WorkspacePath ".venv"
$F2VendorPath = Join-Path $WorkspacePath ".vendor"
$RuntimeStatePath = Join-Path $WorkspacePath "data\runtime.json"
$InstallId = [guid]::NewGuid().ToString("N")
$StagingRoot = Join-Path $WorkspacePath ".venv-installing-$InstallId"
$NewVenvPath = Join-Path $StagingRoot "venv"
$NewVendorPath = Join-Path $StagingRoot "vendor"
$NewVenvPython = Join-Path $NewVenvPath "Scripts\python.exe"
$OldVenvPath = Join-Path $WorkspacePath ".venv-replaced-$InstallId"
$OldVendorPath = Join-Path $WorkspacePath ".venv-vendor-replaced-$InstallId"

if (Test-Path -LiteralPath $RuntimeStatePath) {
    Stop-Setup "检测到正在运行或尚未安全停止的 TokBrain。请先双击 停止.cmd，确认服务停止后再安装。"
}

$PythonExecutable = $null
$PythonPrefixArguments = @()
$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($PyLauncher) {
    $HasRequestedVersion = Test-NativeCommand -FilePath $PyLauncher.Source -Arguments @(
        "-3.12", "-c",
        "import platform, sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and platform.architecture()[0] == '64bit' else 1)"
    )
    if ($HasRequestedVersion) {
        $PythonExecutable = $PyLauncher.Source
        $PythonPrefixArguments = @("-3.12")
    }
}

if (-not $PythonExecutable) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $HasRequestedVersion = Test-NativeCommand -FilePath $PythonCommand.Source -Arguments @(
            "-c",
            "import platform, sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and platform.architecture()[0] == '64bit' else 1)"
        )
        if ($HasRequestedVersion) {
            $PythonExecutable = $PythonCommand.Source
        }
    }
}

if (-not $PythonExecutable) {
    Stop-Setup @"
未找到可用的 64 位 Python 3.12（其他版本不能替代）。
1. 打开 https://www.python.org/downloads/windows/
2. 下载并安装 Python 3.12.x (64-bit)。
3. 安装时勾选 "Add python.exe to PATH"。
4. 安装结束后关闭本窗口，再次双击 安装.cmd。

如果电脑已有 winget，也可在 PowerShell 中运行：
winget install --exact --id Python.Python.3.12
"@
}

$NodeCommand = Get-Command node -ErrorAction SilentlyContinue
$NpmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $NodeCommand -or -not $NpmCommand) {
    Stop-Setup @"
未找到 Node.js/npm。请先安装 Node.js 22 或更高版本：
https://nodejs.org/en/download

如果电脑已有 winget，也可在 PowerShell 中运行：
winget install --exact --id OpenJS.NodeJS.LTS
"@
}
$NodeVersionText = Get-NativeCommandOutput -FilePath $NodeCommand.Source -Arguments @(
    "--version"
)
$NodeVersionMatch = [regex]::Match([string]$NodeVersionText, '^v?(\d+)\.')
if (-not $NodeVersionMatch.Success) {
    Stop-Setup "无法读取 Node.js 版本。请重新安装 Node.js 22 或更高版本。"
}
$NodeMajorVersion = [int]$NodeVersionMatch.Groups[1].Value
if ($NodeMajorVersion -lt 22) {
    Stop-Setup "检测到 Node.js $NodeVersionText，但本项目需要 Node.js 22 或更高版本。"
}
$NpmVersionText = Get-NativeCommandOutput -FilePath $NpmCommand.Source -Arguments @(
    "--version"
)
$NpmVersionMatch = [regex]::Match([string]$NpmVersionText, '^(\d+)\.')
if (-not $NpmVersionMatch.Success -or
    [int]$NpmVersionMatch.Groups[1].Value -lt 10) {
    Stop-Setup "检测到 npm $NpmVersionText，但本项目需要 npm 10 或更高版本。请更新 Node.js LTS。"
}

$NpmCacheCandidates = @()
if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $NpmCacheCandidates += Join-Path $env:LOCALAPPDATA "TokBrain\npm-cache"
}
$NpmCacheCandidates += Join-Path $WorkspacePath "data\cache\npm"
$NpmCachePath = $null
foreach ($CacheCandidate in $NpmCacheCandidates) {
    try {
        [void][System.IO.Directory]::CreateDirectory($CacheCandidate)
        $NpmCachePath = $CacheCandidate
        break
    }
    catch {
        continue
    }
}
if (-not $NpmCachePath) {
    Stop-Setup "无法创建可写的 npm 缓存目录。请确认当前 Windows 用户可写入项目目录和 LocalAppData。"
}

$SwapCompleted = $false
$MovedOldVenv = $false
$MovedOldVendor = $false
$MovedNewVenv = $false
$MovedNewVendor = $false
try {
    New-Item -ItemType Directory -Path $StagingRoot | Out-Null
    Invoke-NativeStep -FilePath $PythonExecutable -Arguments @(
        $PythonPrefixArguments + @("-m", "venv", $NewVenvPath)
    ) -FailureMessage "创建干净的 Python 3.12 虚拟环境失败。"

    # Keep the bootstrap tool deterministic instead of installing whatever pip
    # happens to be newest when setup runs. Equivalent command:
    # python -m pip install --upgrade pip==25.2
    Invoke-NativeStep -FilePath $NewVenvPython -Arguments @(
        "-m", "pip", "install", "--upgrade", "pip==25.2"
    ) -FailureMessage "安装固定版本 pip 失败，请检查网络连接后重试。"
    Invoke-NativeStep -FilePath $NewVenvPython -Arguments @(
        "-m", "pip", "install", "-r", (Join-Path $WorkspacePath "requirements.txt")
    ) -FailureMessage "安装 Python 主依赖失败，请检查网络连接后重试。"

    if (-not $WithoutBilling) {
        Invoke-NativeStep -FilePath $NewVenvPython -Arguments @(
            "-m", "pip", "install", "-r",
            (Join-Path $WorkspacePath "requirements-billing.txt")
        ) -FailureMessage "安装可选账单核对依赖失败；如不使用该功能，可改用 .\scripts\setup.ps1 -WithoutBilling。"
    }
    if ($WithDev) {
        Invoke-NativeStep -FilePath $NewVenvPython -Arguments @(
            "-m", "pip", "install", "-r",
            (Join-Path $WorkspacePath "requirements-dev.txt")
        ) -FailureMessage "安装测试与审计依赖失败。"
    }

    # F2 0.0.1.7 incorrectly pins obsolete vulnerable runtime packages and test
    # tools. Keep only its source package in an isolated directory while the
    # audited compatible runtime versions come from requirements.txt.
    Invoke-NativeStep -FilePath $NewVenvPython -Arguments @(
        "-m", "pip", "install", "--upgrade", "--no-deps", "--target",
        $NewVendorPath, "-r", (Join-Path $WorkspacePath "requirements-f2.txt")
    ) -FailureMessage "安装隔离的 F2 运行时失败，请检查网络连接后重试。"

    Invoke-NativeStep -FilePath $NewVenvPython -Arguments @(
        "-m", "pip", "check"
    ) -FailureMessage "Python 依赖存在版本冲突；旧环境尚未替换，请检查安装输出。"
    $F2ValidationScript = @'
import importlib.metadata as metadata
import sys
from pathlib import Path

vendor = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(vendor))
import f2
import fastapi
import multipart
import sqlalchemy

origin = Path(f2.__file__).resolve()
if vendor not in origin.parents:
    raise SystemExit(f"F2 loaded outside staging vendor directory: {origin}")
versions = {
    distribution.version
    for distribution in metadata.distributions(path=[str(vendor)])
    if (distribution.metadata.get("Name") or "").lower() == "f2"
}
if versions != {"0.0.1.7"}:
    raise SystemExit(f"Unexpected isolated F2 version: {sorted(versions)}")
'@
    Invoke-PythonStdinStep -FilePath $NewVenvPython `
        -Script $F2ValidationScript -Arguments @($NewVendorPath) `
        -FailureMessage "新 Python 环境或隔离 F2 来源校验失败；旧环境尚未替换。"

    $AppConfigLine = Get-NativeCommandOutput -FilePath $NewVenvPython -Arguments @(
        "-c",
        "import json; from app.config import settings; print(json.dumps({'host': settings.app_host, 'port': settings.app_port}))"
    )
    if (-not $AppConfigLine) {
        Stop-Setup "无法读取 APP_HOST/APP_PORT；旧 Python 环境尚未替换。"
    }
    try {
        $AppConfig = $AppConfigLine | ConvertFrom-Json
        $BackendUrl = "http://$($AppConfig.host):$([int]$AppConfig.port)"
    }
    catch {
        Stop-Setup "APP_HOST/APP_PORT 格式无效；旧 Python 环境尚未替换。"
    }

    $PreviousApiUrl = $env:NEXT_PUBLIC_API_URL
    $env:NEXT_PUBLIC_API_URL = $BackendUrl
    Push-Location (Join-Path $WorkspacePath "frontend")
    try {
        # Override any inaccessible machine/user cache setting (for example a
        # stale D:\node_cache) without modifying the user's global npm config.
        & $NpmCommand.Source ci --cache $NpmCachePath
        if ($LASTEXITCODE -ne 0) {
            Stop-Setup @"
安装前端依赖失败。
本次使用的 npm 缓存目录：$NpmCachePath
请先关闭其他 Node.js/npm 进程并重试；如果仍出现 EPERM，请检查安全软件是否拦截该目录。
"@
        }
        Invoke-NativeStep -FilePath $NpmCommand.Source -Arguments @(
            "run", "build"
        ) -FailureMessage "前端生产构建失败；旧 Python 环境尚未替换。"
        $BuildState = [ordered]@{
            state_version = 1
            backend_url = $BackendUrl
            built_at_utc = [datetime]::UtcNow.ToString("o")
            node_version = $NodeVersionText
        }
        Write-AtomicJson -Path (
            Join-Path $WorkspacePath "frontend\.next\tokbrain-build.json"
        ) -Value $BuildState
    }
    finally {
        Pop-Location
        if ($null -eq $PreviousApiUrl) {
            Remove-Item Env:\NEXT_PUBLIC_API_URL -ErrorAction SilentlyContinue
        }
        else {
            $env:NEXT_PUBLIC_API_URL = $PreviousApiUrl
        }
    }

    if (Test-Path -LiteralPath $VenvPath) {
        Move-Item -LiteralPath $VenvPath -Destination $OldVenvPath
        $MovedOldVenv = $true
    }
    if (Test-Path -LiteralPath $F2VendorPath) {
        Move-Item -LiteralPath $F2VendorPath -Destination $OldVendorPath
        $MovedOldVendor = $true
    }
    Move-Item -LiteralPath $NewVenvPath -Destination $VenvPath
    $MovedNewVenv = $true
    Move-Item -LiteralPath $NewVendorPath -Destination $F2VendorPath
    $MovedNewVendor = $true

    $VenvPython = Join-Path $VenvPath "Scripts\python.exe"
    Invoke-NativeStep -FilePath $VenvPython -Arguments @(
        "-m", "pip", "check"
    ) -FailureMessage "切换后的 Python 环境校验失败，安装程序将保留旧环境用于回滚。"
    Invoke-NativeStep -FilePath $VenvPython -Arguments @(
        "-c", @"
from pathlib import Path
from app.services.f2_links import F2_VENDOR_DIR, ensure_f2_runtime

ensure_f2_runtime()
import f2
origin = Path(f2.__file__).resolve()
vendor = F2_VENDOR_DIR.resolve()
raise SystemExit(0 if vendor in origin.parents else 1)
"@
    ) -FailureMessage "切换后的 F2 来源不在 .vendor；安装程序将保留旧环境用于回滚。"

    $SwapCompleted = $true
}
finally {
    if (-not $SwapCompleted) {
        # Roll back only directories created or moved by this install ID. User
        # data, caches, backups, and unrelated environments are never touched.
        if ($MovedNewVendor -and (Test-Path -LiteralPath $F2VendorPath)) {
            Remove-Item -LiteralPath $F2VendorPath -Recurse -Force
        }
        if ($MovedOldVendor -and (Test-Path -LiteralPath $OldVendorPath)) {
            Move-Item -LiteralPath $OldVendorPath -Destination $F2VendorPath
        }
        if ($MovedNewVenv -and (Test-Path -LiteralPath $VenvPath)) {
            Remove-Item -LiteralPath $VenvPath -Recurse -Force
        }
        if ($MovedOldVenv -and (Test-Path -LiteralPath $OldVenvPath)) {
            Move-Item -LiteralPath $OldVenvPath -Destination $VenvPath
        }
    }
    Remove-GeneratedDirectory -Path $StagingRoot -WorkspacePath $WorkspacePath
}

foreach ($ReplacedPath in @($OldVenvPath, $OldVendorPath)) {
    try {
        Remove-GeneratedDirectory -Path $ReplacedPath -WorkspacePath $WorkspacePath
    }
    catch {
        Write-Warning "新环境已启用，但旧的生成环境未能清理：$ReplacedPath"
    }
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
    -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Warning "未同时检测到 ffmpeg 与 ffprobe。安装后加入 PATH；否则视频无法进入完整处理流水线。"
}
Write-Host "安装完成并已生成生产前端。运行 .\start.ps1 或双击 start.cmd。" -ForegroundColor Green
