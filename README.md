# AI Model Gateway & Reliability Platform

Stage 3 is a local HTTP API that simulates OpenAI, Anthropic, and Gemini.
It uses only the Python standard library. No real provider API keys, paid services, package
installation, or external API calls are needed. Local demo bearer keys are required. Python 3.10+ is recommended.

## Run locally

From this project directory:

```bash
cd /home/yetha/projects/ai-platform-mock
umask 077
export GATEWAY_STARTER_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GATEWAY_BUSINESS_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GATEWAY_ENTERPRISE_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GATEWAY_ADMIN_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 -m gateway.server --port 8001 --usage-db usage.sqlite3
```

This example listens on `http://127.0.0.1:8001`, leaving the running Stage 2
server on port 8000 untouched. All examples below use port 8001. Stop it with Ctrl+C.
To use another free port: `python3 -m gateway.server --port 8002`.

In a second terminal with the same key environment (or run the server in the
background from the configured shell):

```bash
curl -i http://127.0.0.1:8001/health
curl -i http://127.0.0.1:8001/providers
curl -i http://127.0.0.1:8001/chat -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain an API in one sentence.","provider":"OpenAI"}'
```

## API contract

| Method | Path | Result |
| --- | --- | --- |
| GET | `/health` | `status: "healthy"` and the service name |
| GET | `/providers` | Provider names and `simulated: true` |
| GET | `/metrics` | Administrator bearer key required; provider counters and circuit health |
| GET | `/usage` | Customer bearer key required; only that customer’s monthly usage |
| POST | `/chat` | Customer bearer key required; response fields remain unchanged |

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
| 401 | Missing, invalid, duplicate, or wrong-role bearer credentials |
| 429 | Rate limit or monthly quota exceeded; `code` distinguishes them |
| 400 | Invalid JSON, invalid fields, unknown provider, or invalid request framing |
| 404 | Unknown endpoint |
| 405 | Wrong method for an endpoint; `Allow` identifies the supported method |
| 411 | Missing Content-Length |
| 413 | Request body too large |
| 415 | Unsupported or missing Content-Type |
| 503 | All providers unavailable, or usage accounting unavailable |

## Structure and design

```text
gateway/
  __init__.py
  providers.py       # Provider names and deterministic simulated responses
  reliability.py     # Policies, simulation, routing, circuits, and metrics
  security.py        # Environment keys, customer identity, and roles
  usage.py           # SQLite admission, quota reservations, and accounting
  service.py         # Validation, dispatch, and elapsed-time measurement
  server.py          # HTTP routing, JSON parsing, and command-line entry point
tests/
  __init__.py
  test_service.py    # Business logic unit tests
  test_security.py   # Authentication, isolation, limits, persistence, concurrency
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
Provider circuit state and provider counters reset on restart. Customer usage,
reservations, and rate admissions persist in SQLite. No real APIs are called.

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
curl -sS http://127.0.0.1:8001/chat -H 'Content-Type: application/json' -d '{"prompt":"Hello","provider":"OpenAI"}' -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY"
curl -sS http://127.0.0.1:8001/metrics -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY"
```

Recover OpenAI, allow the three-second cooldown to expire, then trigger a probe:

```bash
printf '%s\n' '{}' > simulation.next.json
mv simulation.next.json simulation.json
sleep 3
curl -sS http://127.0.0.1:8001/chat -H 'Content-Type: application/json' -d '{"prompt":"Recovered?","provider":"OpenAI"}' -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY"
curl -sS http://127.0.0.1:8001/metrics -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY"
```

Simulate a timeout, retry, and successful recovery within one request:

```bash
printf '%s\n' '{"OpenAI":["timeout","success"]}' > simulation.next.json
mv simulation.next.json simulation.json
curl -sS http://127.0.0.1:8001/chat -H 'Content-Type: application/json' -d '{"prompt":"Retry demo","provider":"OpenAI"}' -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY"
```

Simulate all providers failing (curl shows HTTP 503):

```bash
printf '%s\n' '{"OpenAI":["503"],"Anthropic":["503"],"Gemini":["timeout"]}' > simulation.next.json
mv simulation.next.json simulation.json
curl -i http://127.0.0.1:8001/chat -H 'Content-Type: application/json' -d '{"prompt":"Failure demo","provider":"OpenAI"}' -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY"
```

Reset and recover all providers:

