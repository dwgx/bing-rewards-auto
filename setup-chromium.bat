@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH. Install Python 3.10+ and rerun.
  pause
  exit /b 1
)

if not exist ".venv\" (
  echo [setup] creating virtualenv...
  python -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium

echo.
echo [setup] launching first-time login in Playwright Chromium.
echo         Sign in to your Microsoft account, then keep the browser open
echo         until this script saves auth_chromium.json.
python bing_rewards.py --login --browser chromium

echo.
echo [setup] done. Use run-chromium.bat for daily Chromium runs.
pause
