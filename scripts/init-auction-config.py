import os
import sys
from pathlib import Path

from solana.publickey import PublicKey
from solana.rpc.api import Client

JOB_DIR = Path(__file__).resolve().parent.parent / "jobs" / "auction_settlement"
sys.path.insert(0, str(JOB_DIR))

from auction_client import instruction_init_config, parse_keypair, send_transaction


def main() -> None:
    program_id = PublicKey(os.environ["AUCTION_PROGRAM_ID"])
    recipient_pubkey = PublicKey(os.environ["AUCTION_RECIPIENT_PUBKEY"])
    loser_fee_lamports = int(os.environ.get("LOSER_FEE_LAMPORTS", "100000"))
    min_increment_lamports = int(os.environ.get("MIN_INCREMENT_LAMPORTS", "100000000"))
    signer = parse_keypair(os.environ["CRANKER_PRIVATE_KEY"])
    rpc_url = os.environ.get("RPC_URL", "https://api.devnet.solana.com")

    client = Client(rpc_url)
    instruction = instruction_init_config(
        program_id,
        signer.public_key,
        recipient_pubkey,
        loser_fee_lamports,
        min_increment_lamports,
    )
    send_transaction(client, instruction, signer)
    print("Config initialized.")


if __name__ == "__main__":
    main()
