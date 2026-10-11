# Media privacy

## Reviewed policy (requires operator application)

The supplied MinIO setup helper replaces the shared bucket's anonymous policy with
`docker/minio/public-assets.policy.json`, substituting `MINIO_BUCKET`
(default `persona-images`). Only `s3:GetObject` on `characters/*` and
`templates/*` is public. Anonymous listing, uploads, and deletion are not
granted. Everything else, including `web/orders/<id>/video-<n>.mp4`,
storefront thumbnails, `sessions/*`, and `composite-frames/*`, requires
authenticated access. Predictable keys no longer grant anonymous access.

The prefixes were traced in the Node character/template upload routers,
Node `buildS3Key` (`sessions/`), and storefront `storefront_key` (default
`web/`). Public assets share a bucket with generated media; moving the
storefront to another bucket would also move its catalog reads and require
application changes. A scoped policy preserves the existing bucket and keys.

The policy contains no explicit Deny. Node and storefront authenticated
reads/writes and credential-authorized presigned GET URLs remain available;
URL expiry, signing credentials, endpoint reachability, and application
authorization still apply. Presigned URLs are bearer links: anyone holding
a valid link can read until it expires. Previously downloaded media cannot
be revoked by a bucket policy change.

Keep `S3_BUCKET` consistent with `MINIO_BUCKET`, and keep the storefront
output prefix outside `characters/` and `templates/`. Custom prefixes and
additional buckets require their own policy audit. Store only intentionally
public assets under the two public prefixes.

## Existing deployment remediation (operator action only)

Changing repository files does not change an existing MinIO policy until
setup is explicitly run. No live policy changes were performed for this fix.
The following commands are instructions for an authorized operator, not
automatic deployment steps.

1. Confirm the target endpoint, bucket, and Docker project. Inventory public
   asset prefixes and any CDN/reverse-proxy cache. Use an existing securely
   configured `mc` alias called `deployment`; do not paste credentials into
   this document or command logs.
2. Back up the current policy to a restricted operator-local file:

   ```sh
   mc anonymous get-json deployment/persona-images > prior-anonymous-policy.json
   ```

3. From the repository root, render the reviewed policy for the actual bucket
   name (replace `persona-images` below for a customized deployment):

   ```sh
   sed 's/__BUCKET__/persona-images/g' docker/minio/public-assets.policy.json > public-assets.policy.json
   ```

4. Explicitly replace the complete old policy, rather than adding prefix
   grants while leaving a bucket-wide grant in place:

   ```sh
   mc anonymous set-json public-assets.policy.json deployment/persona-images
   mc anonymous get-json deployment/persona-images
   ```

   The optional `docker/minio/setup.sh` helper assumes a `minio:9000`
   endpoint and policy files mounted at `/etc/persona-minio`. It is not
   part of the API container startup or automatically run on Easypanel.
   Use the reviewed `mc` commands above for the existing VPS. No objects
   are migrated or deleted. Review every other media bucket separately.

5. Verify unsigned GETs for a known existing storefront video and thumbnail
   return access denied; verify known character and template GETs succeed.
   Verify anonymous bucket listing is denied, authenticated uploads work,
   and a fresh application-issued presigned media URL succeeds. Use known
   existing objects: a missing key is not proof of privacy. Treat presigned
   URLs as sensitive and avoid writing them to shared logs.
6. Purge any previously public private-media responses from CDN/proxy caches
   and confirm no alternate public origin bypasses the bucket policy.
   Investigate prior exposure separately. Do not restore the broad policy as
   a routine rollback: it would expose existing generated media again.

## Offline regression check

From this repository root, run:

```sh
python -m pytest tests/test_media_privacy.py -q
```

The checks parse the JSON policy, verify the exact public actions/resources,
exclude generated-media keys, and guard the setup helper. They make
no network calls and do not verify a running MinIO deployment; the operator
checks above are required after remediation.
