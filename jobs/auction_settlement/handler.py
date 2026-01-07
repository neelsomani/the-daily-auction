import math
import os
import time
from typing import List

from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.rpc.core import RPCException
from solana.rpc.types import TxOpts

from auction_client import (
    SECONDS_PER_DAY,
    AuctionDay,
    BidReceipt,
    fetch_auction_day,
    fetch_bid_receipts,
    fetch_config,
    instruction_init_day,
    instruction_refund_batch,
    instruction_settle_day,
    parse_keypair,
    pda_auction_day,
    pda_config,
    pda_vault,
    send_transaction,
)


ERROR_CODES = {
    "AlreadyFinalized": 6003,
    "TooEarly": 6009,
}


def extract_custom_error_code(err: Exception) -> int:
    if hasattr(err, "args") and err.args:
        for arg in err.args:
            if hasattr(arg, "data") and hasattr(arg.data, "err"):
                err_data = arg.data.err
                try:
                    custom = err_data.value.custom
                    return int(custom)
                except Exception:
                    pass
    texts = [str(err), repr(err)]
    if hasattr(err, "args"):
        texts.extend([str(arg) for arg in err.args if arg is not None])
    marker = "custom program error: 0x"
    for text in texts:
        if marker in text:
            try:
                hex_str = text.split(marker)[1].split()[0]
                return int(hex_str, 16)
            except ValueError:
                continue
        if "InstructionErrorCustom(" in text:
            try:
                num = text.split("InstructionErrorCustom(")[1].split(")")[0]
                return int(num)
            except ValueError:
                continue
    return -1


def is_error(err: Exception, name: str) -> bool:
    code = extract_custom_error_code(err)
    if code == ERROR_CODES.get(name, -2):
        return True
    return name in str(err)


def current_day_index() -> int:
    return int(time.time()) // SECONDS_PER_DAY


def chunked(items: List[PublicKey], size: int) -> List[List[PublicKey]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def log(msg: str) -> None:
    print(msg, flush=True)


def maybe_init_day(client: Client, program_id: PublicKey, signer, day_index: int) -> None:
    instruction = instruction_init_day(program_id, signer.public_key, day_index)
    try:
        send_transaction(client, instruction, signer)
        log(f"init_day: ensured day {day_index}")
    except RPCException as err:
        code = extract_custom_error_code(err)
        if code > 0:
            log(f"init_day: program error {code} for day {day_index}")
        else:
            log(f"init_day: RPC error for day {day_index}: {err}")


def settle_with_retry(
    client: Client,
    program_id: PublicKey,
    signer,
    day_index: int,
    retry_window_seconds: int,
    retry_interval_seconds: int,
) -> None:
    config_key, _ = pda_config(program_id)
    auction_day_key, _ = pda_auction_day(program_id, day_index)
    vault_key, _ = pda_vault(program_id, auction_day_key)
    config = fetch_config(client, program_id)
    if not config:
        raise RuntimeError("Config account not found")

    instruction = instruction_settle_day(
        program_id,
        config_key,
        auction_day_key,
        vault_key,
        config.recipient_pubkey,
        day_index,
    )

    start = time.time()
    attempt = 0
    while True:
        attempt += 1
        try:
            send_transaction(client, instruction, signer)
            log(f"settle_day: success for day {day_index}")
            return
        except RPCException as err:
            if is_error(err, "AlreadyFinalized"):
                log(f"settle_day: already finalized for day {day_index}")
                return
            if is_error(err, "TooEarly"):
                if time.time() - start > retry_window_seconds:
                    raise RuntimeError("settle_day: too early beyond retry window") from err
                log("settle_day: too early, retrying")
                time.sleep(retry_interval_seconds)
                continue
            if time.time() - start > retry_window_seconds:
                raise
            backoff = min(retry_interval_seconds * attempt, 60)
            log(f"settle_day: retrying after error: {err}")
            time.sleep(backoff)


def refund_losers(
    client: Client,
    program_id: PublicKey,
    signer,
    day_index: int,
    max_batch_size: int,
    max_runtime_seconds: int,
) -> None:
    auction_day_key, _ = pda_auction_day(program_id, day_index)
    auction_day = fetch_auction_day(client, program_id, day_index)
    if not auction_day:
        log(f"refunds: auction_day missing for day {day_index}")
        return

    if not auction_day.finalized:
        log(f"refunds: auction_day not finalized for day {day_index}")
        return

    if auction_day.refund_count_total > 0 and auction_day.refund_count_completed >= auction_day.refund_count_total:
        log("refunds: already completed")
        return

    receipts = fetch_bid_receipts(client, program_id, auction_day_key)
    losers: List[PublicKey] = []
    for _receipt_key, receipt in receipts:
        if receipt.refunded:
            continue
        if receipt.bidder == auction_day.winner:
            continue
        losers.append(receipt.bidder)

    if not losers:
        log("refunds: no losers to refund")
        return

    config_key, _ = pda_config(program_id)
    vault_key, _ = pda_vault(program_id, auction_day_key)
    batches = chunked(losers, max_batch_size)

    start = time.time()
    total_refunded = 0
    for batch in batches:
        if time.time() - start > max_runtime_seconds:
            log("refunds: max runtime reached, stopping")
            break

        instruction = instruction_refund_batch(
            program_id,
            config_key,
            auction_day_key,
            vault_key,
            signer.public_key,
            day_index,
            batch,
        )
        try:
            send_transaction(client, instruction, signer)
            total_refunded += len(batch)
            log(f"refunds: processed batch of {len(batch)}")
        except RPCException as err:
            log(f"refunds: batch failed: {err}")
            continue

    log(f"refunds: processed {total_refunded} bidders")


def handler(event=None, context=None):
    rpc_url = os.environ.get("RPC_URL", "https://api.devnet.solana.com")
    program_id = PublicKey(os.environ["AUCTION_PROGRAM_ID"])
    signer = parse_keypair(os.environ["CRANKER_PRIVATE_KEY"])

    retry_window_seconds = int(os.environ.get("RETRY_WINDOW_SECONDS", "1800"))
    retry_interval_seconds = int(os.environ.get("RETRY_INTERVAL_SECONDS", "45"))
    max_batch_size = int(os.environ.get("MAX_BATCH_SIZE", "20"))
    max_runtime_seconds = int(os.environ.get("MAX_RUNTIME_SECONDS", "780"))

    client = Client(rpc_url)

    target_day_index = current_day_index() - 1
    log(f"starting settlement for day {target_day_index}")

    if not fetch_auction_day(client, program_id, target_day_index):
        maybe_init_day(client, program_id, signer, target_day_index)

    settle_with_retry(
        client,
        program_id,
        signer,
        target_day_index,
        retry_window_seconds,
        retry_interval_seconds,
    )

    refund_losers(
        client,
        program_id,
        signer,
        target_day_index,
        max_batch_size,
        max_runtime_seconds,
    )

    return {"status": "ok", "day_index": target_day_index}


if __name__ == "__main__":
    handler()
