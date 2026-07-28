$ErrorActionPreference = "Stop"
$WorkspacePath = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $WorkspacePath ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$F2VendorPath = Join-Path $WorkspacePath ".vendor"

$PythonExecutable = $null
$PythonPrefixArguments = @()
$UseExistingVenv = $false
if (Test-Path -LiteralPath $VenvPython) {
    & $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
    $UseExistingVenv = $LASTEXITCODE -eq 0
}

if (-not $UseExistingVenv) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        foreach ($RequestedVersion in @("3.12")) {
            & $PyLauncher.Source "-$RequestedVersion" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $PythonExecutable = $PyLauncher.Source
                $PythonPrefixArguments = @("-$RequestedVersion")
                break
            }
        }
    }

    if (-not $PythonExecutable) {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($PythonCommand) {
            & $PythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $PythonExecutable = $PythonCommand.Source
            }
        }
    }

    if (-not $PythonExecutable) {
        throw "未找到可用的 Python 3.12。请从 python.org 安装，并勾选 Add Python to PATH。"
    }

    & $PythonExecutable @PythonPrefixArguments -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Python 虚拟环境失败。"
    }
}
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $WorkspacePath "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "安装 Python 主依赖失败。"
}
# F2 0.0.1.7 incorrectly pins obsolete vulnerable runtime packages and test
# tools. Keep only its source package in an isolated directory while the
# audited compatible runtime versions come from requirements.txt.
& $VenvPython -m pip install --upgrade --no-deps --target $F2VendorPath `
    -r (Join-Path $WorkspacePath "requirements-f2.txt")
if ($LASTEXITCODE -ne 0) {
    throw "安装隔离的 F2 运行时失败。"
}

$NodeCommand = Get-Command node -ErrorAction SilentlyContinue
$NpmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $NodeCommand -or -not $NpmCommand) {
    throw "未找到 Node.js/npm。请安装 Node.js 22 或更高版本。"
}
$NodeMajorVersion = [int]((& $NodeCommand.Source --version).TrimStart("v").Split(".")[0])
if ($NodeMajorVersion -lt 22) {
    throw "需要 Node.js 22 或更高版本。"
}
Push-Location (Join-Path $WorkspacePath "frontend")
try {
    & $NpmCommand.Source ci
} finally {
    Pop-Location
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "未检测到 ffmpeg。安装后加入 PATH；否则视频无法进入完整处理流水线。"
}
Write-Host "安装完成。运行 .\start.ps1 或双击 start.cmd。" -ForegroundColor Green
