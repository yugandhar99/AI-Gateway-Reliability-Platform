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

## Asynchronous inference

The job endpoints use the same customer bearer keys and tier limits as
`POST /chat`. The bearer key determines the tenant. Administrator and
Prometheus keys cannot access jobs. A job ID belonging to another tenant
returns the same 404 as an unknown ID.

| Method and path | Response |
| --- | --- |
| `POST /jobs` | 202 for a new job, 200 for an idempotent replay. Both return job metadata and a `Location` header. |
| `GET /jobs/{id}` | 200 with job metadata, including terminal states. |
| `GET /jobs/{id}/result` | 200 with `{"id":"…","result":{…}}` after success. Returns 202 with job metadata and `Retry-After: 1` while queued or running, or 409 with job metadata after cancellation or dead-lettering. |
| `DELETE /jobs/{id}` | 200 with cancelled metadata for a queued, running, or already cancelled job. Returns 409 if the job already succeeded or entered the dead-letter queue. |

Submit JSON with the same `prompt` and `provider` fields as chat, plus an
optional integer `priority` from 0 through 9. The default is 5 and 9 has
highest priority. Other fields return 400. For example:

```json
{"provider":"OpenAI","prompt":"Summarize the incident","priority":7}
```

The optional `Idempotency-Key` header accepts 1–128 characters from
`A–Z`, `a–z`, `0–9`, `.`, `_`, `~`, and `-`. A replay with the same
tenant, trimmed prompt, canonical provider, and effective priority returns
the original job's current metadata. Sending a different request with the
same key returns 409. Without a key, each accepted submission creates a
job. Replays bypass admission limits, including when the original job is
terminal. Use the original key after an ambiguous submission response.

Metadata includes `id`, `status`, requested `provider`, `priority`,
`attempts`, `max_attempts`, `created_at`, `updated_at`, `available_at`,
and `error`. Timestamps are Unix seconds and `available_at` is the eligibility
time of the latest queued attempt. `error` is null or a fixed category such
as `providers_unavailable`, `lease_expired`, or `execution_error`. The success
result uses the chat response schema and names the provider that actually
responded after failover.

New admissions can return 429 with `code` set to `tenant_queue_full` or
`queue_full` and `Retry-After: 1`. Existing request-rate and token-quota
limits apply as well. SQLite failures return 503. Cancellation durably
discards the active claim and releases its reservation. Simulated work
already underway may continue, and its result and token charge are fenced
out if cancellation wins the transaction.
