@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "startup_exit_code=%ERRORLEVEL%"

if not "%startup_exit_code%"=="0" (
    echo.
    echo Startup failed. Please keep this window open and check the error above.
    pause
)

exit /b %startup_exit_code%
