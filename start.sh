#!/usr/bin/env bash
# BTC Signal - one-click local start (macOS / Linux). No account, no API key, no paid plan needed.
# Needs Python 3.12+ and Node.js 20+.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x backend/.venv/bin/python ]; then
  echo "[1/3] Creating Python environment..."
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q --upgrade pip
  backend/.venv/bin/pip install -q -r backend/requirements.txt
fi

if [ ! -d frontend/node_modules ]; then
  echo "[2/3] Installing website dependencies..."
  (cd frontend && npm install --no-audit --no-fund)
fi

echo "[3/3] Starting backend (port 8000) and website (port 3000)..."
(cd backend && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000) &
BACK=$!
(cd frontend && npm run dev) &
WEB=$!
trap 'kill $BACK $WEB 2>/dev/null' EXIT INT TERM
echo "Open http://localhost:3000  (first signal appears within about a minute). Ctrl+C to stop."
wait
