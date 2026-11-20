@echo off
rem Gives Claude Code on another computer SSH access to this PC, and writes what it needs into
rem claude-access-<this PC>.json next to this file. Double-click it; Windows asks for admin rights.
rem Details: ssh\give-access.ps1 and ssh\README.md.

rem fltmc works only elevated; otherwise start this file again as administrator.
fltmc >nul 2>&1
if errorlevel 1 (
    set "SELF=%~f0"
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath $env:SELF"
    if errorlevel 1 pause
    exit /b
)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ssh\give-access.ps1"
if errorlevel 1 (
    echo.
    echo Something failed: see the red text above.
) else (
    explorer /select,"%~dp0claude-access-%COMPUTERNAME%.json"
)
echo.
pause
