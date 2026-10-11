# Accounts and Credits

The customer pages (`checkout.html`, `conta.html`, `aguardando.html`, and
`meus-videos.html`) live in `ViegasD/persona-frontend`. This repository,
`ViegasD/persona-website`, contains their API and worker. The old `persona`
bot repository is not part of this release.

## Current Policy

- Registration, email verification and Google sign-in grant zero credits.
- Paid video orders grant no bonus credits. Free credits are prohibited.
- One video costs one credit. Standalone paid credit purchases are not yet
  implemented; the current credit API only exposes balance/history and redemption.
- Historical balances, ledger records and welcome-claim tables are preserved.
- Balances cannot go negative; each order can have only one transaction of
  each ledger kind. Order and account locks serialize checkout and rewards.
- Existing accounts start with zero credits. Historical purchases are not
  automatically rewarded.
- Credits require verified email. Full refunds/chargebacks reverse the
  funding payment's purchase reward or restore redeemed credits once.
  Spent rewards become debt, repaid before new credits become spendable.
  Partial refunds and refunds of unrelated attempts do not revoke orders.

## Customer Flow

Google signup/login and authenticated account linking are implemented.
Activation/security details are in [GOOGLE_AUTH.md](GOOGLE_AUTH.md).
Both login methods share the same account; neither grants any credits.

Customers can register or log in during checkout, select credits, see their
balance, and sign out. The waiting page carries account authentication.
`meus-videos.html` supports login, saved orders, current balance, and logout.
The homepage links directly to login and standalone signup on `conta.html`;
signed-in navigation links to the account and credits. Registration no longer
requires creating an order. The account screen includes logout and supports
confirmation, resend, password recovery/reset,
balance/debt and credit history. Confirmation links expire in 24 hours;
reset links in 30 minutes. Tokens are hashed and single-use. Browser
fragments clear immediately; resets revoke existing sessions. Email
requests have a 60-second per-account/purpose cooldown.
`GET /api/v1/config` exposes only public keys and safe email/processing
availability flags, with no-store caching. Account and checkout screens explain
unavailable signup/email delivery and paused generation without bypassing
verification or changing balances. Google buttons remain commented out.
The admin panel's storefront settings control registration, credit redemption,
accepting new orders and mandatory account checkout, independently of the legacy
bot settings. Defaults preserve existing checkout access. Deployment-level worker
flags and historical cutoff remain hard limits, never editable through this API.
Settings saves use optimistic revision checks. Free-credit policy is fixed at zero.
Guest contact lookup requires a signed guest-order token or account access.
Guest claiming requires login, matching email, and signed order ownership.

Payment retries reuse an unchanged order in the current page. Card selection
does not return an existing Pix QR code. Approved card payments now record
approval and queue video processing. Pix callbacks and card approvals share
the reward service; delayed callbacks are matched to their exact attempt.
Refunded orders cannot be delivered or approved, and customer media and
download endpoints deny them. Already downloaded files cannot be revoked.

Email, video and delivery intents are committed with business state in a
PostgreSQL outbox. Dispatch runs every 10 seconds, retries with backoff,
uses stable Redis job IDs and scrubs email-link payloads after delivery.
Generation output and its next-phase intent commit atomically. Admin
retries use fresh durable job identities.

Recovery runs every 15 minutes for work older than 75 minutes, beyond the
one-hour worker timeout. It resumes from saved output, skips refunded and
active orders, and stops after five phase attempts for admin inspection.
Crashes before saving a provider result can still cause repeated provider
costs. SMTP/WhatsApp are at-least-once external deliveries across crashes.

Checkout loads plans and the paginated character catalog from the API,
including authoritative prices/limits. Failures block checkout with retry;
the hardcoded package/character catalogs have been removed.

Checkout progress uses custom analytics events instead of repeated leads.
Browser and server InitiateCheckout/Purchase events use matching order IDs.
Credit redemption does not report cash revenue. Pix and Hipercard logos are
local assets, removing their runtime dependency on external image hosting.

## Migration and Verification

Use Python 3.12 or newer. In this repository, install `.[dev]`, then run
`alembic upgrade head` against the intended database before starting the
updated API and worker. Revisions 0007 through 0013 add credits, Google identity, account
lifecycle, durable jobs, funding markers, catalog views and slug-array
corrections. Public Node tables are not altered. Fresh PostgreSQL upgrades
and rollback/re-upgrade were exercised locally. View/slug-array downgrades
intentionally do not restore incompatible old representations. Back up
the intended database first; production has not been changed.

Configure the same environment on API and worker:
- `STOREFRONT_ACCOUNT_PAGE_URL`: deployed HTTPS URL of `conta.html`.
- `STOREFRONT_SMTP_HOST`, `STOREFRONT_SMTP_FROM`, port and auth credentials.
- Port 587: STARTTLS=true, SSL=false. Port 465: SSL=true, STARTTLS=false,
  using `STOREFRONT_SMTP_STARTTLS` and `STOREFRONT_SMTP_SSL`.
- Existing database, Redis, JWT, storage and payment-provider configuration.
- `MERCADOPAGO_WEBHOOK_SECRET` and gateway rate limits.

Registration/recovery return 503 when SMTP is not configured. Keep the
worker running and monitor undelivered `web.outbox_job` rows, attempts,
`last_error`, FAILED items and debt.

Backend verification: `.venv/Scripts/python -m pytest -q`.
Integration tests additionally require a disposable migrated database:
`PERSONA_TEST_DATABASE_URL=postgresql://...@127.0.0.1:<port>/persona_storefront_test`.
Tests reject other database names/hosts and mock external providers.
Browser verification from the `persona-frontend` repository root:
`node scripts/verify-storefront.cjs <path-to-playwright-module>`.
Account lifecycle: `node scripts/verify-account-lifecycle.cjs <path-to-playwright-module>`.
The browser check mocks every remote request and writes screenshots into
`.cache/storefront-qa`. It checks mobile/desktop overflow, local assets,
retry reuse, credit checkout, analytics, account history, and waiting-page
authentication. It does not make real payments.

## Staging Checks

- Configure SMTP and verify actual deliverability/domain authentication.
- Exercise Mercado Pago payments/refunds and Meta deduplication in staging.
  Verification here sent no real emails and made no paid provider calls.
- Canonical email claims cannot prevent registrations with multiple real
  mailboxes. Monitor abuse and add edge rate limits before promotions.

This is a verified pass over the storefront account/payment flow, not a
claim that every issue in the Node backend or admin application is resolved.
