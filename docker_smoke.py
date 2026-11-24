"""Run manually after building ai-gateway:stage4; never print credentials."""

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def compose(*args):
    return subprocess.check_output(
        ["docker", "compose", *args], cwd=Path(__file__).parent, text=True
    ).strip()


def request(path, key=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("http://127.0.0.1:8002" + path, data, headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        return response.code, json.load(response)


def check(condition, message):
    if not condition:
        raise SystemExit("FAIL: " + message)
    print("PASS: " + message)


def main():
    key = os.environ.get("GATEWAY_STARTER_API_KEY")
    if not key:
        raise SystemExit("Export GATEWAY_STARTER_API_KEY first (see README).")
    compose("up", "-d", "--wait", "--wait-timeout", "60")
    container = compose("ps", "-q", "gateway")
    details = json.loads(subprocess.check_output(
        ["docker", "inspect", container], text=True))[0]
    # Inspect output contains environment secrets: parse in memory, never print it.
    check(details["Config"]["User"] == "10001:10001", "non-root container user")
    check(details["State"]["Health"]["Status"] == "healthy", "Docker health check")
    bindings = details["NetworkSettings"]["Ports"]["8000/tcp"]
    check(bindings == [{"HostIp": "127.0.0.1", "HostPort": "8002"}],
          "published only at localhost:8002")
    check(request("/health")[0] == 200, "public health endpoint")
    check(request("/usage")[0] == 401, "missing credentials rejected")
    check(request("/usage", "invalid-smoke-key")[0] == 401, "invalid credentials rejected")
    status, before = request("/usage", key)
    check(status == 200, "customer authentication")
    status, _ = request("/chat", key, {"prompt": "Stage 4 smoke check", "provider": "OpenAI"})
    check(status == 200, "authenticated simulated chat")
    status, after = request("/usage", key)
    check(status == 200 and after["requests"] == before["requests"] + 1
          and after["successes"] == before["successes"] + 1, "SQLite usage recorded")
    compose("restart", "gateway")
    compose("up", "-d", "--wait", "--wait-timeout", "60")
    check(request("/usage", key) == (200, after), "usage survives container restart")
    compose("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "60")
    check(request("/usage", key) == (200, after), "volume survives container replacement")
    print("Smoke checks completed; service and volume retained. One simulated request was recorded.")


if __name__ == "__main__":
    main()
