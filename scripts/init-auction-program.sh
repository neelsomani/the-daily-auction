#!/bin/bash

set -euo pipefail

PROGRAM_NAME="auction"
KEYPAIR_PATH="target/deploy/${PROGRAM_NAME}-keypair.json"

mkdir -p "target/deploy"

if [ ! -f "$KEYPAIR_PATH" ]; then
  solana-keygen new -o "$KEYPAIR_PATH" --no-bip39-passphrase -f
fi

PROGRAM_ID=$(solana-keygen pubkey "$KEYPAIR_PATH")

python3 - <<PY
from pathlib import Path

program_id = "${PROGRAM_ID}"

anchor_toml = Path("Anchor.toml")
data = anchor_toml.read_text()
data = data.replace(
    'auction = "Fg6PaFpoGXkYsidMpWTK6W2BeZ7FEfcYkg476zPFsLnS"',
    f'auction = "{program_id}"',
)
anchor_toml.write_text(data)

lib_rs = Path("programs/auction/src/lib.rs")
lib_data = lib_rs.read_text()
lib_data = lib_data.replace(
    'declare_id!("Fg6PaFpoGXkYsidMpWTK6W2BeZ7FEfcYkg476zPFsLnS");',
    f'declare_id!("{program_id}");',
)
lib_rs.write_text(lib_data)
PY

echo "Program ID set to: $PROGRAM_ID"
