@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
set "setup_exit_code=%ERRORLEVEL%"

echo.
if not "%setup_exit_code%"=="0" (
    echo Installation failed. Please keep this window open and check the error above.
) else (
    echo Installation completed. You can now double-click start.cmd.
)
pause
exit /b %setup_exit_code%
