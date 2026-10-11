# Persona Storefront (FastAPI)

Customer website API and worker for `ViegasD/persona-frontend`. Includes
accounts, email verification/recovery, credits, payments, and video generation
through kie.ai and xAI. The legacy `ViegasD/persona` bot is not used here.

## Local development

```bash
pip install -e .[dev]
alembic upgrade head
uvicorn app.main:app --reload --port 8000
arq app.workers.batch_runner.WorkerSettings   # in another terminal
```

Use Python 3.12+, PostgreSQL, Redis and S3-compatible storage. Configure a
private `.env` using `.env.example`. This service owns the `web` schema and
reads the shared catalog through views. Back up the database before upgrades.
Container startup runs migrations before starting the worker and API.

## Existing Test VPS

The existing Easypanel services on `93.127.210.42` are
`hermes_persona-website` (this repo) and `hermes_persona-frontend` (the frontend
repo). No new infrastructure stack is needed. Pushes may trigger configured
automatic deployments; repository files do not update live service settings.

Registration/recovery requires SMTP and `STOREFRONT_ACCOUNT_PAGE_URL` on both
API and worker. Google sign-in remains commented out pending verification;
leave `STOREFRONT_GOOGLE_CLIENT_ID` unset. Configure the exact frontend origin
in `STOREFRONT_WEB_BASE_URL` and the frontend API URL in `storefront-config.js`.

See [CREDITS.md](CREDITS.md), [FRONTEND_CONFIG.md](FRONTEND_CONFIG.md),
[GOOGLE_AUTH.md](GOOGLE_AUTH.md), and [MEDIA_PRIVACY.md](MEDIA_PRIVACY.md).
MinIO privacy remediation is a separate reviewed operator action.

Run `python -m pytest -q`. Integration tests require a disposable migrated
local database named `persona_storefront_test` via `PERSONA_TEST_DATABASE_URL`;
otherwise they are skipped. Browser tests live in `persona-frontend`.

## Layout

- `app/api/v1/` — HTTP routers (catalog, orders, auth, payments, admin).
- `app/core/` — settings, security, logging.
- `app/db/` — SQLAlchemy models + session.
- `app/services/` — Mercado Pago, RunPod, ComfyUI, script writer, storage, delivery.
- `app/workers/batch_runner.py` — arq worker that drives a batch end-to-end.
- `alembic/` — migrations (own `web` schema; never touches existing tables).
