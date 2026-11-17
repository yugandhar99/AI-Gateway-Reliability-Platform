# AI Model Gateway & Reliability Platform

Stage 2 is a local HTTP API that simulates OpenAI, Anthropic, and Gemini.
It uses only the Python standard library. No API keys, paid services, package
installation, or external API calls are needed. Python 3.10+ is recommended.

## Run locally

From this project directory:

```bash
cd /home/yetha/projects/ai-platform-mock
python3 -m gateway.server
```

The API listens on `http://127.0.0.1:8000`. Stop it with Ctrl+C.
To use another port: `python3 -m gateway.server --port 8001`.

In a second terminal:

```bash
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/providers
curl -i http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain an API in one sentence.","provider":"OpenAI"}'
```

## API contract

| Method | Path | Result |
| --- | --- | --- |
| GET | `/health` | `status: "healthy"` and the service name |
| GET | `/providers` | Provider names and `simulated: true` |
| GET | `/metrics` | Per-provider attempt counters and circuit health |
| POST | `/chat` | Simulated response, canonical provider name, status, elapsed milliseconds |

Example chat response (timing varies):

```json
{
  "response": "[Simulated OpenAI response] You said: Explain an API in one sentence.",
  "provider": "OpenAI",
  "status": "success",
  "response_time_ms": 0.012
}
```

`response_time_ms` measures validation and local response generation using a
monotonic performance clock; it excludes network time. It may round to zero.
The mock echoes the prompt and does not perform AI inference.

Requests must contain a JSON object with non-empty string fields `prompt` and
`provider`. Surrounding whitespace is trimmed; provider names are case-insensitive.
Prompts are limited to 10,000 characters before trimming, and request bodies to
65,536 bytes. Extra fields are ignored. Send UTF-8 JSON with
`Content-Type: application/json` and `Content-Length` (curl sets this automatically).
Chunked transfer encoding is unsupported.

Errors use `{"status":"error","error":"A meaningful explanation."}`:

| Status | Meaning |
| --- | --- |
| 400 | Invalid JSON, invalid fields, unknown provider, or invalid request framing |
| 404 | Unknown endpoint |
| 405 | Wrong method for an endpoint; `Allow` identifies the supported method |
| 411 | Missing Content-Length |
| 413 | Request body too large |
| 415 | Unsupported or missing Content-Type |
| 503 | All providers failed or have unavailable circuits |

## Structure and design

```text
gateway/
  __init__.py
  providers.py       # Provider names and deterministic simulated responses
  reliability.py     # Policies, simulation, routing, circuits, and metrics
  service.py         # Validation, dispatch, and elapsed-time measurement
  server.py          # HTTP routing, JSON parsing, and command-line entry point
tests/
  __init__.py
  test_service.py    # Business logic unit tests
  test_http.py       # HTTP handler tests using in-memory request/response streams
.gitignore
README.md
```

Separating the service from HTTP makes validation directly testable. The HTTP
adapter translates requests and errors into JSON. Provider simulation stays in
its own module so it is easy to understand where responses come from.

## Tests

```bash
python3 -m unittest discover -v
```

Tests cover all providers, normalization, Unicode prompts, invalid fields,
length boundaries, health, provider listing, response fields, malformed JSON,
content types, body size, request framing, routes, and HTTP methods. HTTP tests
pass raw HTTP requests through the actual handler using in-memory streams, then
parse the responses with `http.client`. No socket permissions are needed.
A live listening server cannot be verified in environments that block sockets.

This is a learning service built with `http.server`, intended for local use.
State and counters live in one server process and reset on restart. There is no
authentication, persistence, or real API integration.

## Reliability architecture

The service validates input before routing. The requested provider goes first;
remaining providers follow the order OpenAI, Anthropic, Gemini, excluding the
requested provider. The successful response keeps exactly the Stage 1 fields;
`provider` identifies the provider that actually answered.

Each provider has its own policy and a circuit breaker, a switch that temporarily
stops calls to a failing provider:

- CLOSED allows requests. Every failed attempt increments consecutive failures.
- At the threshold, OPEN skips that provider for the recovery interval.
- The first request after that interval changes it to HALF_OPEN and admits one
  probe. Other requests skip it while the probe runs.
- A successful probe closes the circuit and clears consecutive failures; a failed
  probe reopens it immediately. Probes never retry.

A successful ordinary attempt also clears consecutive failures. Circuit opening
can stop retries early. Locks protect shared state, and circuit generations stop
old in-flight results from undoing newer circuit transitions. No background
recovery job runs: a new chat request triggers a probe after the cooldown.

`GET /metrics` returns JSON under `providers`, keyed by canonical name. `requests`,
`successes`, and `failures` count actual provider attempts, including retries;
`skipped` counts circuit admission refusals. In-flight attempts can temporarily
make requests greater than successes plus failures. `state`, `healthy`,
`consecutive_failures`, and `last_error` describe health. Healthy means CLOSED
with no consecutive failures. Reading metrics never triggers recovery.

