import hashlib
import struct
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from base58 import b58decode
from borsh_construct import Bool, CStruct, I64, U8, U32, U64
from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.rpc.types import MemcmpOpts
from solana.transaction import AccountMeta, Transaction, TransactionInstruction
from solana.keypair import Keypair


SECONDS_PER_DAY = 86_400
INIT_DAY_MAX_AHEAD_DAYS = 2

CONFIG_DISCRIMINATOR = hashlib.sha256(b"account:Config").digest()[:8]
AUCTION_DAY_DISCRIMINATOR = hashlib.sha256(b"account:AuctionDay").digest()[:8]
BID_RECEIPT_DISCRIMINATOR = hashlib.sha256(b"account:BidReceipt").digest()[:8]


CONFIG_LAYOUT = CStruct(
    "recipient_pubkey" / U8[32],
    "loser_fee_lamports" / U64,
    "min_increment_lamports" / U64,
    "bump" / U8,
)

AUCTION_DAY_LAYOUT = CStruct(
    "day_index" / I64,
    "finalized" / Bool,
    "winner" / U8[32],
    "highest_bid" / U64,
    "bidder_count" / U32,
    "refund_count_total" / U32,
    "refund_count_completed" / U32,
    "total_bid_lamports" / U64,
    "refund_pool_remaining" / U64,
    "fee_pool_remaining" / U64,
    "vault_bump" / U8,
)

BID_RECEIPT_LAYOUT = CStruct(
    "auction_day" / U8[32],
    "bidder" / U8[32],
    "amount" / U64,
    "refunded" / Bool,
)


@dataclass
class Config:
    recipient_pubkey: PublicKey
    loser_fee_lamports: int
    min_increment_lamports: int
    bump: int


@dataclass
class AuctionDay:
    day_index: int
    finalized: bool
    winner: PublicKey
    highest_bid: int
    bidder_count: int
    refund_count_total: int
    refund_count_completed: int
    total_bid_lamports: int
    refund_pool_remaining: int
    fee_pool_remaining: int
    vault_bump: int


@dataclass
class BidReceipt:
    auction_day: PublicKey
    bidder: PublicKey
    amount: int
    refunded: bool


def parse_keypair(raw: str) -> Keypair:
    raw = raw.strip()
    if raw.startswith("["):
        secret = bytes(__import__("json").loads(raw))
        return Keypair.from_secret_key(secret)
    return Keypair.from_secret_key(b58decode(raw))


def anchor_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def pda_config(program_id: PublicKey) -> Tuple[PublicKey, int]:
    return PublicKey.find_program_address([b"config"], program_id)


def pda_auction_day(program_id: PublicKey, day_index: int) -> Tuple[PublicKey, int]:
    return PublicKey.find_program_address(
        [b"auction_day", day_index.to_bytes(8, "little", signed=True)], program_id
    )


def pda_vault(program_id: PublicKey, auction_day: PublicKey) -> Tuple[PublicKey, int]:
    return PublicKey.find_program_address([b"vault", bytes(auction_day)], program_id)


def pda_bid_receipt(
    program_id: PublicKey, auction_day: PublicKey, bidder: PublicKey
) -> Tuple[PublicKey, int]:
    return PublicKey.find_program_address(
        [b"bid_receipt", bytes(auction_day), bytes(bidder)], program_id
    )


def decode_config(data: bytes) -> Config:
    if data[:8] != CONFIG_DISCRIMINATOR:
        raise ValueError("Invalid Config discriminator")
    parsed = CONFIG_LAYOUT.parse(data[8:])
    return Config(
        recipient_pubkey=PublicKey(parsed.recipient_pubkey),
        loser_fee_lamports=parsed.loser_fee_lamports,
        min_increment_lamports=parsed.min_increment_lamports,
        bump=parsed.bump,
    )


