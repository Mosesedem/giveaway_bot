#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d venv ]]; then
  echo "venv not found — run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

# Use the venv interpreter explicitly so a globally installed uvicorn
# (e.g. /Library/Frameworks/.../bin/uvicorn) cannot shadow the venv one.
exec ./venv/bin/python -m uvicorn app.main:app --reload "$@"