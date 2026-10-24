# New-Order Generation

To enable future generations without retrying historical orders, set these in
the backend Easypanel environment and preserve them on subsequent deployments:

- `STOREFRONT_WORKER_ENABLED=true`
- `STOREFRONT_PROCESSING_START_AT=<fixed activation timestamp with UTC offset>`
- `STOREFRONT_VIDEO_QUEUE=<fresh dedicated queue name>`
- `STOREFRONT_RECOVERY_ENABLED=false`
- `STOREFRONT_WHATSAPP_DELIVERY_ENABLED=false`

Use the same fixed cutoff and queue on subsequent restarts. Never replace the
cutoff with the current time on each deployment, or outstanding eligible orders
will stop processing. Do not reuse or drain the historical `arq:queue`.

The API and worker must share these settings. Producers and workers select the
same video queue. Order creation time is checked before processing, recovery,
outbox dispatch, and delivery. Website delivery does not require WhatsApp.

Before enabling, verify provider credentials, Redis, storage, and worker health.
Metadata checks do not prove that a paid generation will succeed. Do not charge
for a synthetic test or submit historical orders as a test.
