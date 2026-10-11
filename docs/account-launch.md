# Account Launch Checklist

The frontend provides standalone login/signup at `conta.html#login` and
`conta.html#signup`, account balance/history, email confirmation and password
recovery. The homepage and video library link to these screens. No migration
or historical order update is required for this navigation release.

## Email Delivery

Recommended provider: **Resend via SMTP**, using the existing mail delivery
implementation, with no SDK or new dependency. Create a Resend account, add and
verify `recadoanimado.com` (or a dedicated sending subdomain), and create a
send-only API key restricted to that domain. If DNS is hosted on Cloudflare,
Resend offers automatic DNS setup through Domain Connect; otherwise add only
the records shown by Resend, preserving existing mail/DNS records.

Paste these non-secret settings into the API and account-worker environment:

```dotenv
STOREFRONT_ACCOUNT_PAGE_URL=https://recadoanimado.com/conta.html
STOREFRONT_SMTP_HOST=smtp.resend.com
STOREFRONT_SMTP_PORT=587
STOREFRONT_SMTP_USER=resend
STOREFRONT_SMTP_FROM=Recado Animado <conta@recadoanimado.com>
STOREFRONT_SMTP_STARTTLS=true
STOREFRONT_SMTP_SSL=false
```

Set `STOREFRONT_SMTP_PASSWORD` privately to the Resend API key. Use a sender
address under the domain actually verified; the example assumes verification
of the root domain. Keep open/click tracking disabled for account links.

Provider references: [SMTP settings](https://resend.com/docs/send-with-smtp),
[domain verification](https://resend.com/docs/dashboard/domains/introduction),
[Cloudflare automatic setup](https://resend.com/docs/guides/dns/cloudflare).

### Other SMTP Providers

An operator must configure the same values on the API and account-mail worker:

- `STOREFRONT_ACCOUNT_PAGE_URL=https://recadoanimado.com/conta.html`
- `STOREFRONT_SMTP_HOST`: provider SMTP hostname.
- `STOREFRONT_SMTP_FROM`: verified sender address.
- `STOREFRONT_SMTP_USER` and `STOREFRONT_SMTP_PASSWORD`: provider credentials.
- `STOREFRONT_SMTP_PORT=587`, `STOREFRONT_SMTP_STARTTLS=true`,
  `STOREFRONT_SMTP_SSL=false`; for port 465 use SSL=true and STARTTLS=false.

Keep secrets in the VPS environment, never in source control or chat. Complete
the provider's sender/domain authentication and verify delivery to a test inbox.
The public config's `account_email_enabled` flag checks configuration presence,
not successful delivery; inspect account-email outbox failures as well.

The test VPS currently launches only the API. Start the **account-only** worker
with `arq app.workers.batch_runner.AccountWorkerSettings` once SMTP is ready.
The repository's `start.sh` also selects this worker when processing is disabled;
changing the VPS command must retain all processing safety flags below.

## Preserve The Historical Processing Hold

Keep `STOREFRONT_WORKER_ENABLED=false`,
`STOREFRONT_RECOVERY_ENABLED=false`, and
`STOREFRONT_WHATSAPP_DELIVERY_ENABLED=false`.
Do not start `WorkerSettings` or enable recovery. Account email processing must
not retry old video jobs, grant historical purchase rewards or alter orders.

New payment/redemption is intentionally blocked while generation is paused.
Enabling it requires separate approval and an explicit timezone-aware
`STOREFRONT_PROCESSING_START_AT` cutoff that excludes historical orders.
Google sign-in stays commented out pending verification.

## Verification

Run backend `python -m pytest -q`, then frontend:

```text
node scripts/verify-account-lifecycle.cjs <playwright-module-path>
node scripts/verify-account-navigation.cjs <playwright-module-path>
node scripts/verify-storefront.cjs <playwright-module-path>
```

These browser tests mock remote requests. They send no real emails, payments or
generation jobs. Verify real email delivery separately using an approved test
account; never use historical customer records for testing.
