#!/bin/sh
set -e

PORT="${APP_PORT:-6768}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"