```bash
printf '%s\n' '{}' > simulation.next.json
mv simulation.next.json simulation.json
sleep 5
curl -sS http://127.0.0.1:8001/chat -H 'Content-Type: application/json' -d '{"prompt":"Hello","provider":"OpenAI"}' -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY"
curl -sS http://127.0.0.1:8001/chat -H 'Content-Type: application/json' -d '{"prompt":"Hello","provider":"Anthropic"}' -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY"
curl -sS http://127.0.0.1:8001/chat -H 'Content-Type: application/json' -d '{"prompt":"Hello","provider":"Gemini"}' -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY"
curl -sS http://127.0.0.1:8001/metrics -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY"
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

## Stage 3 security and accounting

| Demo customer | Environment variable | Requests per rolling 60 seconds | Tokens per UTC month |
| --- | --- | --- | --- |
| starter-demo | `GATEWAY_STARTER_API_KEY` | 5 | 10,000 |
| business-demo | `GATEWAY_BUSINESS_API_KEY` | 30 | 100,000 |
| enterprise-demo | `GATEWAY_ENTERPRISE_API_KEY` | 100 | 1,000,000 |

The server loads keys from the environment once at startup. At least one customer
key is required; unset customer tiers are disabled. Keys must be distinct,
nonempty ASCII values without whitespace. The optional `GATEWAY_ADMIN_API_KEY`
must differ from customer keys; without it, metrics access is disabled. Admin
keys cannot use customer endpoints, and customer keys cannot read metrics.
`/health` and the static `/providers` list remain public. Duplicate Authorization
headers are rejected. Query parameters and JSON cannot choose a customer: the
bearer key alone determines whose quota and usage apply.

The startup commands above generate fresh demonstration keys without putting
literal keys in source or shell history. Keep them in the same shell as your
curl commands. For example, add `&` to the server command to run it in the
background, or securely configure the same environment in your second terminal.
Do not print or share your keys. Environment variables are not automatically
loaded from `.env`; this server adds no dotenv dependency. Use `umask 077` before
creating the database so it is private to your local user. Local SQLite files,
sidecars, `.env` files, and simulation inputs are ignored by Git. For key rotation,
restart your Stage 3 server with new values: stable customer IDs preserve usage.
This local HTTP demonstration binds to loopback by default. Remote deployment
would require TLS and a production HTTP server; bearer keys travel in headers.

The request path is authentication → existing input validation → atomic rate
and quota admission → existing provider router → accounting settlement. Invalid
input and authentication failures do not consume rate or quota. Each admitted
chat consumes one rate slot, including chats where all providers fail. Provider
retries and failovers do not count as additional customer requests. `/usage`
and `/metrics` reads do not consume chat rate slots.

Token estimation is deliberately simple and reproducible: round each input and
output character count up to groups of four Unicode characters, then add them.
The input is the trimmed prompt; output is the actual successful simulated
response. This is not a real model tokenizer. Costs use a fictional flat price
of **estimated USD 1 per million estimated tokens**, identical for every provider
and tier. No real money is charged. `/usage` labels tokens and costs as estimates;
its USD value is a six-decimal string calculated with integers. The successful
`/chat` response retains exactly its original fields; read estimates at `/usage`.
Failed provider attempts consume no estimated tokens or cost in this mock.

Before routing, SQLite reserves input tokens plus the largest possible simulated
output among all three providers. This small conservative bound ensures failover
cannot exceed the quota, even with concurrent requests. A prompt that cannot fit
this bound is rejected with HTTP 429 and `code: quota_exhausted`. A successful
request releases unused reservation and records actual estimates; an all-provider
failure releases the whole reservation and records a failed request. Usage is
attributed to the UTC month in which admission occurred, even if completion
crosses a month boundary. New months start with a fresh allowance, retaining
history in SQLite.

SQLite `BEGIN IMMEDIATE` makes checking limits and inserting the reservation one
indivisible operation across threads and processes using the same database file.
Each operation has its own connection; no SQLite connection is shared between
threads. The database contains customer IDs, admission times, monthly usage,
reservation counts, and outcomes—no keys or prompts. Provider calls run outside
transactions, so slow simulated calls do not block other customers' admissions.
Accounting errors return a safe HTTP 503 without exposing database details.

A process crash may leave a pending reservation. It stays charged against the
month's available quota rather than risk allowing overspending. `/usage` exposes
`pending` and `reserved_tokens` for this reason. There is no automatic expiry or
refund of abandoned reservations in this stage. Rate windows still age out after
60 seconds. Preserve the database on restart; deleting it resets enforcement.
The local demo retains request rows indefinitely; archival and administrative
reservation reconciliation are future work.

### Stage 3 request examples

Run these in the shell holding the generated environment keys, while Stage 3
listens on port 8001:

```bash
# Public health check.
curl -sS http://127.0.0.1:8001/health

# Missing credentials: HTTP 401.
curl -i http://127.0.0.1:8001/chat -H 'Content-Type: application/json' \
  -d '{"prompt":"Hello","provider":"OpenAI"}'

# Successful authenticated request and customer-specific totals.
curl -sS http://127.0.0.1:8001/chat \
  -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain Kubernetes","provider":"OpenAI"}'
curl -sS http://127.0.0.1:8001/usage \
  -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY" | python3 -m json.tool

# A different customer's independent totals.
curl -sS http://127.0.0.1:8001/usage \
  -H "Authorization: Bearer $GATEWAY_BUSINESS_API_KEY" | python3 -m json.tool

# Administrator-only operational metrics.
curl -sS http://127.0.0.1:8001/metrics \
  -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY" | python3 -m json.tool

# Six quick Starter requests: at most five admitted in any rolling minute.
for attempt in 1 2 3 4 5 6; do
  curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8001/chat \
    -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"prompt":"Rate demo","provider":"OpenAI"}'
done

python3 -m unittest discover -v
```

### Stage 3 troubleshooting

- **401:** check that the same environment keys are available to the server and
  curl shell, use `Authorization: Bearer`, and use the correct customer/admin
  role. Restart Stage 3 after changing its key environment. Never paste keys
  into issue reports or debug logs.
- **429 / rate_limit_exceeded:** wait the number of seconds in `Retry-After`.
  A rolling 60-second window persists across restarts and isolates customers.
- **429 / quota_exhausted:** inspect `/usage`, shorten the prompt, or wait for
  the next UTC calendar month. Reservations reduce `remaining_tokens` until
  settled. A prompt can exceed the remaining Starter allowance.
- **503 / accounting unavailable:** verify the database directory exists and is
  writable, and avoid holding external SQLite write transactions. SQLite waits
  up to five seconds for locks, then fails safely before granting admission.
- **503 / all providers unavailable:** follow the Stage 2 simulation recovery
  steps above, with the customer bearer header and Stage 3 port.

The full suite includes the original validation and reliability coverage plus
authentication, role separation, environment configuration, same-tier customer
isolation, exact rate boundaries, all tier limits, month rollover, quota
exhaustion, persistence, concurrent admissions, usage estimates, failed-request
refunds, and authenticated failover. It uses no real network calls or paid APIs.
