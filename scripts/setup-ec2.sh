#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

DOMAIN=${DOMAIN:-api.thedailyauction.com}
APP_DIR=${APP_DIR:-$(pwd)}

apt-get update
apt-get install -y git curl ca-certificates

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

if ! command -v docker compose >/dev/null 2>&1; then
  apt-get install -y docker-compose-plugin
fi

if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | tee /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

if [ ! -f "$APP_DIR/docker-compose.yml" ]; then
  echo "Run this script from the repo root (missing docker-compose.yml)." >&2
  exit 1
fi

cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
  reverse_proxy 127.0.0.1:8080
}
EOF

systemctl enable caddy
systemctl restart caddy

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.template" "$APP_DIR/.env"
  echo "Created $APP_DIR/.env from template. Fill it in before running." >&2
fi

cat <<EOF

Setup complete.
Next steps:
  cd $APP_DIR
  vim .env
  docker compose up -d --build
EOF
