# The Daily Auction

This repo is a monorepo for the Daily Auction system.

## Core Infrastructure

### Setup (AWS + local)

1. Copy `.env.template` to `.env` and fill in the AWS values (buckets, domains, region).
2. Run the AWS setup script:

```sh
./scripts/setup-aws.sh
```

Add the DNS validation CNAMEs in your DNS provider, wait for the cert to be ISSUED, set `ACM_CERT_ARN` in `.env`, then re-run `./scripts/setup-aws.sh`.

After `scripts/setup-aws.sh`, it prints the CloudFront domains. Create DNS records:

- `preview.thedailyauction.app` → CNAME to the preview CloudFront domain
- `www.thedailyauction.app` → CNAME to the prod CloudFront domain
- `editor.thedailyauction.com` → CNAME to the editor CloudFront domain

3. Start local services:

```sh
docker compose up --build
```

Codex listens on `http://localhost:8080`. Deploy listens on the internal Docker network only.

4. Run the editor locally:

```
cd editor
python3 -m http.server 8000
```

Production hosting note:
- Deploy Codex + Deploy on a single EC2 instance with Docker Compose (Codex public, Deploy internal-only). Other setups are possible, but EC2 is the assumed default.
- Run `./scripts/publish-editor.sh` to deploy the editor remotely
- `api.thedailyauction.com` should point to the Codex server (public). Keep Deploy internal-only.

### Required AWS permissions (for setup script)

If you run `scripts/setup-aws.sh` with an IAM user, it needs S3 + CloudFront + ACM permissions.
You can attach this as an inline policy (adjust bucket names if you changed them):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AcmCert",
      "Effect": "Allow",
      "Action": [
        "acm:RequestCertificate",
        "acm:DeleteCertificate",
        "acm:DescribeCertificate",
        "acm:ListCertificates"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudFront",
      "Effect": "Allow",
      "Action": [
        "cloudfront:CreateDistribution",
        "cloudfront:GetDistribution",
        "cloudfront:ListDistributions",
        "cloudfront:CreateCachePolicy",
        "cloudfront:ListCachePolicies",
        "cloudfront:CreateOriginAccessControl",
        "cloudfront:ListOriginAccessControls"
      ],
      "Resource": "*"
    },
    {
      "Sid": "S3",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:PutBucketPolicy",
        "s3:PutBucketPublicAccessBlock",
        "s3:PutBucketOwnershipControls",
        "s3:ListBucket",
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::thedailyauction-preview",
        "arn:aws:s3:::thedailyauction-prod",
        "arn:aws:s3:::thedailyauction-editor",
        "arn:aws:s3:::thedailyauction-preview/*",
        "arn:aws:s3:::thedailyauction-prod/*",
        "arn:aws:s3:::thedailyauction-editor/*"
      ]
    },
    {
      "Sid": "StsIdentity",
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity"],
      "Resource": "*"
    }
  ]
}
```

### Structure

- `codex`: Control plane for edit/deploy/nuke and the static `site/` working tree.
- `deploy`: Internal-only deploy service that owns the baseline and publishes to S3.
- `site-template`: Baseline static site template for nukes and initialization.
- `editor`: Static wallet-connected editor UI for local testing.

### Request signing

Requests to `/edit`, `/deploy`, and `/nuke` require these headers:

- `X-Wallet`: base58 Solana public key (currently must match `MASTER_WALLET`)
- `X-Nonce`: unique per request
- `X-Expiry`: unix epoch seconds
- `X-Signature`: base58 signature of the message

Message format (bytes):

```
{wallet}:{nonce}:{expiry}:{path}:{sha256(body)}
```

Signature verification uses the wallet public key (ed25519) only.

### Editor activity feed

The Codex service exposes `GET /history?limit=200` for the editor UI. It returns
recent activity entries stored in `site/.codex_history.jsonl`.

### Notes

- `codex/scripts/edit.sh` is a placeholder; replace it with Codex CLI integration.
- `deploy/scripts/*` use `aws s3 sync` and require AWS credentials.
