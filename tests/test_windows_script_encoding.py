from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_powershell_scripts_use_utf8_bom():
    scripts = (
        ROOT / "start.ps1",
        ROOT / "scripts" / "setup.ps1",
        ROOT / "scripts" / "stop.ps1",
        ROOT / "scripts" / "prepublish_check.ps1",
    )
    for script in scripts:
        assert script.read_bytes().startswith(b"\xef\xbb\xbf"), script


def test_setup_launcher_uses_process_scoped_execution_policy_bypass():
    launcher = (ROOT / "setup.cmd").read_text(encoding="utf-8").lower()
    localized_launcher = (ROOT / "安装.cmd").read_text(encoding="utf-8").lower()
    assert "powershell.exe" in launcher
    assert "-executionpolicy bypass" in launcher
    assert r"scripts\setup.ps1" in launcher
    assert 'call "%~dp0setup.cmd"' in localized_launcher


def test_setup_handles_missing_python_runtime_as_a_probe_failure():
    setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    assert "function Test-NativeCommand" in setup
    assert "function Invoke-PythonStdinStep" in setup
    assert "$Script | & $FilePath - @Arguments" in setup
    assert '$ErrorActionPreference = "Continue"' in setup
    assert "No suitable Python runtime found" not in setup
    assert "Python.Python.3.12" in setup
    assert "完成上述操作后，请再次双击 安装.cmd" in setup


def test_setup_checks_node_before_installing_project_dependencies():
    setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    node_check = setup.index("$NodeCommand = Get-Command node")
    pip_install = setup.index("-m pip install --upgrade pip")
    assert node_check < pip_install


def test_setup_overrides_inaccessible_global_npm_cache():
    setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    assert '$env:LOCALAPPDATA' in setup
    assert '"TokBrain\\npm-cache"' in setup
    assert "ci --cache $NpmCachePath" in setup
    assert "without modifying the user's global npm config" in setup
    assert "如果仍出现 EPERM" in setup


def test_startup_reads_the_api_contract_from_the_backend_instead_of_hardcoding_it():
    startup = (ROOT / "start.ps1").read_text(encoding="utf-8-sig")
    assert "from app.main import API_CONTRACT_VERSION" in startup
    assert "$ExpectedApiContract" in startup
    assert "$RuntimeProbeScript | & $BackendPython -" in startup
    assert "$RuntimeProbeDiagnostic" in startup
    assert "$RuntimeProbeScript | & $BackendPython - 2>$null" not in startup
    assert "api_contract -eq 4" not in startup
    assert "api_contract = 4" not in startup


def test_start_stop_and_restart_share_runtime_state_recovery():
    startup = (ROOT / "start.ps1").read_text(encoding="utf-8-sig")
    stop = (ROOT / "scripts" / "stop.ps1").read_text(encoding="utf-8-sig")
    restart = (ROOT / "重启.cmd").read_text(encoding="utf-8").lower()

    assert "function Repair-StaleRuntimeState" in startup
    assert "Repair-StaleRuntimeState -CurrentBackendPort" in startup
    assert "function Stop-LegacyRuntime" in stop
    assert "Test-LegacyBackendListener" in stop
    assert "Test-LegacyFrontendListener" in stop
    assert "未终止任何进程，运行状态已保留" in stop
    assert "get-content -raw -encoding utf8 -literalpath $statepath" in stop.lower()
    assert "-not $current.command_line_sha256 -or" in startup.lower()
    assert "-not $current.command_line_sha256 -or" in stop.lower()
    assert 'scripts\\stop.ps1' in restart
    assert 'start.ps1' in restart
    assert 'restart-control.log' in restart
    assert ">>" not in restart
    assert restart.count("pause") >= 2
    assert restart.index('scripts\\stop.ps1') < restart.index('start.ps1')


def test_prepublish_uses_a_workspace_owned_pytest_temp_directory():
    prepublish = (ROOT / "scripts" / "prepublish_check.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert '--basetemp ".test-tmp\\prepublish"' in prepublish
