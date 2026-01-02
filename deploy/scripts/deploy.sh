#!/bin/sh
set -eu

BASELINE_DIR=${BASELINE_DIR:-/app/baseline}
ENABLE_GIT_HISTORY=${ENABLE_GIT_HISTORY:-false}
S3_BUCKET_PROD=${S3_BUCKET_PROD:-}
AWS_REGION=${AWS_REGION:-us-east-1}

/app/scripts/init_baseline.sh
/app/scripts/sync_from_codex.sh "$BASELINE_DIR"

if [ "$ENABLE_GIT_HISTORY" = "true" ]; then
  if [ ! -d "$BASELINE_DIR/.git" ]; then
    git -C "$BASELINE_DIR" init
  fi
  git -C "$BASELINE_DIR" add -A
  git -C "$BASELINE_DIR" commit -m "Deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)" || true
fi

if [ -z "$S3_BUCKET_PROD" ]; then
  echo "S3_BUCKET_PROD required" >&2
  exit 1
fi

aws s3 sync "$BASELINE_DIR" "s3://$S3_BUCKET_PROD" --region "$AWS_REGION" --delete
