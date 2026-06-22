#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d venv ]]; then
  echo "venv not found — run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

# Use the venv interpreter explicitly so a globally installed uvicorn
# (e.g. /Library/Frameworks/.../bin/uvicorn) cannot shadow the venv one.
PORT="${APP_PORT:-${WEB_PORT:-6768}}"
exec ./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload "$@"