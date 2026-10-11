#!/bin/sh
set -e

# Run DB migrations
alembic upgrade head

# Start arq worker in background
if python -c 'from app.core.settings import get_settings; import sys; sys.exit(0 if get_settings().worker_enabled else 1)'; then
    arq app.workers.batch_runner.WorkerSettings &
else
    arq app.workers.batch_runner.AccountWorkerSettings &
fi

# Start API in foreground (PID 1)
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
