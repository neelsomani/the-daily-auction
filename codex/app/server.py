import json
import os
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import requests

from security import CreditLimiter, NonceStore, verify_request

SITE_DIR = os.environ.get("SITE_DIR", "/app/site")
DEPLOY_INTERNAL_URL = os.environ.get("DEPLOY_INTERNAL_URL", "http://deploy:9090")
HISTORY_PATH = os.environ.get("HISTORY_PATH", "/app/site/.codex_history.jsonl")
nonce_store = NonceStore()
credit_limiter = CreditLimiter(int(os.environ.get("MAX_REQUESTS_PER_WALLET_PER_DAY", "50")))
history_lock = threading.Lock()


def append_history(entry: dict) -> None:
    payload = {
        "timestamp": int(time.time()),
        **entry,
    }
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with history_lock:
        with open(HISTORY_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")


def read_history(limit: int) -> list[dict]:
    if not os.path.exists(HISTORY_PATH):
        return []
    with history_lock:
        with open(HISTORY_PATH, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    trimmed = lines[-limit:] if limit > 0 else lines
    items = []
    for line in trimmed:
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "codex-control/0.1"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Wallet, X-Nonce, X-Expiry, X-Signature",
        )
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def _authenticate(self, body: bytes) -> bool:
        headers = {key.lower(): value for key, value in self.headers.items()}
        ctx = verify_request(headers, body, self.path, nonce_store)
        if not credit_limiter.allow(ctx.wallet):
            raise ValueError("credit limit exceeded")
        return True

    def _handle_internal_reset(self, body: bytes) -> None:
        payload = json.loads(body or b"{}")
        source_dir = payload.get("source_dir")
        if not source_dir:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "source_dir required"})
            return
        subprocess.run(["/bin/sh", "/app/scripts/reset.sh", source_dir, SITE_DIR], check=True)
        self._send_json(HTTPStatus.OK, {"status": "reset complete"})

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Wallet, X-Nonce, X-Expiry, X-Signature",
        )
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/history":
            params = parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            items = read_history(max(1, min(limit, 500)))
            self._send_json(HTTPStatus.OK, {"items": items})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/reset":
            self._handle_internal_reset(body)
            return

        try:
            self._authenticate(body)
        except ValueError as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
            return

        if path == "/edit":
            payload = json.loads(body or b"{}")
            instruction = payload.get("instruction", "")
            append_history(
                {
                    "type": "edit_request",
                    "wallet": self.headers.get("X-Wallet", ""),
                    "instruction": instruction,
                }
            )
            subprocess.run(["/bin/sh", "/app/scripts/edit.sh", instruction, SITE_DIR], check=True)
            try:
                response = requests.post(f"{DEPLOY_INTERNAL_URL}/publish_preview", timeout=30)
                if response.status_code >= 400:
                    append_history(
                        {
                            "type": "edit_error",
                            "wallet": self.headers.get("X-Wallet", ""),
                            "status": response.status_code,
                            "response": response.text,
                        }
                    )
                    self._send_json(response.status_code, response.json())
                    return
            except requests.RequestException as exc:
                append_history(
                    {
                        "type": "edit_error",
                        "wallet": self.headers.get("X-Wallet", ""),
                        "status": HTTPStatus.BAD_GATEWAY,
                        "response": str(exc),
                    }
                )
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
            append_history(
                {
                    "type": "edit_complete",
                    "wallet": self.headers.get("X-Wallet", ""),
                    "status": "preview_published",
                }
            )
            self._send_json(HTTPStatus.OK, {"status": "edit applied"})
            return

        if path == "/deploy":
            response = requests.post(f"{DEPLOY_INTERNAL_URL}/deploy", timeout=30)
            append_history(
                {
                    "type": "deploy",
                    "wallet": self.headers.get("X-Wallet", ""),
                    "status": response.status_code,
                    "response": response.text,
                }
            )
            self._send_json(response.status_code, response.json())
            return

        if path == "/nuke":
            response = requests.post(f"{DEPLOY_INTERNAL_URL}/nuke", timeout=30)
            append_history(
                {
                    "type": "nuke",
                    "wallet": self.headers.get("X-Wallet", ""),
                    "status": response.status_code,
                    "response": response.text,
                }
            )
            self._send_json(response.status_code, response.json())
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main() -> None:
    port = int(os.environ.get("CODEX_PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Codex control plane listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
