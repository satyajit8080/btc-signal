@echo off
REM BTC Signal - one-click local start (Windows). No account, no API key, no paid plan needed.
REM Needs Python 3.12+ and Node.js 20+ installed.
setlocal
cd /d "%~dp0"

if not exist backend\.venv\Scripts\python.exe (
  echo [1/3] Creating Python environment...
  python -m venv backend\.venv || goto :fail
  backend\.venv\Scripts\python.exe -m pip install -q --upgrade pip
  backend\.venv\Scripts\pip.exe install -q -r backend\requirements.txt || goto :fail
)

if not exist frontend\node_modules (
  echo [2/3] Installing website dependencies...
  pushd frontend
  call npm install --no-audit --no-fund || goto :fail
  popd
)

echo [3/3] Starting backend (port 8000) and website (port 3000)...
start "BTC Signal - backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
start "BTC Signal - website" cmd /k "cd /d %~dp0frontend && npm run dev"
timeout /t 8 >nul
start http://localhost:3000
echo.
echo Open http://localhost:3000  (first signal appears within about a minute)
echo Close the two windows to stop.
exit /b 0

:fail
echo Setup failed. Check that Python and Node.js are installed and on PATH.
exit /b 1
