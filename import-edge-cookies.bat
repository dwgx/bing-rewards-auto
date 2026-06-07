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

call .venv\Scripts\activate.bat

echo [import] checking for Edge remote debugging on port 9222...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:9222/json/version -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo [import] Edge is not exposing a debug port.
  echo [import] Close all Edge windows, then press any key. This script will reopen Edge
  echo          with your normal profile and export the current Microsoft cookies.
  pause >nul

  set EDGE_EXE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe
  if not exist "%EDGE_EXE%" set EDGE_EXE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe
  if not exist "%EDGE_EXE%" (
    echo [ERROR] Microsoft Edge was not found.
    pause
    exit /b 1
  )

  start "" "%EDGE_EXE%" --remote-debugging-port=9222 --profile-directory=Default https://rewards.bing.com/
  echo [import] waiting for Edge debug port...
  powershell -NoProfile -Command "$ok=$false; for ($i=0; $i -lt 30; $i++) { try { Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:9222/json/version -TimeoutSec 1 | Out-Null; $ok=$true; break } catch { Start-Sleep -Seconds 1 } }; if ($ok) { exit 0 } else { exit 1 }"
  if errorlevel 1 (
    echo [ERROR] Edge debug port did not open. Make sure all Edge processes are closed, then rerun.
    pause
    exit /b 1
  )
)

echo [import] exporting auth_msedge.json from Edge...
.venv\Scripts\python -u bing_rewards.py --import-cdp http://127.0.0.1:9222 --browser msedge
if errorlevel 1 (
  echo [ERROR] import failed. Open https://rewards.bing.com/ in that Edge window and confirm you are signed in, then rerun.
  pause
  exit /b 1
)

echo.
echo [import] done. You can now run run.bat.
pause
