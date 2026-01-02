#!/usr/bin/env bash
set -euo pipefail

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ".env"
  set +a
fi

if [ -z "${S3_BUCKET_EDITOR:-}" ]; then
  echo "S3_BUCKET_EDITOR required" >&2
  exit 1
fi

AWS_REGION="${AWS_REGION:-us-east-1}"

if [ ! -d "editor" ]; then
  echo "editor directory not found" >&2
  exit 1
fi

aws s3 sync "editor" "s3://${S3_BUCKET_EDITOR}" --region "$AWS_REGION" --delete
