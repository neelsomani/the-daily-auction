#!/bin/sh
set -eu

SOURCE_DIR=${1:-}
DEST_DIR=${2:-/app/site}

if [ -z "$SOURCE_DIR" ]; then
  echo "source_dir required" >&2
  exit 1
fi

if [ ! -d "$SOURCE_DIR" ]; then
  echo "source_dir not found" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"

rsync -a --delete "$SOURCE_DIR/" "$DEST_DIR/"
