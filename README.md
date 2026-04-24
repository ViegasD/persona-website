# Persona Storefront (FastAPI)

Website backend that sells the same cameo-video products as the WhatsApp bot.
Generation is delegated to a RunPod GPU pod running ComfyUI; orders queue up
and are processed in batches.

## Local development

```bash
cd apps/storefront
pip install -e .[dev]
alembic upgrade head
uvicorn app.main:app --reload --port 8000
arq app.workers.batch_runner.WorkerSettings   # in another terminal
```

The service expects the same Postgres / Redis / S3 already used by the Node
backend (see root `.env.example`). It writes only to the `web` Postgres
schema and reads `character` / `occasion` / `style_template` through views.

## Layout

- `app/api/v1/` — HTTP routers (catalog, orders, auth, payments, admin).
- `app/core/` — settings, security, logging.
- `app/db/` — SQLAlchemy models + session.
- `app/services/` — Mercado Pago, RunPod, ComfyUI, script writer, storage, delivery.
- `app/workers/batch_runner.py` — arq worker that drives a batch end-to-end.
- `alembic/` — migrations (own `web` schema; never touches existing tables).
