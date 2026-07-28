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
