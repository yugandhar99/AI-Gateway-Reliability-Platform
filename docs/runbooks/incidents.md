# Incident detection and recovery runbook

## Scope and safety boundary

All provider outages and restoration actions are simulated. The incident engine is deterministic and cannot execute shell commands, call an LLM or modify Kubernetes. Normal operation does not restore mock providers. A shared administrator credential authorizes incident actions; there is no second-operator enforcement.

Keep audit databases private. Preserve state before investigation. Do not delete usage rows, clear circuits forcibly, or replay an approval after an interrupted execution.

## Detect and triage

1. Check public `/health` for process liveness, then admin `/metrics` for actual provider state. A healthy process does not establish healthy providers or storage.
2. Inspect `/incidents` and `/incidents?id=<id>` with the admin bearer. Use `POST /incidents/evaluate` with `{}` if explicit evaluation is needed.
3. Inspect the rolling 60-second evidence window. At least five chats are needed for rate/latency symptoms: server error rate above 10% or P95 above 500ms. Provider unavailability and open circuits can trigger without that minimum.
4. P1 indicates all providers unavailable or severe chat error evidence; P2 indicates degraded provider redundancy or moderate server errors; P3 covers latency without established provider fault. Root-cause summaries are rule-based hypotheses, not externally confirmed diagnoses.
5. Check scenario JSON syntax and readability, provider counters, quota status, database write permissions, and monitoring scrape credentials. Never copy secret values into logs or tickets.

## Approve, execute and verify

List recommendations before selecting an action:

```bash
curl --fail 'http://127.0.0.1:8000/incidents/recommendations?id=1' \
  -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY"
```

Available actions:

| Action | Effect |
|---|---|
| `probe_providers` | Fixed probes through normal routing/circuit cooldowns; fallback can occur |
| `restore_mock_providers` | Process-local success override plus probes; available only with `--enable-mock-remediation` |
| `reconcile_reservations` | Release pending reservations older than 300 seconds only when owner exit is proven in the same host/boot/PID scope |

After a human reviews evidence and chooses `probe_providers`, the API sequence is:

```bash
curl --fail http://127.0.0.1:8000/incidents/approve \
  -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"incident_id":1,"action":"probe_providers"}'
# Substitute the actual returned approval_id and incident_id below.
curl --fail http://127.0.0.1:8000/incidents/execute \
  -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' -d '{"incident_id":1,"approval_id":1}'
curl --fail http://127.0.0.1:8000/incidents/verify \
  -H "Authorization: Bearer $GATEWAY_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' -d '{"incident_id":1}'
```

Approvals expire after 300 seconds and bind the incident revision. Material evidence changes invalidate them. Missing, stale, expired or consumed approvals return 409. Review fresh evidence and obtain a new approval if appropriate; do not bypass the guard.

An executed action enters `verifying`. Wait for old failures to leave the 60-second window, then send at least five fresh successful customer chats. Explicit verification requires no symptoms, no unavailable providers, no fresh errors and fresh P95 at most 500ms. `/health` alone cannot resolve an incident. Persisted audit events capture approval, execution and verification.

## Isolated demonstrations

```bash
# Creates its own loopback server, scenario and SQLite database; rejects unapproved execution.
python3 scripts/simulate_incident.py --directory monitoring/runtime/operator-detection
# Separate run: explicitly prompts a human before enabling the recovery sequence.
python3 scripts/simulate_incident.py --interactive --directory monitoring/runtime/operator-recovery
```

Choose a new directory each run. The interactive path waits for circuit cooldown and an observation window, preserving its scenario and database. Never script an approval response as evidence of human approval.

## Storage or interrupted execution

If storage returns 503, check disk space, ownership and volume attachment. Do not replace the database to make errors disappear. Back up using SQLite's online backup facility or a consistent stopped snapshot. On interrupted incident execution, inspect audit and action effects before making a new decision: durable claims intentionally prevent automatic replay. Reconciliation skips live, unknown, foreign-scope and legacy owners; skipped reservations need investigation, not blanket deletion.

## Close out

Record incident ID, source revision, scenario, timestamps, evidence, approved action and verification outcome with credentials redacted. Recovery of this local simulator is not evidence of a real upstream or cloud recovery. Keep captured screenshots subject to the [screenshot review guidance](../../screenshots/README.md).
