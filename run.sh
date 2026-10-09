#!/usr/bin/env bash
# Start ScriptFactory. Open http://localhost:8000 on a laptop, or http://<your-ip>:8000 on a phone on the same Wi-Fi.
cd "$(dirname "$0")"
pip install -q -r requirements.txt --break-system-packages 2>/dev/null || pip install -q -r requirements.txt
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
