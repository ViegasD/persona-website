# Financial reporting

Migration `0016_admin_finance` adds financial settings, order annotations and provider payment snapshots. It does not update customer balances or existing order/payment states. Back up the database before migration.

The authenticated `/api/v1/admin/finance` report separates BRL payment receipts from micro-USD generation costs. Configure the billing USD/BRL rate in the admin panel; the default is unset. Configure text/review token prices for each model in USD per million tokens. Missing prices, exchange rates, fees or video cost records prevent a complete contribution margin.

Contribution is receipts minus refunds, payment fees, estimated affiliate commission, recorded generation costs, per-video overhead and order adjustments. Revenue, fees and order adjustments are allocated equally among an order's videos. All logged attempts count, including failures and regenerations. This is not accounting net profit: taxes and unallocated fixed expenses are excluded. Changing financial assumptions recalculates the report; the result is not an immutable historical accounting ledger. Affiliate commission uses the current affiliate percentage and is labeled estimated.

Historical generation logs remain estimates and may omit older attempts or text/review calls. New usage records include provenance, model and provider request IDs when available. OpenAI token usage stays unpriced until configured; xAI estimates are replaced only when explicit provider usage cost is returned. Records without an item ID are reported separately and are not silently assigned to an order.

`POST /admin/orders/{id}/reconcile-fees` performs a Mercado Pago read and saves isolated financial facts after validating currency, amount and external reference. It never calls webhook handlers, changes payment state, grants credits, delivers media or enqueues generation. Explicit empty provider fee arrays mean zero; absent fees remain unknown. Manual order fee overrides take priority, then provider fees, then the configured estimate.

`PUT /admin/orders/{id}/finance-note` stores test flags, notes and manual cost adjustments in a separate table. Tests are excluded from financial reports by default, but their order records are preserved. Reports accept date, order/name/email, order status, attention and test filters; date boundaries use America/Araguaina. Reports cap at 5,000 matching orders and expose truncation.

Processing eligibility still uses the existing server cutoff and queue restrictions. Finance operations never relax them. Do not enable historical recovery as part of deploying this feature.

Validation: run the complete pytest suite against a disposable PostgreSQL database with `PERSONA_TEST_DATABASE_URL`; it includes migration, lifecycle, financial allocation, unknown-cost and read-only reconciliation checks. Never point lifecycle tests at the application database.