Routing logs are JSON lines on stderr: attempts, successes, failures, failovers,
skips, and circuit transitions. They contain provider names and fixed failure
categories, never prompts, payloads, credentials, or request URLs.

## Configure policies and simulate failures

Run these commands from the project directory. Policies load at startup. Missing
providers use defaults: timeout 0.2 seconds, one retry (two total attempts),
failure threshold 3, recovery interval 5 seconds. Retries have no backoff because
this is a bounded local simulation. Retry count accepts integers 0–10; failure
threshold accepts integers 1–1000; both time values must be finite and positive.

```bash
cd /home/yetha/projects/ai-platform-mock
cat > policies.json <<'JSON'
{
  "OpenAI": {
    "timeout_seconds": 0.1,
    "max_retries": 1,
    "failure_threshold": 2,
    "recovery_seconds": 3
  }
}
JSON
printf '%s\n' '{}' > simulation.json
python3 -m gateway.server --config policies.json --simulation-file simulation.json
```

In a second terminal, simulate OpenAI 503 failures and observe Anthropic answer:

```bash
cd /home/yetha/projects/ai-platform-mock
printf '%s\n' '{"OpenAI":["503"]}' > simulation.next.json
mv simulation.next.json simulation.json
curl -sS http://127.0.0.1:8000/chat -H 'Content-Type: application/json' -d '{"prompt":"Hello","provider":"OpenAI"}'
curl -sS http://127.0.0.1:8000/metrics
```

Recover OpenAI, allow the three-second cooldown to expire, then trigger a probe:

```bash
printf '%s\n' '{}' > simulation.next.json
mv simulation.next.json simulation.json
sleep 3
curl -sS http://127.0.0.1:8000/chat -H 'Content-Type: application/json' -d '{"prompt":"Recovered?","provider":"OpenAI"}'
curl -sS http://127.0.0.1:8000/metrics
```

Simulate a timeout, retry, and successful recovery within one request:

```bash
printf '%s\n' '{"OpenAI":["timeout","success"]}' > simulation.next.json
mv simulation.next.json simulation.json
curl -sS http://127.0.0.1:8000/chat -H 'Content-Type: application/json' -d '{"prompt":"Retry demo","provider":"OpenAI"}'
```

Simulate all providers failing (curl shows HTTP 503):

```bash
printf '%s\n' '{"OpenAI":["503"],"Anthropic":["503"],"Gemini":["timeout"]}' > simulation.next.json
mv simulation.next.json simulation.json
curl -i http://127.0.0.1:8000/chat -H 'Content-Type: application/json' -d '{"prompt":"Failure demo","provider":"OpenAI"}'
```

Reset and recover all providers:

```bash
printf '%s\n' '{}' > simulation.next.json
mv simulation.next.json simulation.json
sleep 5
curl -sS http://127.0.0.1:8000/chat -H 'Content-Type: application/json' -d '{"prompt":"Hello","provider":"OpenAI"}'
curl -sS http://127.0.0.1:8000/chat -H 'Content-Type: application/json' -d '{"prompt":"Hello","provider":"Anthropic"}'
curl -sS http://127.0.0.1:8000/chat -H 'Content-Type: application/json' -d '{"prompt":"Hello","provider":"Gemini"}'
curl -sS http://127.0.0.1:8000/metrics
```

Simulation JSON maps provider names to nonempty outcome lists containing
`success`, `503`, or `timeout`. Unlisted providers succeed. Sequences consume one
entry per attempt and repeat their final entry. A changed JSON configuration
resets sequence positions for all providers, but preserves circuit state and
counters. Atomic file replacement (`mv` above) avoids partial reads. A timeout
waits exactly the configured simulated deadline then raises a timeout failure;
there are no network calls or worker threads to cancel. This models a deadline
for simulated calls; a future real integration must enforce deadlines in its
network client.

## Troubleshooting Stage 2

- Still failing after editing the file: inspect `/metrics`, wait for the provider's
  recovery interval, then send another chat request to trigger its probe.
- `simulation_config_error`: check the file path, valid JSON, canonical provider
  names, nonempty lists, and supported outcomes. File errors fail closed through
  the normal retry and failover path; file contents are never logged.
- Unexpected latency: each timeout attempt waits its configured timeout. With
  all providers timing out, the upper simulated wait is the sum of each
  provider's timeout times its allowed attempts. Circuit skips reduce this.
- Policy edits have no effect: restart the server; only simulation data reloads.
- Port already in use: run with `--port 8001` and update curl URLs.

Run `python3 -m unittest discover -v` for both stages. Reliability tests inject a
clock and sleeper, use temporary local simulation files, and exercise in-memory
HTTP requests. They make no network calls or slow sleeps.
