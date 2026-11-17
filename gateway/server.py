"""JSON HTTP adapter implemented with Python's standard library."""

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from gateway.providers import PROVIDERS
from gateway.service import ValidationError, chat

MAX_BODY_BYTES = 65_536
ROUTES = {"/health": "GET", "/providers": "GET", "/chat": "POST"}


class GatewayHandler(BaseHTTPRequestHandler):
    def _json(self, status, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status, message, headers=None):
        self._json(status, {"status": "error", "error": message}, headers)

    def send_error(self, code, message=None, explain=None):
        """Keep errors from the underlying HTTP parser in JSON too."""
        self._error(code, message or HTTPStatus(code).phrase)

    def _dispatch(self):
        path = urlsplit(self.path).path
        allowed = ROUTES.get(path)
        if allowed is None:
            self._error(404, "Endpoint not found.")
        elif self.command != allowed:
            self._error(405, f"Use {allowed} for {path}.", {"Allow": allowed})
        elif path == "/health":
            self._json(200, {"status": "healthy", "service": "AI Model Gateway & Reliability Platform"})
        elif path == "/providers":
            self._json(200, {"providers": list(PROVIDERS), "simulated": True})
        else:
            self._handle_chat()

    def _handle_chat(self):
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(400, "Transfer-Encoding is not supported; use Content-Length.")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(415, "Content-Type must be application/json.")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._error(411, "Content-Length is required.")
            return
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._error(400, "Content-Length must be a non-negative integer.")
            return
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            self._error(413, f"Request body must be at most {MAX_BODY_BYTES} bytes.")
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._error(400, "Incomplete request body.")
            return
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=self._reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._error(400, "Request body must be valid UTF-8 JSON.")
            return
        try:
            result = chat(payload)
        except ValidationError as exc:
            self._error(400, str(exc))
            return
        self._json(200, result)

    @staticmethod
    def _reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch
    do_PATCH = _dispatch
    do_HEAD = _dispatch
    do_OPTIONS = _dispatch


def create_server(host="127.0.0.1", port=8000):
    return ThreadingHTTPServer((host, port), GatewayHandler)


def main():
    parser = argparse.ArgumentParser(description="Run the simulated AI gateway locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    with create_server(args.host, args.port) as server:
        print(f"AI gateway listening on http://{args.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
