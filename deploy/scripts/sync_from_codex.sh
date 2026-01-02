#!/bin/sh
set -eu

CODEX_SITE_DIR=${CODEX_SITE_DIR:-/app/codex_site}
TARGET_DIR=${1:-}

if [ -z "$TARGET_DIR" ]; then
  echo "target dir required" >&2
  exit 1
fi

if [ ! -d "$CODEX_SITE_DIR" ]; then
  echo "codex site dir not found" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"
rsync -a --delete "$CODEX_SITE_DIR/" "$TARGET_DIR/"
