#!/bin/sh
set -eu

TEMPLATE_DIR=${TEMPLATE_DIR:-/app/template}
BASELINE_DIR=${BASELINE_DIR:-/app/baseline}
CODEX_RESET_URL=${CODEX_RESET_URL:-http://codex:8080/reset}
S3_BUCKET_PREVIEW=${S3_BUCKET_PREVIEW:-}
S3_BUCKET_PROD=${S3_BUCKET_PROD:-}
AWS_REGION=${AWS_REGION:-us-east-1}

/app/scripts/init_baseline.sh

if [ ! -d "$TEMPLATE_DIR" ]; then
  echo "template dir not found" >&2
  exit 1
fi

rsync -a --delete "$TEMPLATE_DIR/" "$BASELINE_DIR/"

curl -sS -X POST "$CODEX_RESET_URL" \
  -H "Content-Type: application/json" \
  -d "{\"source_dir\": \"$BASELINE_DIR\"}" > /dev/null

if [ -z "$S3_BUCKET_PREVIEW" ] || [ -z "$S3_BUCKET_PROD" ]; then
  echo "S3 buckets required" >&2
  exit 1
fi

aws s3 sync "$BASELINE_DIR" "s3://$S3_BUCKET_PREVIEW" --region "$AWS_REGION" --delete
aws s3 sync "$BASELINE_DIR" "s3://$S3_BUCKET_PROD" --region "$AWS_REGION" --delete
