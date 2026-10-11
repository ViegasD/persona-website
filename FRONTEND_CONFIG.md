# Frontend configuration

All maintained account, library, checkout and waiting pages load
`storefront-config.js` in `ViegasD/persona-frontend` before their inline code. That file supplies
the API base URL to the pages and the shared Google helper. Deploy it with
the HTML pages and `google-auth.js`; do not deploy only the helper.

## API URL

The existing production API is the fallback configured in that one file.
On localhost, 127.0.0.1 and IPv6 loopback, the default is
`http://localhost:8001/api/v1`. Serve the frontend at
`http://localhost:3001`, with the storefront API listening on port 8001.

For staging or a different production API, set the fallback in the shared
file, or inject trusted deployment configuration before that script:

```html
<script>
window.PERSONA_CONFIG = { apiBaseUrl: 'https://staging-api.example.com/api/v1' };
</script>
<script src="storefront-config.js"></script>
```

This is public deployment configuration, not a secret. Never put API keys
or provider credentials in it, and do not derive it from a query string or
untrusted user input. HTTPS pages must use an HTTPS API. Configure the API's
CORS origins and Google's authorized JavaScript origins for the exact
frontend scheme, hostname and port. Google login requires an authorized
HTTP/HTTPS origin, not a file URL.

`STOREFRONT_API_BASE_URL` remains a backend setting for generated API/media
URLs; it does not modify static browser assets automatically. Update both
settings when changing environments, and invalidate cached HTML/scripts.

## Session behavior

The shared session helper emits changes on login/logout and tracks
cross-tab storage changes. Google callbacks are bound to both the session
and their specific challenge. Checkout disables account switching while
submitting and verifies the captured session before and after its API
requests. If another tab changes accounts, submission stops rather than
continuing under the new identity. A subsequent attempt requires checking
the displayed account and order details again.

## Verification

From the repository root, run:

```sh
node scripts/verify-auth-regressions.cjs /path/to/playwright
node scripts/verify-google-auth.cjs /path/to/playwright
node scripts/verify-storefront.cjs /path/to/playwright
node scripts/verify-account-lifecycle.cjs /path/to/playwright
```

The regression suite blocks or mocks every remote request. Real Google
OAuth, SMTP, payment gateways and live storage policy changes are not
performed by these browser checks.
