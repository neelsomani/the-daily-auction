#!/bin/bash

set -euo pipefail

# Load env vars
if [ ! -f .env ]; then
  echo "Missing .env file. Copy .env.template and fill it in."
  exit 1
fi
set -a && source .env && set +a

LAMBDA_NAME="${LAMBDA_NAME:-auction-settlement-daily}"
REGION="${AWS_REGION:-us-west-2}"
ZIPFILE="function.zip"
HANDLER="handler.handler"
ROLE_NAME="${LAMBDA_ROLE_NAME:-AuctionLambdaExecutionRole}"
RULE_NAME="${SCHEDULE_RULE_NAME:-daily-auction-settlement}"
CRON_EXPR="${SCHEDULE_CRON_EXPR:-cron(0 0 * * ? *)}"

export AWS_PAGER=""

aws_cli() {
  if [ -n "${AWS_PROFILE:-}" ]; then
    aws --profile "$AWS_PROFILE" "$@"
  else
    aws "$@"
  fi
}

wait_for_lambda_update() {
  local name="$1"
  local status
  for _ in {1..30}; do
    status=$(aws_cli lambda get-function-configuration --function-name "$name" --query 'LastUpdateStatus' --output text 2>/dev/null || echo "")
    if [ "$status" != "InProgress" ] && [ -n "$status" ]; then
      return 0
    fi
    sleep 2
  done
  return 1
}

AWS_USER_ID=$(aws_cli sts get-caller-identity --query 'UserId' --output text 2>/dev/null || echo "")
ACCOUNT_ID=$(aws_cli sts get-caller-identity --query 'Account' --output text 2>/dev/null || echo "")

if [ -z "$ACCOUNT_ID" ]; then
  echo "Error: Could not retrieve AWS account ID"
  exit 1
fi

ACCOUNT_ID=$(echo "$ACCOUNT_ID" | tr -d '[:space:]')
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

if ! aws_cli iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws_cli iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document file://<(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
  )

  aws_cli iam attach-role-policy --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_DIR="$SCRIPT_DIR/../jobs/auction_settlement"
ZIPFILE_PATH="$JOB_DIR/$ZIPFILE"
PACKAGE_DIR="$JOB_DIR/package"

rm -rf "$PACKAGE_DIR" "$ZIPFILE_PATH"
mkdir -p "$PACKAGE_DIR"

docker run --rm --platform linux/amd64 \
  -v "$JOB_DIR":/lambda \
  -w /lambda \
  amazonlinux:2023 \
  bash -c "
    set -e
    dnf install -y python3.12 python3.12-devel python3.12-pip gcc gcc-c++ make automake autoconf libtool >/dev/null
    python3.12 -m pip install --upgrade pip
    python3.12 -m pip install --target package -r requirements.txt --upgrade
  "

cp "$JOB_DIR/handler.py" "$PACKAGE_DIR/"
cp "$JOB_DIR/auction_client.py" "$PACKAGE_DIR/"

(cd "$PACKAGE_DIR" && zip -qr "$ZIPFILE_PATH" .)

if [ -z "${AUCTION_PROGRAM_ID:-}" ] || [ -z "${CRANKER_PRIVATE_KEY:-}" ]; then
  echo "Missing required AUCTION_PROGRAM_ID or CRANKER_PRIVATE_KEY in .env."
  exit 1
fi

ENVIRONMENT_JSON=$(jq -n \
  --arg AUCTION_PROGRAM_ID "$AUCTION_PROGRAM_ID" \
  --arg CRANKER_PRIVATE_KEY "$CRANKER_PRIVATE_KEY" \
  --arg RPC_URL "${RPC_URL:-https://api.devnet.solana.com}" \
  --arg MAX_BATCH_SIZE "${MAX_BATCH_SIZE:-20}" \
  --arg RETRY_WINDOW_SECONDS "${RETRY_WINDOW_SECONDS:-1800}" \
  --arg RETRY_INTERVAL_SECONDS "${RETRY_INTERVAL_SECONDS:-45}" \
  --arg MAX_RUNTIME_SECONDS "${MAX_RUNTIME_SECONDS:-780}" \
  '{
     Variables: {
       AUCTION_PROGRAM_ID: $AUCTION_PROGRAM_ID,
       CRANKER_PRIVATE_KEY: $CRANKER_PRIVATE_KEY,
       RPC_URL: $RPC_URL,
       MAX_BATCH_SIZE: $MAX_BATCH_SIZE,
       RETRY_WINDOW_SECONDS: $RETRY_WINDOW_SECONDS,
       RETRY_INTERVAL_SECONDS: $RETRY_INTERVAL_SECONDS,
       MAX_RUNTIME_SECONDS: $MAX_RUNTIME_SECONDS
     }
   }')

if aws_cli lambda get-function --function-name "$LAMBDA_NAME" >/dev/null 2>&1; then
  echo "Updating existing Lambda function..."
  aws_cli lambda update-function-code \
    --function-name "$LAMBDA_NAME" \
    --zip-file "fileb://$ZIPFILE_PATH"

  wait_for_lambda_update "$LAMBDA_NAME" || true
  
  aws_cli lambda update-function-configuration \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.12 \
    --handler "$HANDLER" \
    --timeout 300 \
    --memory-size 1024 \
    --environment "$ENVIRONMENT_JSON"
else
  echo "Creating new Lambda function..."
  aws_cli lambda create-function \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.12 \
    --handler "$HANDLER" \
    --timeout 300 \
    --memory-size 1024 \
    --zip-file "fileb://$ZIPFILE_PATH" \
    --region "$REGION" \
    --role "$ROLE_ARN" \
    --environment "$ENVIRONMENT_JSON"
fi

aws_cli events put-rule --schedule-expression "$CRON_EXPR" --name "$RULE_NAME" --region "$REGION"
aws_cli events put-targets --rule "$RULE_NAME" \
  --targets "Id"="1","Arn"="arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$LAMBDA_NAME"
aws_cli lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id eventbridge \
  --action 'lambda:InvokeFunction' \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:$REGION:$ACCOUNT_ID:rule/$RULE_NAME" || true

echo "Deployment complete."