def decode_auction_day(data: bytes) -> AuctionDay:
    if data[:8] != AUCTION_DAY_DISCRIMINATOR:
        raise ValueError("Invalid AuctionDay discriminator")
    parsed = AUCTION_DAY_LAYOUT.parse(data[8:])
    return AuctionDay(
        day_index=parsed.day_index,
        finalized=parsed.finalized,
        winner=PublicKey(parsed.winner),
        highest_bid=parsed.highest_bid,
        bidder_count=parsed.bidder_count,
        refund_count_total=parsed.refund_count_total,
        refund_count_completed=parsed.refund_count_completed,
        total_bid_lamports=parsed.total_bid_lamports,
        refund_pool_remaining=parsed.refund_pool_remaining,
        fee_pool_remaining=parsed.fee_pool_remaining,
        vault_bump=parsed.vault_bump,
    )


def decode_bid_receipt(data: bytes) -> BidReceipt:
    if data[:8] != BID_RECEIPT_DISCRIMINATOR:
        raise ValueError("Invalid BidReceipt discriminator")
    parsed = BID_RECEIPT_LAYOUT.parse(data[8:])
    return BidReceipt(
        auction_day=PublicKey(parsed.auction_day),
        bidder=PublicKey(parsed.bidder),
        amount=parsed.amount,
        refunded=parsed.refunded,
    )


def encode_i64(value: int) -> bytes:
    return struct.pack("<q", value)


def encode_u32(value: int) -> bytes:
    return struct.pack("<I", value)


def encode_pubkey(pubkey: PublicKey) -> bytes:
    return bytes(pubkey)


def encode_vec_pubkeys(pubkeys: Iterable[PublicKey]) -> bytes:
    pubkeys = list(pubkeys)
    data = encode_u32(len(pubkeys))
    for key in pubkeys:
        data += encode_pubkey(key)
    return data


def instruction_init_day(program_id: PublicKey, payer: PublicKey, day_index: int) -> TransactionInstruction:
    data = anchor_discriminator("init_day") + encode_i64(day_index)
    auction_day, _ = pda_auction_day(program_id, day_index)
    vault, _ = pda_vault(program_id, auction_day)
    keys = [
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(auction_day, is_signer=False, is_writable=True),
        AccountMeta(vault, is_signer=False, is_writable=True),
        AccountMeta(PublicKey("11111111111111111111111111111111"), is_signer=False, is_writable=False),
    ]
    return TransactionInstruction(program_id=program_id, data=data, keys=keys)


def instruction_init_config(
    program_id: PublicKey,
    payer: PublicKey,
    recipient_pubkey: PublicKey,
    loser_fee_lamports: int,
    min_increment_lamports: int,
) -> TransactionInstruction:
    data = (
        anchor_discriminator("init_config")
        + encode_pubkey(recipient_pubkey)
        + struct.pack("<Q", int(loser_fee_lamports))
        + struct.pack("<Q", int(min_increment_lamports))
    )
    config_key, _ = pda_config(program_id)
    keys = [
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(config_key, is_signer=False, is_writable=True),
        AccountMeta(PublicKey("11111111111111111111111111111111"), is_signer=False, is_writable=False),
    ]
    return TransactionInstruction(program_id=program_id, data=data, keys=keys)


def instruction_settle_day(
    program_id: PublicKey,
    config: PublicKey,
    auction_day: PublicKey,
    vault: PublicKey,
    recipient: PublicKey,
    day_index: int,
) -> TransactionInstruction:
    data = anchor_discriminator("settle_day") + encode_i64(day_index)
    keys = [
        AccountMeta(config, is_signer=False, is_writable=False),
        AccountMeta(auction_day, is_signer=False, is_writable=True),
        AccountMeta(vault, is_signer=False, is_writable=True),
        AccountMeta(recipient, is_signer=False, is_writable=True),
        AccountMeta(PublicKey("11111111111111111111111111111111"), is_signer=False, is_writable=False),
    ]
    return TransactionInstruction(program_id=program_id, data=data, keys=keys)


