#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

DOMAIN=${DOMAIN:-api.thedailyauction.com}
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
APP_DIR=$(cd "$SCRIPT_DIR/.." && pwd)

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y git curl ca-certificates
elif command -v yum >/dev/null 2>&1; then
  yum install -y git ca-certificates
else
  echo "No supported package manager (apt-get/yum)." >&2
  exit 1
fi

download_binary() {
  local url="$1"
  local dest="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
  else
    if command -v yum >/dev/null 2>&1; then
      yum install -y wget
    fi
    wget -qO "$dest" "$url"
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  if command -v yum >/dev/null 2>&1; then
    yum install -y docker
    systemctl enable --now docker
  else
    curl -fsSL https://get.docker.com | sh
  fi
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Installing Docker Compose v2..."
  install -d /usr/local/lib/docker/cli-plugins /usr/libexec/docker/cli-plugins
  download_binary \
    https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64 \
    /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  cp /usr/local/lib/docker/cli-plugins/docker-compose \
    /usr/libexec/docker/cli-plugins/docker-compose || true
fi

docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose install failed" >&2
  exit 1
}

if ! command -v caddy >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
  else
    curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=amd64" -o /usr/local/bin/caddy
    chmod +x /usr/local/bin/caddy
    if ! getent group caddy >/dev/null 2>&1; then
      groupadd --system caddy
    fi
    if ! getent passwd caddy >/dev/null 2>&1; then
      useradd --system --gid caddy --create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy
    fi
    cat >/etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy
After=network.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
TimeoutStopSec=5s
LimitNOFILE=1048576
LimitNPROC=512
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
  fi
fi

if [ ! -f "$APP_DIR/docker-compose.yml" ]; then
  echo "Run this script from the repo root (missing docker-compose.yml)." >&2
  exit 1
fi

mkdir -p /etc/caddy
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
  sudo vim .env
  sudo docker compose up -d --build
EOF
