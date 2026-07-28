@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
set "stop_exit_code=%ERRORLEVEL%"
if not "%stop_exit_code%"=="0" (
    echo.
    echo Stop failed. Please keep this window open and check the error above.
    pause
    exit /b %stop_exit_code%
)

call "%~dp0start.cmd"
exit /b %ERRORLEVEL%
