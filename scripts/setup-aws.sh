#!/usr/bin/env bash
set -euo pipefail

if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ".env"
  set +a
fi

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "Missing required env var: $name" >&2
    exit 1
  fi
}

require_env AWS_REGION
require_env S3_BUCKET_PREVIEW
require_env S3_BUCKET_PROD
require_env S3_BUCKET_EDITOR
require_env CF_DOMAIN_PREVIEW
require_env CF_DOMAIN_PROD
require_env CF_DOMAIN_EDITOR
PREVIEW_FRAME_ANCESTORS=${PREVIEW_FRAME_ANCESTORS:-"https://editor.thedailyauction.com http://localhost:8000"}
PREVIEW_FRAME_ANCESTORS="${PREVIEW_FRAME_ANCESTORS#\"}"
PREVIEW_FRAME_ANCESTORS="${PREVIEW_FRAME_ANCESTORS%\"}"

ACM_REGION="${ACM_REGION:-us-east-1}"
ACM_CERT_ARN="${ACM_CERT_ARN:-${ACM_CERT_ARN_PREVIEW:-}}"

AWS_REGION_LOWER="$(printf "%s" "$AWS_REGION" | tr '[:upper:]' '[:lower:]')"

print_validation_records() {
  local cert_arn="$1"
  echo "DNS validation records (add these CNAMEs):"
  local output
  output="$(aws acm describe-certificate --region "$ACM_REGION" --certificate-arn "$cert_arn" \
    --query 'Certificate.DomainValidationOptions[].ResourceRecord' --output text)"
  if [ -z "$output" ] || [ "$output" = "None" ]; then
    echo "(not available yet) re-run after a minute with:"
    echo "aws acm describe-certificate --region $ACM_REGION --certificate-arn $cert_arn --query 'Certificate.DomainValidationOptions[].ResourceRecord' --output text"
    return
  fi
  printf "%s\n" "$output" | while read -r name type value; do
    if [ -n "$name" ] && [ -n "$type" ] && [ -n "$value" ]; then
      echo "- $name $type $value"
    fi
  done
}

if [ -z "${ACM_CERT_ARN:-}" ]; then
  cert_arn="$(aws acm request-certificate --region "$ACM_REGION" \
    --domain-name "$CF_DOMAIN_PREVIEW" \
    --subject-alternative-names "$CF_DOMAIN_PROD" "$CF_DOMAIN_EDITOR" \
    --validation-method DNS \
    --query 'CertificateArn' --output text)"

  echo "Requested ACM cert: $cert_arn"
  echo "Ensure AWS credentials are set in your environment before running AWS CLI commands."
  echo "DNS validation records are not immediate. Wait a minute, then run:"
  echo "aws acm describe-certificate --region $ACM_REGION --certificate-arn $cert_arn --query 'Certificate.DomainValidationOptions[].ResourceRecord' --output text"
  echo "Add those CNAMEs in your DNS provider to validate the cert."
  echo "To check status:"
  echo "aws acm describe-certificate --region $ACM_REGION --certificate-arn $cert_arn --query 'Certificate.Status' --output text"
  echo "After it is ISSUED, set ACM_CERT_ARN to this ARN and re-run."
  exit 0
fi

cert_status="$(aws acm describe-certificate --region "$ACM_REGION" \
  --certificate-arn "$ACM_CERT_ARN" --query 'Certificate.Status' --output text)"

if [ "$cert_status" != "ISSUED" ]; then
  echo "ACM cert not issued yet (status: $cert_status)."
  print_validation_records "$ACM_CERT_ARN"
  echo "Validate the DNS records above, wait for ISSUED, then re-run."
  exit 0
fi

create_bucket() {
  local bucket="$1"
  if aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    echo "Bucket exists: $bucket"
  else
    if [ "$AWS_REGION_LOWER" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$bucket" --region "$AWS_REGION_LOWER" >/dev/null
    else
      aws s3api create-bucket \
        --bucket "$bucket" \
        --region "$AWS_REGION_LOWER" \
        --create-bucket-configuration "LocationConstraint=$AWS_REGION_LOWER" >/dev/null
    fi
    echo "Created bucket: $bucket"
  fi

  aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null

  aws s3api put-bucket-ownership-controls --bucket "$bucket" --ownership-controls \
    "Rules=[{ObjectOwnership=BucketOwnerEnforced}]" >/dev/null
}

create_bucket "$S3_BUCKET_PREVIEW"
create_bucket "$S3_BUCKET_PROD"
create_bucket "$S3_BUCKET_EDITOR"

cache_policy_name="thedailyauction-low-ttl"
cache_policy_id="$(aws cloudfront list-cache-policies --type custom \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='$cache_policy_name'].CachePolicy.Id | [0]" \
  --output text)"

