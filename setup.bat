@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH. Install Python 3.10+ and rerun.
  exit /b 1
)

if not exist ".venv\" (
  echo [setup] creating virtualenv...
  python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

rem We drive the real Edge binary via channel="msedge", so no chromium download needed.
rem But Playwright still needs its runtime files:
python -m playwright install-deps 2>nul

echo.
echo [setup] importing your existing Edge Microsoft login...
python bing_rewards.py --import-profile
if errorlevel 1 (
  echo [setup] profile import failed. Launching first-time Edge login...
  python bing_rewards.py --login
)

echo.
echo [setup] done. Use run.bat daily, or schtasks /create to schedule.
pause
