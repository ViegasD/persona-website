#!/bin/sh
set -eu

# Limit substitution to bucket-name characters, never JSON or sed syntax.
case "${MINIO_BUCKET:?MINIO_BUCKET is required}" in
  *[!a-z0-9.-]*) echo "Invalid MINIO_BUCKET" >&2; exit 1 ;;
esac

mc alias set local http://minio:9000 "${MINIO_ROOT_USER:?}" "${MINIO_ROOT_PASSWORD:?}"
mc mb "local/$MINIO_BUCKET" --ignore-existing
sed "s/__BUCKET__/$MINIO_BUCKET/g" /etc/persona-minio/public-assets.policy.json > /tmp/public-assets.policy.json
# Replace the whole policy, including any old bucket-wide download grant.
mc anonymous set-json /tmp/public-assets.policy.json "local/$MINIO_BUCKET"
echo 'Public asset policy applied successfully'
