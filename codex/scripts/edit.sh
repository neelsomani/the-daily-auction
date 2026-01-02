#!/bin/sh
set -eu

INSTRUCTION=${1:-}
SITE_DIR=${2:-/app/site}

if [ -z "$INSTRUCTION" ]; then
  echo "instruction required" >&2
  exit 1
fi

mkdir -p "$SITE_DIR"
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "[$NOW] $INSTRUCTION" >> "$SITE_DIR/REQUESTS.log"

INDEX_FILE="$SITE_DIR/index.html"

escape_html() {
  printf "%s" "$1" | sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' \
    -e "s/'/\&#39;/g"
}

escaped_instruction=$(escape_html "$INSTRUCTION")

hash_input=$(printf "%s" "$INSTRUCTION")
if command -v md5sum >/dev/null 2>&1; then
  hash_value=$(printf "%s" "$hash_input" | md5sum | awk '{print $1}')
elif command -v md5 >/dev/null 2>&1; then
  hash_value=$(printf "%s" "$hash_input" | md5 | awk '{print $NF}')
else
  hash_value=$(printf "%s" "$hash_input" | openssl dgst -md5 | awk '{print $NF}')
fi

snippet="      <p class=\"update-hash\">Latest edit hash: ${hash_value}</p>"

if [ -f "$INDEX_FILE" ]; then
  tmp_file="$(mktemp)"
  awk -v snippet="$snippet" '
    /class="update-hash"/ { print snippet; done=1; next }
    /<\/main>/ && !done { print snippet; done=1 }
    { print }
    END { if (!done) print snippet }
  ' "$INDEX_FILE" > "$tmp_file"
  mv "$tmp_file" "$INDEX_FILE"
else
  cat > "$INDEX_FILE" <<EOF
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>The Daily Auction</title>
  </head>
  <body>
    <main>
${snippet}
    </main>
  </body>
</html>
EOF
fi
