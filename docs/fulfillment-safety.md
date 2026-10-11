# Fulfillment Safety

Website access is the primary delivery channel. Evolution/WhatsApp credentials
are not required; `STOREFRONT_WHATSAPP_DELIVERY_ENABLED` defaults to `false`.
Delivery requires the plan's exact video count, all items READY, and saved video
keys. Repeated delivery reuses the successful website delivery record.

## Processing Pause

`STOREFRONT_WORKER_ENABLED=false` is the default and pauses video generation,
delivery, recovery, and new checkout. Startup selects an account-only ARQ worker
on a separate queue so queued video jobs are preserved. It still dispatches account
emails. Starting the normal worker while paused is rejected before queue consumption.
Existing authorized video downloads remain available. These guards work
even when deployment restores the normal container entrypoint.

To process only newly created orders, explicitly set:

```dotenv
STOREFRONT_WORKER_ENABLED=true
STOREFRONT_PROCESSING_START_AT=<approved timezone-aware deployment timestamp>
STOREFRONT_RECOVERY_ENABLED=false
STOREFRONT_WHATSAPP_DELIVERY_ENABLED=false
```

The cutoff is required when enabling processing and applies to order creation
time, not payment time. Orders and pending jobs before it are skipped without
changing records. Historical drafts cannot be checked out; create a new order.
Recovery is separately opt-in and respects the same cutoff. Do not move the
cutoff backward to recover historical orders without explicit business approval.

No data migration or historical-order cleanup accompanies these changes.
Provider tests are mocked; integration tests use a disposable local database.
Video downloads are checked with `ffprobe` before upload/readiness. Docker installs
it through the `ffmpeg` package; local workers also need FFmpeg on their PATH.
Composite images use a separate fully decoded image download path.

Optional WhatsApp sends use an at-most-once delivery claim committed before the
network call. A crash can leave that notification PENDING for operator review;
it is not automatically resent. Website delivery is already committed in that
case and is not dependent on the notification.
