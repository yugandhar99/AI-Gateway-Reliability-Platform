# Development and troubleshooting

The application uses the Python standard library. Use Python 3.13 to match CI; no runtime package installation is required. Create a virtual environment and generated credentials using the [quick start](../README.md#quick-start).

## Local verification

```bash
python3 -m unittest discover -v
python3 -m compileall -q gateway scripts tests
python3 scripts/validate_incident_dashboard.py
# Choose a fresh directory for each isolated demonstration.
python3 scripts/simulate_incident.py --directory monitoring/runtime/development-detection
```

The incident script starts a loopback HTTP server and preserves scenario and audit data in an ignored directory. Its default mode verifies detection and rejects execution without approval. The interactive recovery mode requires an operator; see the [runbook](runbooks/incidents.md).

Infrastructure checks require Helm 3.19.0, kubeconform 0.8.0, PyYAML 6.0.3 and network access to Kubernetes schemas. The tool installer downloads checksum-verified Linux amd64 binaries into the ignored `.tools/` directory.

```bash
bash scripts/install_ci_tools.sh
python3 -m pip install PyYAML==6.0.3
python3 scripts/validate_infra.py
```

Container, alert and monitoring checks are documented in the [deployment guide](deployment.md). Test results and their limits belong in the [validation record](validation.md).

## Routing and multi-tenant security

The requested provider is tried first, followed by the remaining providers in registry order. Defaults are a 0.2-second simulated timeout, one retry, three consecutive failures to open a circuit, and a five-second cooldown. After cooldown, a single half-open probe may run; success closes the circuit, failure reopens it. Exhausting all providers returns HTTP 503.

| Tier | Requests per rolling minute | Estimated tokens per UTC month |
|---|---:|---:|
| Starter | 5 | 10,000 |
| Business | 30 | 100,000 |
| Enterprise | 100 | 1,000,000 |

The environment registry creates one demo identity per configured tier. Accounting is scoped to that identity, with reservation before routing and settlement afterward. Failed admitted requests consume rate capacity but settle without token charges. Tokens use a four-character estimate; the $1/million-token cost model is illustrative, unrelated to provider billing. This is a multi-tenant demonstration, with no user onboarding or identity provider integration.

## Troubleshooting

| Symptom | Action |
|---|---|
| Startup requires a customer key | Set at least one `GATEWAY_<TIER>_API_KEY`; keys must be distinct ASCII strings without whitespace |
| HTTP 401 | Match the credential role; customer, admin and metrics credentials are separate |
| HTTP 429 | Inspect quota/rate data through `/usage`; respect `Retry-After` when present |
| HTTP 503 | Inspect admin `/metrics`, simulation configuration, and database permissions |
| Prometheus target down | Check gateway reachability and the dedicated metrics credential file |
| Grafana rejects changed password | Initial password is persisted; use Grafana's password reset procedure |
| Pod pending | Check CSI support for ReadWriteOncePod, storage class and PVC binding |
| Image pull failure | Load the local image into the cluster or supply an accessible image repository/tag |
| Incident stays verifying | Wait for old failures to age out; send five fresh successful chats and verify explicitly |
