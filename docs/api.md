# API reference

Base URL: `http://127.0.0.1:8000` for local Python, `:8002` for basic Compose, `:8003` for monitoring Compose. Authentication uses `Authorization: Bearer <key>`. Request bodies require `Content-Type: application/json` and Content-Length; curl supplies length for `-d`. Streaming/chunked request bodies are unsupported.

| Method | Path | Role | Purpose / body |
|---|---|---|---|
| GET | `/health` | Public | Process liveness |
| GET | `/providers` | Public | Provider names and `simulated: true` |
| POST | `/chat` | Customer | `{"prompt":"Hello","provider":"OpenAI"}` |
| GET | `/usage` | Customer | Current identity's UTC monthly accounting |
| GET | `/metrics` | Admin | Provider counters and circuit state |
| GET | `/metrics/prometheus` | Metrics | Prometheus text exposition |
| GET | `/incidents` | Admin | Latest 100 incidents |
| GET | `/incidents?id=1` | Admin | Incident, audit and approval records |
| GET | `/incidents/recommendations?id=1` | Admin | Allowlisted recommended actions |
| POST | `/incidents/evaluate` | Admin | `{}`; returns correlated incident or null |
| POST | `/incidents/approve` | Admin | `{"incident_id":1,"action":"probe_providers"}` |
| POST | `/incidents/execute` | Admin | `{"incident_id":1,"approval_id":1}` |
| POST | `/incidents/verify` | Admin | `{"incident_id":1}` |

Provider names are case-insensitive on input. Fallback can change the provider returned. Illustrative response shape (timing varies):

```json
{"response":"[Simulated OpenAI response] You said: Hello","provider":"OpenAI","status":"success","response_time_ms":0.123}
```

```bash
curl --fail http://127.0.0.1:8000/metrics -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY"
curl --fail http://127.0.0.1:8000/metrics/prometheus -H "Authorization: Bearer $GATEWAY_METRICS_API_KEY"
curl --fail http://127.0.0.1:8000/incidents/evaluate \
  -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' -d '{}'
```

Errors generally contain `status: error` and `error`. Admission failures also include `code`; exhausted routing includes per-provider `providers` reasons. Typical statuses: 400 malformed input, 401 invalid/missing role credential, 404 unknown endpoint, 405 unsupported method, 409 incident/approval conflict, 411 missing Content-Length, 413 oversized body, 415 wrong content type, 429 rate/quota exhausted, and 503 exhausted routing or unavailable storage. Rate rejections supply Retry-After; monthly quota exhaustion does not.

Example missing-credential check:

```bash
curl -i http://127.0.0.1:8000/usage
# HTTP 401 with WWW-Authenticate: Bearer
```

Read the [incident runbook](runbooks/incidents.md) before approving any action. Administrative API authentication represents the operator approval boundary; it does not prove a separate human identity or require a second approver.
