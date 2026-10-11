# Google Sign-In

Google sign-in is currently paused pending app verification. The
`google-auth.js` script tags are commented out in checkout, the account
page and the video library. Password signup/login and credits remain
available. The backend implementation is retained; remove the comments
on all three script tags only when ready to enable Google again.

The storefront supports Google signup/login in checkout, the video library
and the account page. The account page also links Google to an existing
authenticated account with the same email. A shared `google-auth.js` in `persona-frontend`
loads the official Google Identity Services button only when configured.
The password login/recovery flows remain available.

## Activation

1. In Google Cloud, configure OAuth branding/consent and create an OAuth
   client with application type **Web application**. Configure test users
   while the consent app is in testing.
2. Add the exact frontend origins under **Authorized JavaScript origins**,
   including scheme and port, without a path. Add your deployed HTTPS
   customer domain and `http://localhost:3001` for local browser testing.
3. Set `STOREFRONT_GOOGLE_CLIENT_ID` on the storefront API to that web
   client's ID (ending in `.apps.googleusercontent.com`). It is public
   configuration, not a secret. The existing AI `GOOGLE_API_KEY` is unrelated.
   No OAuth client secret or Google API key is used by this popup ID-token flow.
4. Install the updated backend dependencies and run `alembic upgrade head`
   (revision `0013_google_auth`). Deploy the updated API, HTML pages and
   `storefront-config.js` and `google-auth.js` together. Configure the browser
   API URL as described in `FRONTEND_CONFIG.md`. Keep the API's allowed CORS origins aligned
   with the deployed frontend. Local HTML alone does not enable real Google
   sign-in; use an authorized HTTP/HTTPS origin.
5. Configure SMTP and the existing worker for verification of non-Gmail,
   non-Workspace addresses. Apply gateway rate limits to sign-in/challenge
   endpoints before public launch.

If CSP is enabled, permit the Google Identity Services script, frames and
connections according to Google's setup guide. Review popup/COOP settings
there too. A missing client ID hides Google controls and makes sign-in
endpoints fail closed. SDK/network failures expose a retry control.

## Security and Credits

- The backend uses Google's `google-auth` library to verify signature,
  audience, issuer and expiry; browser-supplied user IDs are never trusted.
- Google `sub` is the stable unique identity. Changed Google emails do not
  rebind the stored account or create another account during sign-in.
- A server-issued random nonce expires in ten minutes. Only its hash is
  stored. Verified nonces are consumed in a separate committed transaction,
  including when later binding fails, and concurrent replay is rejected.
  The browser renews challenges before expiry and after logout. Session
  changes invalidate pending callbacks so stale responses cannot replace
  a newer password or Google session.
- An existing email/password account is not silently merged on email alone.
  Sign in first and use **Vincular Google** in `conta.html`. Linking requires
  a valid account JWT, matching email, and a session-version check under the
  account lock so password resets revoke an in-flight linking session.
- Google-authoritative Gmail/Workspace email can release the welcome
  credit. Other email providers require our email confirmation first.
  The existing canonical identity claim and ledger prevent another welcome
  bonus when linking, signing in again or using Gmail aliases.
- Raw Google ID tokens are not stored. The client retains the same local
  storefront JWT used by the password flow. No One Tap/automatic login or
  Google profile-photo tracking is enabled.

## Verification

`tests/test_google_auth.py` includes offline RSA-signed fixtures exercising
the actual Google verifier, plus invalid claims and disabled configuration.
The disposable PostgreSQL suite covers signup, binding, credit identity,
subject stability, expiry/replay/concurrency, failed binding and session
revocation. `scripts/verify-google-auth.cjs` mocks Google and the API to test
desktop/mobile signup/login/link, disabled configuration and failure/retry.
It does not claim a live OAuth login; that needs your configured client ID.

`scripts/verify-auth-regressions.cjs` exercises delayed responses, logout,
idle nonce expiry, expired-session recovery, guest access, library failures,
checkout identity locking and the shared frontend API configuration. All
provider and API calls are mocked; no emails or payments are sent.

Sources: [Google token verification](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token),
[Google web setup](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid),
[Google button API](https://developers.google.com/identity/gsi/web/reference/js-reference).
