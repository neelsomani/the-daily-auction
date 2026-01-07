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

5. Deploy the Solana auction program (Anchor)

Location: `programs/auction`.

Initialize the program ID (generates `target/deploy/auction-keypair.json` if missing
and updates `Anchor.toml` + `programs/auction/src/lib.rs`):

```sh
./scripts/init-auction-program.sh
```

Set your program ID in `.env` (needed for the nightly job below).

Build with:

```sh
cd programs/auction
anchor build
```

Create a deploy wallet (and optionally reuse it as the cranker wallet):

```sh
solana-keygen new -o ~/.config/solana/auction-operator.json
```

Then set `CRANKER_PRIVATE_KEY` and `AUCTION_RECIPIENT_PUBKEY` in your `.env`
(use an absolute path; `~` won’t expand when sourced):

```sh
CRANKER_PRIVATE_KEY="$(cat /Users/YOUR_USER/.config/solana/auction-operator.json)"
AUCTION_RECIPIENT_PUBKEY=9ajQ4mq9cxTqTDT8b2LyQGnooAbwgFdPn72xkWdSdj5S
```

RPC configuration:
- Anchor uses the cluster in `Anchor.toml` under `[provider]` (e.g. `Devnet` or `Mainnet`).
- Update the `wallet` field in `Anchor.toml` to point at your deploy keypair.
- Make sure you point at either devnet or mainnet RPC depending on what you want to do.

Devnet SOL for deploy + cranker wallets:

```sh
solana config set --url https://api.devnet.solana.com
solana airdrop 1 ~/.config/solana/auction-operator.json
solana airdrop 1 /path/to/cranker-keypair.json
# Request more SOL at https://faucet.solana.com/
```

If you use the same keypair for deploy and cranker, a single airdrop to that keypair is enough.

Deploy with:

```sh
cd programs/auction
anchor deploy
```

The program implements the spec in `docs/solana-auction-spec.md`.

6. Deploy the nightly settlement job to AWS Lambda

Location: `jobs/auction_settlement`.

The Lambda entrypoint is `jobs/auction_settlement/handler.py`.

Initialize the config (one-time, before running the nightly job):

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r jobs/auction_settlement/requirements.txt
set -a && source .env && set +a
python3 scripts/init-auction-config.py
```

Test the lambda function locally:

```sh
python3 jobs/auction_settlement/handler.py
```

Deployment script:

```sh
./scripts/deploy-auction-lambda.sh
```

Required env vars:
- `AUCTION_PROGRAM_ID`
- `CRANKER_PRIVATE_KEY` (base58 or JSON array secret key - must be in quotes)

Optional env vars:
- `RPC_URL` (default devnet - match the Anchor deployment above)
- `MAX_BATCH_SIZE` (default 20)
- `RETRY_WINDOW_SECONDS` (default 1800)
- `RETRY_INTERVAL_SECONDS` (default 45)
- `MAX_RUNTIME_SECONDS` (default 780)

7. Test the public auction website (Next.js)

Location: `public-auction`.

Public site env vars (set in `public-auction/.env.local`):
- `NEXT_PUBLIC_AUCTION_PROGRAM_ID`
- `NEXT_PUBLIC_RPC_URL` (default devnet)
- `NEXT_PUBLIC_DISABLE_SITE`

You can start from `public-auction/.env.local.example`.

Node.js version >= v18.17.0 is required.

```sh
cd public-auction
npm install
npm run dev
```

### Required AWS permissions (for setup + Lambda deploy scripts)

Recommended: If you run `scripts/setup-aws.sh` and `scripts/deploy-auction-lambda.sh` with the same IAM user,
attach a single inline policy that includes S3 + CloudFront + ACM + IAM + Lambda + EventBridge permissions.
Adjust bucket names and function/role names if you changed them.

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
    },
    {
      "Sid": "IamRoleManagement",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:GetRole",
        "iam:AttachRolePolicy",
        "iam:PassRole"
      ],
      "Resource": "arn:aws:iam::*:role/AuctionLambdaExecutionRole"
    },
    {
      "Sid": "LambdaManagement",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction",
        "lambda:GetFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:AddPermission"
      ],
      "Resource": "arn:aws:lambda:*:*:function:auction-settlement-daily"
    },
    {
      "Sid": "EventBridgeManagement",
      "Effect": "Allow",
      "Action": [
        "events:PutRule",
        "events:PutTargets"
      ],
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
- `programs/auction`: Anchor-based Solana auction program.
- `jobs/auction_settlement`: Python AWS Lambda job for nightly settlement and refunds.
- `public-auction`: Next.js public auction wrapper site.

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

### Notes

- `codex/scripts/edit.sh` is a placeholder; replace it with Codex CLI integration.
- `deploy/scripts/*` use `aws s3 sync` and require AWS credentials.
