import json
import os
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class Handler(BaseHTTPRequestHandler):
    server_version = "deploy-control/0.1"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == "/publish_preview":
            subprocess.run(["/bin/sh", "/app/scripts/publish_preview.sh"], check=True)
            self._send_json(HTTPStatus.OK, {"status": "preview published"})
            return

        if path == "/deploy":
            subprocess.run(["/bin/sh", "/app/scripts/deploy.sh"], check=True)
            self._send_json(HTTPStatus.OK, {"status": "production deployed"})
            return

        if path == "/nuke":
            subprocess.run(["/bin/sh", "/app/scripts/nuke.sh"], check=True)
            self._send_json(HTTPStatus.OK, {"status": "nuke complete"})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


def main() -> None:
    port = int(os.environ.get("DEPLOY_PORT", "9090"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Deploy control plane listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
