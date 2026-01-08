import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import base58
import requests
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from solana.publickey import PublicKey


@dataclass
class AuthContext:
    wallet: str
    nonce: str
    expiry: int


class NonceStore:
    def __init__(self) -> None:
        self._nonces: dict[str, int] = {}

    def add(self, nonce: str, expiry: int) -> None:
        self._nonces[nonce] = expiry

    def seen(self, nonce: str) -> bool:
        self._cleanup()
        return nonce in self._nonces

    def _cleanup(self) -> None:
        now = int(time.time())
        expired = [key for key, exp in self._nonces.items() if exp <= now]
        for key in expired:
            del self._nonces[key]


class CreditLimiter:
    def __init__(self, max_per_wallet_per_day: int) -> None:
        self._max = max_per_wallet_per_day
        self._counts: dict[tuple[str, str], int] = {}

    def allow(self, wallet: str) -> bool:
        master_wallet = os.environ.get("MASTER_WALLET", "").strip()
        if master_wallet and wallet == master_wallet:
            return True
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (wallet, day)
        count = self._counts.get(key, 0)
        if count >= self._max:
            return False
        self._counts[key] = count + 1
        return True

    def usage(self, wallet: str) -> tuple[int, int, int]:
        master_wallet = os.environ.get("MASTER_WALLET", "").strip()
        if master_wallet and wallet == master_wallet:
            return 0, self._max, self._max
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (wallet, day)
        used = self._counts.get(key, 0)
        remaining = max(0, self._max - used)
        return used, remaining, self._max


SECONDS_PER_DAY = 86_400
AUCTION_DAY_DISCRIMINATOR = hashlib.sha256(b"account:AuctionDay").digest()[:8]
DEFAULT_PUBKEY = str(PublicKey(bytes(32)))
_winner_lock = threading.Lock()
_winner_cache: dict[str, object] = {"day_index": None, "winner": None, "timestamp": 0.0}


def _current_day_index() -> int:
    return int(time.time()) // SECONDS_PER_DAY


def _auction_day_pda(program_id: PublicKey, day_index: int) -> PublicKey:
    seed = [b"auction_day", day_index.to_bytes(8, "little", signed=True)]
    return PublicKey.find_program_address(seed, program_id)[0]


def _decode_winner(data: bytes) -> str | None:
    if len(data) < 8 + 8 + 1 + 32:
        raise ValueError("auction day data too short")
    if data[:8] != AUCTION_DAY_DISCRIMINATOR:
        raise ValueError("invalid auction day discriminator")
    offset = 8 + 8 + 1
    winner_bytes = data[offset : offset + 32]
    winner = str(PublicKey(winner_bytes))
    if winner == DEFAULT_PUBKEY:
        return None
    return winner


def _fetch_auction_day_winner(program_id: str, day_index: int, rpc_url: str) -> str | None:
    program_key = PublicKey(program_id)
    auction_day = _auction_day_pda(program_key, day_index)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [str(auction_day), {"encoding": "base64"}],
    }
    response = requests.post(rpc_url, json=body, timeout=10)
    response.raise_for_status()
    payload = response.json()
    value = payload.get("result", {}).get("value")
    if not value:
        return None
    data = value.get("data")
    if not data or not isinstance(data, list) or not data[0]:
        return None
    decoded = base64.b64decode(data[0])
    return _decode_winner(decoded)


def _get_cached_winner(day_index: int, ttl_seconds: int) -> str | None:
    now = time.time()
    with _winner_lock:
        if _winner_cache["day_index"] == day_index and now - float(_winner_cache["timestamp"]) < ttl_seconds:
            return _winner_cache["winner"]  # type: ignore[return-value]
    return None


def _set_cached_winner(day_index: int, winner: str | None) -> None:
    with _winner_lock:
        _winner_cache["day_index"] = day_index
        _winner_cache["winner"] = winner
        _winner_cache["timestamp"] = time.time()


def is_wallet_authorized(wallet: str) -> bool:
    program_id = os.environ.get("AUCTION_PROGRAM_ID", "").strip()
    if not program_id:
        raise ValueError("AUCTION_PROGRAM_ID not set")
    rpc_url = os.environ.get("RPC_URL", "https://api.devnet.solana.com")
    day_index = _current_day_index() - 1
    if day_index < 0:
        return False
    ttl_seconds = int(os.environ.get("WINNER_CACHE_TTL_SECONDS", "30"))
    winner = _get_cached_winner(day_index, ttl_seconds)
    if winner is None:
        try:
            winner = _fetch_auction_day_winner(program_id, day_index, rpc_url)
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"winner lookup failed: {exc}") from exc
        _set_cached_winner(day_index, winner)
    return winner is not None and winner == wallet


def _hash_body(body_bytes: bytes) -> str:
    return hashlib.sha256(body_bytes).hexdigest()


def _signature_message(wallet: str, nonce: str, expiry: int, path: str, body_hash: str) -> bytes:
    message = f"{wallet}:{nonce}:{expiry}:{path}:{body_hash}"
    return message.encode("utf-8")


def _verify_ed25519_signature(wallet: str, signature: str, message: bytes) -> bool:
    try:
        public_key = VerifyKey(base58.b58decode(wallet))
        if not signature.startswith("base64:"):
            raise ValueError("signature must be base64")
        signed = base64.b64decode(signature[len("base64:") :])
        public_key.verify(message, signed)
        return True
    except (ValueError, BadSignatureError):
        return False


def verify_request(headers: dict[str, str], body_bytes: bytes, path: str, nonce_store: NonceStore) -> AuthContext:
    wallet = headers.get("x-wallet", "").strip()
    nonce = headers.get("x-nonce", "").strip()
    expiry_raw = headers.get("x-expiry", "").strip()
    signature = headers.get("x-signature", "").strip()

    if not wallet or not nonce or not expiry_raw or not signature:
        raise ValueError("missing auth headers")

    master_wallet = os.environ.get("MASTER_WALLET", "").strip()
    if master_wallet and wallet == master_wallet:
        pass
    else:
        if not is_wallet_authorized(wallet):
            raise ValueError("wallet not authorized")

    try:
        expiry = int(expiry_raw)
    except ValueError as exc:
        raise ValueError("invalid expiry") from exc

    now = int(time.time())
    if expiry <= now:
        raise ValueError("expired request")

    if nonce_store.seen(nonce):
        raise ValueError("nonce already used")

    body_hash = _hash_body(body_bytes)
    message = _signature_message(wallet, nonce, expiry, path, body_hash)

    if not _verify_ed25519_signature(wallet, signature, message):
        raise ValueError("invalid signature")

    nonce_store.add(nonce, expiry)
    return AuthContext(wallet=wallet, nonce=nonce, expiry=expiry)
