$ErrorActionPreference = "Stop"

$WorkspacePath = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$StatePath = Join-Path $WorkspacePath "data\runtime.json"

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
        # WMI command-line access can be unavailable to a standard user.
    }
    return [ordered]@{
        pid = [int]$Process.Id
        creation_time_utc = $CreationTime.ToString("o")
        executable_path = [System.IO.Path]::GetFullPath($ExecutablePath)
        command_line_sha256 = $CommandLineSha256
    }
}

function Test-ProcessIdentity {
    param(
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)]$Current
    )

    try {
        if (-not $Expected.pid -or -not $Expected.creation_time_utc -or
            -not $Expected.executable_path) {
            return $false
        }
        $ExpectedCreation = [datetime]::Parse(
            [string]$Expected.creation_time_utc,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
        $CurrentCreation = [datetime]::Parse(
            [string]$Current.creation_time_utc,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
        return (
            [int]$Expected.pid -eq [int]$Current.pid -and
            [math]::Abs(($CurrentCreation - $ExpectedCreation).TotalSeconds) -lt 1 -and
            [string]::Equals(
                [string]$Expected.executable_path,
                [string]$Current.executable_path,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -and (
                -not $Expected.command_line_sha256 -or
                -not $Current.command_line_sha256 -or
                [string]::Equals(
                    [string]$Expected.command_line_sha256,
                    [string]$Current.command_line_sha256,
                    [System.StringComparison]::Ordinal
                )
            )
        )
    }
    catch {
        return $false
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

function Stop-RecordedProcess {
    param(
        [Parameter(Mandatory = $true)]$Identity,
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)]$SeenProcessIds
    )

    if (-not $Identity.pid) {
        throw "运行状态中的 $Role 进程身份不完整；为避免误停其他软件，未执行终止操作。"
    }
    $ProcessId = [int]$Identity.pid
    if ($SeenProcessIds.Contains($ProcessId)) {
        return
    }
    [void]$SeenProcessIds.Add($ProcessId)

    $Current = Get-ProcessIdentity -ProcessId $ProcessId
    if (-not $Current) {
        return
    }
    if (-not (Test-ProcessIdentity -Expected $Identity -Current $Current)) {
        Write-Warning "$Role 记录的 PID $ProcessId 已属于另一个进程；已跳过，未终止该进程。"
        return
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Get-RuntimeInteger {
    param(
        [Parameter(Mandatory = $true)]$RuntimeState,
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$DefaultValue = 0
    )

    $Property = $RuntimeState.PSObject.Properties[$Name]
    if (-not $Property -or $null -eq $Property.Value) {
        return $DefaultValue
    }
    try {
        return [int]$Property.Value
    }
    catch {
        return $DefaultValue
    }
}

function Test-LegacyBackendListener {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$ExpectedApiContract
    )

    try {
        $Response = Invoke-RestMethod -TimeoutSec 2 "http://127.0.0.1:$Port/health"
        return (
            [string]$Response.status -eq "healthy" -and
            [int]$Response.api_contract -eq $ExpectedApiContract
        )
    }
    catch {
        return $false
    }
}

function Test-LegacyFrontendListener {
    param([Parameter(Mandatory = $true)][int]$Port)

    try {
        $Response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 `
            "http://127.0.0.1:$Port/"
        return (
            [int]$Response.StatusCode -eq 200 -and
            [string]$Response.Content -match "TokBrain"
        )
    }
    catch {
        return $false
    }
}

function Stop-LegacyRuntime {
    param([Parameter(Mandatory = $true)]$RuntimeState)

    $BackendProcessId = Get-RuntimeInteger -RuntimeState $RuntimeState `
        -Name "backend_pid"
    $FrontendProcessId = Get-RuntimeInteger -RuntimeState $RuntimeState `
        -Name "frontend_pid"
    $BackendPort = Get-RuntimeInteger -RuntimeState $RuntimeState `
        -Name "backend_port" -DefaultValue 8000
    $FrontendPort = Get-RuntimeInteger -RuntimeState $RuntimeState `
        -Name "frontend_port" -DefaultValue 3000
    $ExpectedApiContract = Get-RuntimeInteger -RuntimeState $RuntimeState `
        -Name "api_contract"

    if ($BackendPort -lt 1 -or $BackendPort -gt 65535 -or
        $FrontendPort -lt 1 -or $FrontendPort -gt 65535) {
        throw "旧运行状态中的端口无效；为避免误停其他软件，未执行终止操作。"
    }

    $Problems = [System.Collections.Generic.List[string]]::new()
    $ValidatedProcessIds = [System.Collections.Generic.List[int]]::new()
    $ListenerSpecs = @(
        [pscustomobject]@{
            role = "后端"
            port = $BackendPort
            recorded_pid = $BackendProcessId
            process_pattern = "python*"
        },
        [pscustomobject]@{
            role = "前端"
            port = $FrontendPort
            recorded_pid = $FrontendProcessId
            process_pattern = "node*"
        }
    )

    foreach ($Spec in $ListenerSpecs) {
        $ListeningProcessId = Get-ListeningProcessId -Port $Spec.port
        if (-not $ListeningProcessId) {
            continue
        }
        if (-not $Spec.recorded_pid -or
            $ListeningProcessId -ne [int]$Spec.recorded_pid) {
            $Problems.Add(
                "$($Spec.role)端口 $($Spec.port) 由未记录的进程 $ListeningProcessId 占用"
            )
            continue
        }
        $Process = Get-Process -Id $ListeningProcessId -ErrorAction SilentlyContinue
        if (-not $Process -or $Process.ProcessName -notlike $Spec.process_pattern) {
            $Problems.Add(
                "$($Spec.role) PID $ListeningProcessId 的程序类型与旧记录不符"
            )
            continue
        }
        $ProbePassed = if ($Spec.role -eq "后端") {
            $ExpectedApiContract -gt 0 -and (Test-LegacyBackendListener `
                -Port $Spec.port -ExpectedApiContract $ExpectedApiContract)
        }
        else {
            Test-LegacyFrontendListener -Port $Spec.port
        }
        if (-not $ProbePassed) {
            $Problems.Add(
                "$($Spec.role) PID $ListeningProcessId 未通过 TokBrain 服务特征校验"
            )
            continue
        }
        $ValidatedProcessIds.Add($ListeningProcessId)
    }

    if ($Problems.Count) {
        throw (
            "旧运行状态无法安全验证：$($Problems -join '；')。" +
            "未终止任何进程，运行状态已保留。"
        )
    }

    foreach ($ProcessId in $ValidatedProcessIds) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }

    for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
        $StillRunning = @(
            $ValidatedProcessIds | Where-Object {
                Get-Process -Id $_ -ErrorAction SilentlyContinue
            }
        )
        if (-not $StillRunning.Count) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    $Remaining = @(
        $ValidatedProcessIds | Where-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue
        }
    )
    if ($Remaining.Count) {
        throw "旧 TokBrain 进程未能停止：PID $($Remaining -join '、')；运行状态已保留。"
    }

    Remove-Item -LiteralPath $StatePath -Force
    if ($ValidatedProcessIds.Count) {
        Write-Host "已验证并停止旧启动器留下的 TokBrain 实例；运行状态已升级清理。"
    }
    else {
        Write-Host "旧运行状态已失效，未发现仍在监听的 TokBrain 进程；已自动清理。"
    }
}

if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    Write-Host "没有找到本项目的运行状态；未停止任何进程。"
    exit 0
}

try {
    $RuntimeState = Get-Content -Raw -Encoding UTF8 -LiteralPath $StatePath |
        ConvertFrom-Json
}
catch {
    throw "data\runtime.json 无法解析；为避免误停其他软件，未执行终止操作。"
}

$StateVersion = Get-RuntimeInteger -RuntimeState $RuntimeState `
    -Name "state_version"
if ($StateVersion -ne 2) {
    Stop-LegacyRuntime -RuntimeState $RuntimeState
    exit 0
}
try {
    [void][guid]::Parse([string]$RuntimeState.instance_id)
    $RecordedWorkspace = [System.IO.Path]::GetFullPath(
        [string]$RuntimeState.workspace_path
    )
}
catch {
    throw "运行状态缺少有效实例标识；为避免误停其他软件，未执行终止操作。"
}
if (-not [string]::Equals(
    $RecordedWorkspace,
    $WorkspacePath,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "运行状态属于其他项目目录；未停止任何进程。"
}

$BackendPort = [int]$RuntimeState.backend_port
$FrontendPort = [int]$RuntimeState.frontend_port
if ($BackendPort -lt 1 -or $BackendPort -gt 65535 -or
    $FrontendPort -lt 1 -or $FrontendPort -gt 65535) {
    throw "运行状态中的端口无效；为避免误停其他软件，未执行终止操作。"
}

# Listener identities are recorded after readiness. Launchers are recorded at
# creation time. Every PID is matched on creation time and executable path;
# when Windows permits command-line inspection, its SHA-256 must match too.
$SeenProcessIds = [System.Collections.Generic.HashSet[int]]::new()
Stop-RecordedProcess -Identity $RuntimeState.frontend_listener `
    -Role "前端监听" -SeenProcessIds $SeenProcessIds
Stop-RecordedProcess -Identity $RuntimeState.backend_listener `
    -Role "后端监听" -SeenProcessIds $SeenProcessIds
Stop-RecordedProcess -Identity $RuntimeState.frontend_launcher `
    -Role "前端启动器" -SeenProcessIds $SeenProcessIds
Stop-RecordedProcess -Identity $RuntimeState.backend_launcher `
    -Role "后端启动器" -SeenProcessIds $SeenProcessIds

Start-Sleep -Milliseconds 400

$UnsafeListeners = @()
foreach ($ListenerSpec in @(
    [pscustomobject]@{
        role = "后端"
        port = $BackendPort
        identity = $RuntimeState.backend_listener
    },
    [pscustomobject]@{
        role = "前端"
        port = $FrontendPort
        identity = $RuntimeState.frontend_listener
    }
)) {
    $ListeningProcessId = Get-ListeningProcessId -Port $ListenerSpec.port
    if (-not $ListeningProcessId) {
        continue
    }
    if ($ListeningProcessId -ne [int]$ListenerSpec.identity.pid) {
        Write-Warning (
            "端口 $($ListenerSpec.port) 当前由未记录的进程 $ListeningProcessId 占用；" +
            "它不是已验证的 TokBrain 实例，未予终止。"
        )
        continue
    }
    $Current = Get-ProcessIdentity -ProcessId $ListeningProcessId
    if ($Current -and (Test-ProcessIdentity `
        -Expected $ListenerSpec.identity -Current $Current)) {
        $UnsafeListeners += "$($ListenerSpec.role) PID $ListeningProcessId"
    }
    else {
        $UnsafeListeners += "$($ListenerSpec.role) PID $ListeningProcessId（身份不匹配）"
    }
}

if ($UnsafeListeners.Count) {
    throw "未能安全停止记录的 TokBrain 监听进程：$($UnsafeListeners -join '，')。运行状态已保留，请检查进程权限。"
}

Remove-Item -LiteralPath $StatePath -Force
Write-Host "已停止身份匹配的 TokBrain 实例；未触碰任何未记录或身份不匹配的进程。"
