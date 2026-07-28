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
