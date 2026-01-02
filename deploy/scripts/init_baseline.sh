#!/bin/sh
set -eu

TEMPLATE_DIR=${TEMPLATE_DIR:-/app/template}
BASELINE_DIR=${BASELINE_DIR:-/app/baseline}
ENABLE_GIT_HISTORY=${ENABLE_GIT_HISTORY:-false}

if [ ! -d "$TEMPLATE_DIR" ]; then
  echo "template directory not found" >&2
  exit 1
fi

mkdir -p "$BASELINE_DIR"

if [ -z "$(ls -A "$BASELINE_DIR")" ]; then
  rsync -a "$TEMPLATE_DIR/" "$BASELINE_DIR/"
  if [ "$ENABLE_GIT_HISTORY" = "true" ]; then
    git -C "$BASELINE_DIR" init
    git -C "$BASELINE_DIR" add -A
    git -C "$BASELINE_DIR" commit -m "Baseline init" || true
  fi
fi
