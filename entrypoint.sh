#!/bin/sh
set -e

if [ "$SERVICE_ROLE" = "worker" ]; then
    exec python worker/worker.py
else
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
