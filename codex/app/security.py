import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


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
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (wallet, day)
        count = self._counts.get(key, 0)
        if count >= self._max:
            return False
        self._counts[key] = count + 1
        return True


def _hash_body(body_bytes: bytes) -> str:
    return hashlib.sha256(body_bytes).hexdigest()


def _signature_message(wallet: str, nonce: str, expiry: int, path: str, body_hash: str) -> bytes:
    message = f"{wallet}:{nonce}:{expiry}:{path}:{body_hash}"
    return message.encode("utf-8")


def _verify_ed25519_signature(wallet: str, signature: str, message: bytes) -> bool:
    try:
        public_key = VerifyKey(base58.b58decode(wallet))
        signed = base58.b58decode(signature)
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
    if master_wallet and wallet != master_wallet:
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