def instruction_refund_batch(
    program_id: PublicKey,
    config: PublicKey,
    auction_day: PublicKey,
    vault: PublicKey,
    cranker: PublicKey,
    day_index: int,
    bidders: List[PublicKey],
) -> TransactionInstruction:
    data = anchor_discriminator("refund_batch") + encode_i64(day_index) + encode_vec_pubkeys(bidders)
    keys = [
        AccountMeta(config, is_signer=False, is_writable=False),
        AccountMeta(auction_day, is_signer=False, is_writable=True),
        AccountMeta(vault, is_signer=False, is_writable=True),
        AccountMeta(cranker, is_signer=True, is_writable=True),
        AccountMeta(PublicKey("11111111111111111111111111111111"), is_signer=False, is_writable=False),
    ]
    for bidder in bidders:
        bid_receipt, _ = pda_bid_receipt(program_id, auction_day, bidder)
        keys.append(AccountMeta(bid_receipt, is_signer=False, is_writable=True))
        keys.append(AccountMeta(bidder, is_signer=False, is_writable=True))
    return TransactionInstruction(program_id=program_id, data=data, keys=keys)


def _extract_blockhash(resp) -> str:
    if isinstance(resp, dict):
        return resp["result"]["value"]["blockhash"]
    if hasattr(resp, "value"):
        return str(resp.value.blockhash)
    if hasattr(resp, "result") and hasattr(resp.result, "value"):
        return str(resp.result.value.blockhash)
    raise ValueError("Unable to extract blockhash")


def send_transaction(
    client: Client, instruction: TransactionInstruction, signer: Keypair
) -> dict:
    blockhash_resp = client.get_latest_blockhash()
    blockhash = _extract_blockhash(blockhash_resp)
    tx = Transaction(fee_payer=signer.public_key, recent_blockhash=blockhash)
    tx.add(instruction)
    tx.sign(signer)
    resp = client.send_raw_transaction(tx.serialize())
    signature = None
    if isinstance(resp, dict):
        signature = resp.get("result")
    elif hasattr(resp, "value"):
        signature = str(resp.value)
    if signature:
        client.confirm_transaction(signature)
    return resp


def _extract_value(resp):
    if isinstance(resp, dict):
        return resp.get("result", {}).get("value")
    if hasattr(resp, "value"):
        return resp.value
    if hasattr(resp, "result") and hasattr(resp.result, "value"):
        return resp.result.value
    return None


def _decode_account_data(value) -> Optional[bytes]:
    if value is None:
        return None
    data_field = getattr(value, "data", None)
    if data_field is None and isinstance(value, dict):
        data_field = value.get("data")
    if data_field is None:
        return None
    if isinstance(data_field, bytes):
        return data_field
    if isinstance(data_field, (list, tuple)):
        if not data_field:
            return None
        data_field = data_field[0]
    if isinstance(data_field, str):
        return bytes(__import__("base64").b64decode(data_field))
    return None


def fetch_account(client: Client, pubkey: PublicKey) -> Optional[bytes]:
    resp = client.get_account_info(pubkey)
    value = _extract_value(resp)
    return _decode_account_data(value)


def fetch_config(client: Client, program_id: PublicKey) -> Optional[Config]:
    config_key, _ = pda_config(program_id)
    data = fetch_account(client, config_key)
    if not data:
        return None
    return decode_config(data)


def fetch_auction_day(
    client: Client, program_id: PublicKey, day_index: int
) -> Optional[AuctionDay]:
    auction_day, _ = pda_auction_day(program_id, day_index)
    data = fetch_account(client, auction_day)
    if not data:
        return None
    return decode_auction_day(data)


def fetch_bid_receipts(
    client: Client, program_id: PublicKey, auction_day: PublicKey
) -> List[Tuple[PublicKey, BidReceipt]]:
    filters = [
        81,
        MemcmpOpts(offset=8, bytes=str(auction_day)),
    ]
    resp = client.get_program_accounts(
        program_id,
        encoding="base64",
        filters=filters,
    )
    value = _extract_value(resp)
    results: List[Tuple[PublicKey, BidReceipt]] = []
    if value is None:
        return results
    for item in value:
        if isinstance(item, dict):
            pubkey = PublicKey(item["pubkey"])
            data = bytes(__import__("base64").b64decode(item["account"]["data"][0]))
        else:
            pubkey = PublicKey(str(item.pubkey))
            data = _decode_account_data(item.account)
            if data is None:
                continue
        receipt = decode_bid_receipt(data)
        results.append((pubkey, receipt))
    return results
