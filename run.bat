@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] virtualenv missing. Run setup.bat first.
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

rem Build a timestamp via PowerShell (wmic was removed from Windows 11).
for /f "delims=" %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%I
set LOGFILE=logs\run_%STAMP%.log

echo === Bing Rewards run started at %DATE% %TIME% ===
echo Log: %LOGFILE%
echo.

call .venv\Scripts\activate.bat

set RUN_BROWSER=msedge

if not exist "auth_msedge.json" if not exist "auth.json" (
  if exist "auth_chromium.json" (
    echo [setup] using existing Chromium auth because no Edge auth file was found.
    set RUN_BROWSER=chromium
  ) else (
    echo [setup] no Edge auth file found. Importing your existing Edge profile...
    .venv\Scripts\python -u bing_rewards.py --import-profile
    if errorlevel 1 (
      echo [setup] To reuse your already-open Edge login, run import-edge-cookies.bat.
      echo [setup] Edge profile import failed. Launching first-time Edge login...
      .venv\Scripts\python -u bing_rewards.py --login
      if errorlevel 1 (
        echo [setup] Edge login failed. Falling back to Playwright Chromium...
        .venv\Scripts\python -u bing_rewards.py --login --browser chromium
        if errorlevel 1 (
          echo [ERROR] login failed. Complete Microsoft sign-in in the browser window and rerun.
          pause
          exit /b 1
        )
        set RUN_BROWSER=chromium
      )
    )
  )
)

rem Stream output to console AND log file. -u flag forces unbuffered Python.
if "%RUN_BROWSER%"=="chromium" (
  .venv\Scripts\python -u bing_rewards.py --browser chromium %PYARGS% 2>&1 | powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $input | ForEach-Object { Write-Host $_; Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }"
) else (
  .venv\Scripts\python -u bing_rewards.py %PYARGS% 2>&1 | powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $input | ForEach-Object { Write-Host $_; Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }"
)

set RC=%ERRORLEVEL%
copy /Y "%LOGFILE%" last_run.log >nul 2>&1

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
