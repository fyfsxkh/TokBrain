@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
set "stop_exit_code=%ERRORLEVEL%"
if not "%stop_exit_code%"=="0" (
    echo.
    echo Restart failed while stopping. Please check the error above.
    pause
    exit /b %stop_exit_code%
)

rem Older versions redirected this command to restart-control.log. Child
rem processes inherited and locked that file, so future restarts could not
rem open it. The legacy log is no longer used.
del /f /q "%~dp0data\logs\restart-control.log" >nul 2>&1

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "start_exit_code=%ERRORLEVEL%"
if not "%start_exit_code%"=="0" (
    echo.
    echo Restart failed while starting. Please check the error above.
    pause
    exit /b %start_exit_code%
)

echo Restart succeeded. TokBrain is opening in your browser.
powershell.exe -NoProfile -Command "Start-Sleep -Seconds 3"
exit /b 0
