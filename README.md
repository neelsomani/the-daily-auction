# The Daily Auction

This repo is a monorepo for the Daily Auction system.

## Core Infrastructure

### Setup (AWS + local)

1. Copy `.env.template` to `.env` and fill in the AWS values (buckets, domains, region).
2. Run the AWS setup script:

```sh
./scripts/setup-aws.sh
```

3. Start local services:

```sh
docker compose up --build
```

Codex listens on `http://localhost:8080`. Deploy listens on the internal Docker network only.

Production hosting note:
- Deploy Codex + Deploy on a single EC2 instance with Docker Compose (Codex public, Deploy internal-only). Other setups are possible, but EC2 is the assumed default.

### DNS setup

After `scripts/setup-aws.sh`, it prints the CloudFront domains. Create DNS records:

- `preview.thedailyauction.app` → CNAME to the preview CloudFront domain
- `www.thedailyauction.app` → CNAME to the prod CloudFront domain
- `editor.thedailyauction.com` → CNAME to the editor CloudFront domain
- `api.thedailyauction.com` should point to the Codex server (public). Keep Deploy internal-only.

### Editor deployment

Upload the editor UI to its S3 bucket:

```sh
./scripts/publish-editor.sh
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
