#!/bin/sh
set -e

# Start arq worker in background
arq app.workers.batch_runner.WorkerSettings &

# Start API in foreground (PID 1)
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
