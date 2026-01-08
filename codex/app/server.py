import json
import os
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

from security import AuthContext, CreditLimiter, NonceStore, verify_request
from html_edit import HtmlEditError, run_html_edit

SITE_DIR = os.environ.get("SITE_DIR", "/app/site")
DEPLOY_INTERNAL_URL = os.environ.get("DEPLOY_INTERNAL_URL", "http://deploy:9090")
HISTORY_PATH = os.environ.get("HISTORY_PATH", "/app/site/.codex_history.jsonl")
HISTORY_MAX_LINES = int(os.environ.get("HISTORY_MAX_LINES", "2000"))
HTML_EDIT_TARGET_PATH = os.environ.get("HTML_EDIT_TARGET_PATH", os.path.join(SITE_DIR, "index.html"))
nonce_store = NonceStore()
credit_limiter = CreditLimiter(int(os.environ.get("MAX_REQUESTS_PER_WALLET_PER_DAY", "50")))
history_lock = threading.Lock()
edit_lock = threading.Lock()


def append_history(entry: dict) -> None:
    payload = {
        "timestamp": int(time.time()),
        **entry,
    }
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with history_lock:
        with open(HISTORY_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        if HISTORY_MAX_LINES > 0:
            _trim_history(HISTORY_MAX_LINES)


def _trim_history(max_lines: int) -> None:
    if not os.path.exists(HISTORY_PATH):
        return
    with open(HISTORY_PATH, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    if len(lines) <= max_lines:
        return
    trimmed = lines[-max_lines:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as handle:
        handle.writelines(trimmed)


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

    def _authenticate(self, body: bytes, path: str, consume: bool = True) -> AuthContext:
        headers = {key.lower(): value for key, value in self.headers.items()}
        ctx = verify_request(headers, body, path, nonce_store)
        if consume and not credit_limiter.allow(ctx.wallet):
            raise ValueError("credit limit exceeded")
        return ctx

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
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == "/reset":
            self._handle_internal_reset(body)
            return

        if path == "/credits":
            payload = json.loads(body or b"{}")
            wallet = payload.get("wallet") or self.headers.get("X-Wallet", "")
            wallet = wallet.strip()
            if not wallet:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "wallet required"})
                return
            used, remaining, max_per_day = credit_limiter.usage(wallet)
            self._send_json(
                HTTPStatus.OK,
                {"used": used, "remaining": remaining, "max": max_per_day},
            )
            return

        try:
            self._authenticate(body, path)
        except ValueError as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
            return

        if path == "/edit":
            payload = json.loads(body or b"{}")
            instruction = payload.get("instruction", "")
            instruction_log = instruction
            if len(instruction_log) > 200:
                instruction_log = f"{instruction_log[:200]}…"
            append_history(
                {
                    "type": "edit_request",
                    "wallet": self.headers.get("X-Wallet", ""),
                    "instruction": instruction_log,
                }
            )
            try:
                with edit_lock:
                    result = run_html_edit(instruction, HTML_EDIT_TARGET_PATH, HTML_EDIT_TARGET_PATH)
                    append_history(
                        {
                            "type": "edit_debug",
                            "wallet": self.headers.get("X-Wallet", ""),
                            "bytes_written": result.get("bytes_written"),
                            "original_hash": result.get("original_hash"),
                            "updated_hash": result.get("updated_hash"),
                            "changed": result.get("changed"),
                            "codex_stdout": result.get("codex_stdout"),
                            "codex_stderr": result.get("codex_stderr"),
                        "codex_stdout_len": result.get("codex_stdout_len"),
                        "codex_stderr_len": result.get("codex_stderr_len"),
                        "html_length": result.get("html_length"),
                    }
                )
            except HtmlEditError as exc:
                append_history(
                    {
                        "type": "edit_error",
                        "wallet": self.headers.get("X-Wallet", ""),
                        "status": HTTPStatus.BAD_REQUEST,
                        "response": str(exc),
                    }
                )
                payload = {"error": str(exc)}
                if exc.raw_output is not None:
                    payload["raw_output"] = exc.raw_output
                self._send_json(HTTPStatus.BAD_REQUEST, payload)
                return
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
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Codex control plane listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
