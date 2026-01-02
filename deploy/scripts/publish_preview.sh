#!/bin/sh
set -eu

S3_BUCKET_PREVIEW=${S3_BUCKET_PREVIEW:-}
AWS_REGION=${AWS_REGION:-us-east-1}
CODEX_SITE_DIR=${CODEX_SITE_DIR:-/app/codex_site}

if [ -z "$S3_BUCKET_PREVIEW" ]; then
  echo "S3_BUCKET_PREVIEW required" >&2
  exit 1
fi

if [ ! -d "$CODEX_SITE_DIR" ]; then
  echo "codex site dir not found" >&2
  exit 1
fi

aws s3 sync "$CODEX_SITE_DIR" "s3://$S3_BUCKET_PREVIEW" --region "$AWS_REGION" --delete
