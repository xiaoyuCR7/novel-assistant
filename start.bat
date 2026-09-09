@echo off
setlocal DisableDelayedExpansion
title Novel Assistant - Frontend and Backend

pushd "%~dp0"
if errorlevel 1 (
    echo [ERROR] Startup failed: cannot open the project directory.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1"
set "launchExitCode=%errorlevel%"
popd

if not "%launchExitCode%"=="0" (
    echo [ERROR] Startup failed. Check the messages above.
    pause
)
exit /b %launchExitCode%
