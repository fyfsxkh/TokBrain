$ErrorActionPreference = "Stop"

function Test-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    # Windows PowerShell 5.1 turns redirected native stderr into an ErrorRecord.
    # A missing py.exe runtime is an expected probe failure, not a script error.
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $FilePath @Arguments 2>$null
        $ExitCode = $LASTEXITCODE
        return ($ExitCode -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

function Get-NativeCommandOutput {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $Output = @(& $FilePath @Arguments 2>$null)
        $ExitCode = $LASTEXITCODE
        if ($ExitCode -ne 0) {
            return $null
        }
        return (($Output -join "`n").Trim())
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
}

function Stop-Setup {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    Write-Host ""
    Write-Host "安装未完成 / Setup did not complete." -ForegroundColor Red
    Write-Host $Message -ForegroundColor Yellow
    Write-Host ""
    Write-Host "完成上述操作后，请再次双击 安装.cmd。" -ForegroundColor Cyan
    exit 1
}

$WorkspacePath = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $WorkspacePath ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$F2VendorPath = Join-Path $WorkspacePath ".vendor"

$PythonExecutable = $null
$PythonPrefixArguments = @()
$UseExistingVenv = $false
if (Test-Path -LiteralPath $VenvPython) {
    $UseExistingVenv = Test-NativeCommand -FilePath $VenvPython -Arguments @(
        "-c",
        "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
    )
}

if (-not $UseExistingVenv) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        foreach ($RequestedVersion in @("3.12")) {
            $HasRequestedVersion = Test-NativeCommand -FilePath $PyLauncher.Source -Arguments @(
                "-$RequestedVersion",
                "-c",
                "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
            )
            if ($HasRequestedVersion) {
                $PythonExecutable = $PyLauncher.Source
                $PythonPrefixArguments = @("-$RequestedVersion")
                break
            }
        }
    }

    if (-not $PythonExecutable) {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($PythonCommand) {
            $HasRequestedVersion = Test-NativeCommand -FilePath $PythonCommand.Source -Arguments @(
                "-c",
                "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
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
$NodeVersionText = Get-NativeCommandOutput -FilePath $NodeCommand.Source -Arguments @("--version")
$NodeVersionMatch = [regex]::Match([string]$NodeVersionText, '^v?(\d+)\.')
if (-not $NodeVersionMatch.Success) {
    Stop-Setup "无法读取 Node.js 版本。请重新安装 Node.js 22 或更高版本。"
}
$NodeMajorVersion = [int]$NodeVersionMatch.Groups[1].Value
if ($NodeMajorVersion -lt 22) {
    Stop-Setup "检测到 Node.js $NodeVersionText，但本项目需要 Node.js 22 或更高版本。"
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
    } catch {
        continue
    }
}
if (-not $NpmCachePath) {
    Stop-Setup "无法创建可写的 npm 缓存目录。请确认当前 Windows 用户可写入项目目录和 LocalAppData。"
}

if (-not $UseExistingVenv) {
    & $PythonExecutable @PythonPrefixArguments -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        Stop-Setup "创建 Python 虚拟环境失败。"
    }
}
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Stop-Setup "升级 pip 失败，请检查网络连接后重试。"
}
& $VenvPython -m pip install -r (Join-Path $WorkspacePath "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Stop-Setup "安装 Python 主依赖失败，请检查网络连接后重试。"
}
# F2 0.0.1.7 incorrectly pins obsolete vulnerable runtime packages and test
# tools. Keep only its source package in an isolated directory while the
# audited compatible runtime versions come from requirements.txt.
& $VenvPython -m pip install --upgrade --no-deps --target $F2VendorPath `
    -r (Join-Path $WorkspacePath "requirements-f2.txt")
if ($LASTEXITCODE -ne 0) {
    Stop-Setup "安装隔离的 F2 运行时失败，请检查网络连接后重试。"
}
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
} finally {
    Pop-Location
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "未检测到 ffmpeg。安装后加入 PATH；否则视频无法进入完整处理流水线。"
}
Write-Host "安装完成。运行 .\start.ps1 或双击 start.cmd。" -ForegroundColor Green