if [ -z "$cache_policy_id" ] || [ "$cache_policy_id" = "None" ]; then
  cache_policy_id="$(aws cloudfront create-cache-policy --cache-policy-config '{
    "Name": "'"$cache_policy_name"'",
    "DefaultTTL": 5,
    "MaxTTL": 60,
    "MinTTL": 0,
    "ParametersInCacheKeyAndForwardedToOrigin": {
      "EnableAcceptEncodingGzip": true,
      "EnableAcceptEncodingBrotli": true,
      "HeadersConfig": { "HeaderBehavior": "none" },
      "CookiesConfig": { "CookieBehavior": "none" },
      "QueryStringsConfig": { "QueryStringBehavior": "none" }
    }
  }' --query 'CachePolicy.Id' --output text)"
  echo "Created cache policy: $cache_policy_id"
else
  echo "Using cache policy: $cache_policy_id"
fi

headers_policy_name="thedailyauction-preview-csp"
headers_policy_id="$(aws cloudfront list-response-headers-policies --type custom \
  --query "ResponseHeadersPolicyList.Items[?ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name=='$headers_policy_name'].ResponseHeadersPolicy.Id | [0]" \
  --output text)"

if [ -z "$headers_policy_id" ] || [ "$headers_policy_id" = "None" ]; then
  csp_value="frame-ancestors ${PREVIEW_FRAME_ANCESTORS}"
  csp_value_escaped="$(printf "%s" "$csp_value" | sed 's/"/\\"/g')"
  headers_policy_id="$(aws cloudfront create-response-headers-policy --response-headers-policy-config '{
    "Name": "'"$headers_policy_name"'",
    "Comment": "Preview CSP frame-ancestors",
    "SecurityHeadersConfig": {
      "ContentSecurityPolicy": {
        "Override": true,
        "ContentSecurityPolicy": "'"$csp_value_escaped"'"
      }
    }
  }' --query 'ResponseHeadersPolicy.Id' --output text)"
  echo "Created response headers policy: $headers_policy_id"
else
  echo "Using response headers policy: $headers_policy_id"
fi

oac_name="thedailyauction-oac"
oac_id="$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='$oac_name'].Id | [0]" \
  --output text)"

if [ -z "$oac_id" ] || [ "$oac_id" = "None" ]; then
  oac_id="$(aws cloudfront create-origin-access-control --origin-access-control-config '{
    "Name": "'"$oac_name"'",
    "Description": "OAC for The Daily Auction buckets",
    "SigningProtocol": "sigv4",
    "SigningBehavior": "always",
    "OriginAccessControlOriginType": "s3"
  }' --query 'OriginAccessControl.Id' --output text)"
  echo "Created OAC: $oac_id"
else
  echo "Using OAC: $oac_id"
fi

create_distribution() {
  local bucket="$1"
  local domain="$2"
  local cert_arn="$3"
  local label="$4"
  local response_headers_policy_id="${5:-}"

  local existing_id
  existing_id="$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Aliases.Items!=null && contains(Aliases.Items, '$domain')].Id | [0]" \
    --output text)"

  if [ -n "$existing_id" ] && [ "$existing_id" != "None" ]; then
    echo "Using existing distribution for $domain ($existing_id)" >&2
    if [ -n "$response_headers_policy_id" ]; then
      local dist_config_file
      local updated_config_file
      local etag
      dist_config_file="$(mktemp)"
      updated_config_file="$(mktemp)"
      aws cloudfront get-distribution-config --id "$existing_id" \
        --query 'DistributionConfig' --output json >"$dist_config_file"
      etag="$(aws cloudfront get-distribution-config --id "$existing_id" --query 'ETag' --output text)"

      python3 - "$dist_config_file" "$updated_config_file" "$response_headers_policy_id" <<'PY'
import json
import sys

src, dst, policy_id = sys.argv[1:4]
with open(src, "r", encoding="utf-8") as handle:
    config = json.load(handle)

cache_behavior = config.get("DefaultCacheBehavior", {})
cache_behavior["ResponseHeadersPolicyId"] = policy_id
config["DefaultCacheBehavior"] = cache_behavior

with open(dst, "w", encoding="utf-8") as handle:
    json.dump(config, handle)
