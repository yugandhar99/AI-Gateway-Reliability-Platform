import http.client
import json
from io import BytesIO
import unittest

from gateway.server import MAX_BODY_BYTES, GatewayHandler


class MemoryConnection:
    """Supply real HTTP wire bytes without requiring OS socket permissions."""

    def __init__(self, request):
        self.input = BytesIO(request)
        self.output = BytesIO()

    def makefile(self, mode, *args):
        return self.input if "r" in mode else self.output

    def sendall(self, data):
        self.output.write(data)


class QuietHandler(GatewayHandler):
    def log_message(self, format, *args):
        pass


class HTTPTests(unittest.TestCase):
    def request(self, method, path, body=None, headers=None, add_length=True):
        body = body.encode("utf-8") if isinstance(body, str) else body or b""
        headers = dict(headers or {})
        if add_length and "Content-Length" not in headers:
            headers["Content-Length"] = str(len(body))
        wire = f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n"
        wire += "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        connection = MemoryConnection(wire.encode("ascii") + b"\r\n" + body)
        QuietHandler(connection, ("127.0.0.1", 12345), None)
        response = http.client.HTTPResponse(MemoryConnection(connection.output.getvalue()))
        response.begin()
        data = response.read()
        self.assertTrue(response.getheader("Content-Type").startswith("application/json"))
        self.assertEqual(int(response.getheader("Content-Length")), len(data))
        return response.status, dict(response.getheaders()), json.loads(data) if data else None

    def test_health(self):
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "healthy")

    def test_providers(self):
        status, _, body = self.request("GET", "/providers")
        self.assertEqual(status, 200)
        self.assertEqual(body["providers"], ["OpenAI", "Anthropic", "Gemini"])
        self.assertTrue(body["simulated"])

    def test_chat(self):
        for provider in ("OpenAI", "Anthropic", "Gemini"):
            with self.subTest(provider=provider):
                status, _, body = self.request("POST", "/chat", json.dumps(
                    {"prompt": "Hello", "provider": provider}), {"Content-Type": "application/json"})
                self.assertEqual(status, 200)
                self.assertEqual(body["provider"], provider)
                self.assertEqual(body["status"], "success")
                self.assertIsInstance(body["response"], str)
                self.assertGreaterEqual(body["response_time_ms"], 0)

    def test_invalid_json_and_inputs(self):
        for raw in (b"", b"{", b"\xff", b"null", b"[]", b"{}", b'{"prompt":NaN}',
                    b'{"prompt":"hi","provider":"missing"}'):
            with self.subTest(raw=raw):
                status, _, body = self.request("POST", "/chat", raw, {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                self.assertEqual(body["status"], "error")
                self.assertTrue(body["error"])

    def test_unsupported_content_type(self):
        self.assertEqual(self.request("POST", "/chat", "{}")[0], 415)

    def test_oversized_body(self):
        self.assertEqual(self.request("POST", "/chat", "", {
            "Content-Type": "application/json", "Content-Length": str(MAX_BODY_BYTES + 1)})[0], 413)

    def test_invalid_content_length(self):
        for length in ("-1", "abc"):
            with self.subTest(length=length):
                self.assertEqual(self.request("POST", "/chat", "", {
                    "Content-Type": "application/json", "Content-Length": length})[0], 400)

    def test_missing_length(self):
        self.assertEqual(self.request("POST", "/chat", "", {
            "Content-Type": "application/json", "Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request("POST", "/chat", headers={
            "Content-Type": "application/json"}, add_length=False)[0], 411)

    def test_incomplete_body(self):
        self.assertEqual(self.request("POST", "/chat", "{}", {
            "Content-Type": "application/json", "Content-Length": "10"})[0], 400)

    def test_routing_and_methods(self):
        self.assertEqual(self.request("GET", "/missing")[0], 404)
        for method, path, allowed in (("GET", "/chat", "POST"), ("POST", "/health", "GET"),
                                      ("PUT", "/providers", "GET")):
            with self.subTest(method=method, path=path):
                status, headers, _ = self.request(method, path)
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], allowed)
