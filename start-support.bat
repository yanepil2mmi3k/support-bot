@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-support.ps1"
set "LAUNCHER_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %LAUNCHER_EXIT_CODE%
