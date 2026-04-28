@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] virtualenv missing. Run setup-chrome.bat first.
  pause
  exit /b 1
)
if not exist "auth_chrome.json" if not exist "auth.json" (
  echo [ERROR] no auth file found. Run setup-chrome.bat first to log in.
  pause
  exit /b 1
)

if not exist logs mkdir logs

rem Strip --quiet (used by Task Scheduler) from args before forwarding to Python.
set QUIET=0
set PYARGS=
:argloop
if "%~1"=="" goto argdone
if /i "%~1"=="--quiet" (
  set QUIET=1
) else (
  set PYARGS=%PYARGS% %1
)
shift
goto argloop
:argdone

for /f "delims=" %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%I
set LOGFILE=logs\run_chrome_%STAMP%.log

echo === Bing Rewards [Chrome] run started at %DATE% %TIME% ===
echo Log: %LOGFILE%
echo.

call .venv\Scripts\activate.bat
.venv\Scripts\python -u bing_rewards.py --browser chrome %PYARGS% 2>&1 | powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $input | ForEach-Object { Write-Host $_; Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }"

set RC=%ERRORLEVEL%
copy /Y "%LOGFILE%" last_run_chrome.log >nul 2>&1

echo.
echo === Exit code: %RC% ===
echo Log saved to: %LOGFILE%
echo.

if "%QUIET%"=="1" (
  exit /b %RC%
)

echo Press any key to close ^(auto-close in 60s^).
choice /t 60 /d y /n >nul
exit /b %RC%