PY

      aws cloudfront update-distribution --id "$existing_id" \
        --if-match "$etag" \
        --distribution-config "file://$updated_config_file" >/dev/null

      rm -f "$dist_config_file" "$updated_config_file"
      echo "$existing_id|$(aws cloudfront get-distribution --id "$existing_id" --query 'Distribution.DomainName' --output text)"
      return
    fi

    echo "$existing_id|$(aws cloudfront get-distribution --id "$existing_id" --query 'Distribution.DomainName' --output text)"
    return
  fi

  local origin_domain="${bucket}.s3.${AWS_REGION_LOWER}.amazonaws.com"
  local temp_file
  temp_file="$(mktemp)"

  local response_headers_block=""
  if [ -n "$response_headers_policy_id" ]; then
    response_headers_block=$',\n    "ResponseHeadersPolicyId": '"\"$response_headers_policy_id\""
  fi

  cat >"$temp_file" <<EOF
{
  "CallerReference": "$(date +%s)-$label",
  "Comment": "The Daily Auction $label",
  "Enabled": true,
  "DefaultRootObject": "index.html",
  "Aliases": {
    "Quantity": 1,
    "Items": ["$domain"]
  },
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "S3-$bucket",
      "DomainName": "$origin_domain",
      "OriginAccessControlId": "$oac_id",
      "S3OriginConfig": {
        "OriginAccessIdentity": ""
      }
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "S3-$bucket",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {
      "Quantity": 2,
      "Items": ["GET", "HEAD"],
      "CachedMethods": {
        "Quantity": 2,
        "Items": ["GET", "HEAD"]
      }
    },
    "Compress": true,
    "CachePolicyId": "$cache_policy_id"$response_headers_block
  },
  "ViewerCertificate": {
    "ACMCertificateArn": "$cert_arn",
    "SSLSupportMethod": "sni-only",
    "MinimumProtocolVersion": "TLSv1.2_2021"
  },
  "HttpVersion": "http2",
  "PriceClass": "PriceClass_100"
}
EOF

  local dist_id
  local dist_domain
  dist_id="$(aws cloudfront create-distribution --distribution-config "file://$temp_file" \
    --query 'Distribution.Id' --output text)"
  dist_domain="$(aws cloudfront get-distribution --id "$dist_id" \
    --query 'Distribution.DomainName' --output text)"

  rm -f "$temp_file"

  echo "$dist_id|$dist_domain"
}

preview_result="$(create_distribution "$S3_BUCKET_PREVIEW" "$CF_DOMAIN_PREVIEW" "$ACM_CERT_ARN" "preview" "$headers_policy_id")"
prod_result="$(create_distribution "$S3_BUCKET_PROD" "$CF_DOMAIN_PROD" "$ACM_CERT_ARN" "prod")"
editor_result="$(create_distribution "$S3_BUCKET_EDITOR" "$CF_DOMAIN_EDITOR" "$ACM_CERT_ARN" "editor")"

preview_id="${preview_result%%|*}"
preview_domain="${preview_result##*|}"
prod_id="${prod_result%%|*}"
prod_domain="${prod_result##*|}"
editor_id="${editor_result%%|*}"
editor_domain="${editor_result##*|}"

account_id="$(aws sts get-caller-identity --query Account --output text)"

apply_bucket_policy() {
  local bucket="$1"
  local dist_id="$2"
  if [ -z "$dist_id" ] || [ "$dist_id" = "None" ]; then
    echo "Missing distribution id for bucket policy on $bucket" >&2
    exit 1
  fi
  local dist_arn="arn:aws:cloudfront::$account_id:distribution/$dist_id"
  local policy_file
  policy_file="$(mktemp)"
  cat >"$policy_file" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowCloudFrontReadOnly",
      "Effect": "Allow",
      "Principal": { "Service": "cloudfront.amazonaws.com" },
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::$bucket/*",
      "Condition": {
        "StringEquals": { "AWS:SourceArn": "$dist_arn" }
      }
    }
  ]
}
EOF
  if [ "${DEBUG_POLICY:-}" = "1" ]; then
    echo "Bucket policy for $bucket:" >&2
    cat "$policy_file" >&2
  fi
  aws s3api put-bucket-policy --bucket "$bucket" --policy "file://$policy_file" >/dev/null
  rm -f "$policy_file"
}

apply_bucket_policy "$S3_BUCKET_PREVIEW" "$preview_id"
apply_bucket_policy "$S3_BUCKET_PROD" "$prod_id"
apply_bucket_policy "$S3_BUCKET_EDITOR" "$editor_id"

cat <<EOF

CloudFront distributions created:
- preview: $preview_domain (id: $preview_id)
- prod:    $prod_domain (id: $prod_id)
- editor:  $editor_domain (id: $editor_id)

DNS records to add:
- $CF_DOMAIN_PREVIEW CNAME $preview_domain
- $CF_DOMAIN_PROD CNAME $prod_domain
- $CF_DOMAIN_EDITOR CNAME $editor_domain
EOF
