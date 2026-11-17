# Final engineering review

Reviewed application revision `e139b8db9dd135ebc5ab5491d0b6f0a9189d0ee5` in the local checkout. The repository’s README, API, architecture, deployment guide, and inference runbook supply the requirements baseline available for this review. The implementation meets the tested single-host, simulated-provider requirements. Production exposure and multi-host scaling remain outside that validation.

## Implemented requirements

| Requirement | Implementation and evidence |
| --- | --- |
| Asynchronous job API | `POST /jobs` returns 202 and a status URL. Customer-scoped endpoints read status and results and cancel queued or running jobs. Pending results return 202 with `Retry-After`. |
| Tenant isolation and admission | Customer bearer credentials scope jobs and idempotency keys. Replays return the existing job, changed payloads return 409, and the same key can be used by a different tenant. Global and per-tenant active limits return 429 with `Retry-After`. The synchronous and asynchronous paths share rate and token quotas. |
| Scheduling and concurrency | Configurable worker slots process independent jobs concurrently. Eligible jobs are ordered by priority, with aging for continuously ready jobs. Running jobs carry renewable leases. |
| Recovery and terminal outcomes | A supervisor replaces exited worker threads. Expired claims are recovered during polling after thread or process loss. Failed jobs retry with capped exponential backoff and move to durable dead letter after exhausting attempts. |
| Accounting and cancellation | Admission reserves usage once. Successful settlement commits the result and token charge atomically. Retry retains the reservation. Cancellation or dead letter releases it. Lease fencing rejects stale renewals and completions. |
| Reliability and operations | Workers reuse simulated provider timeouts, retries, failover and circuit breakers. Authenticated Prometheus metrics cover queue states, transitions, capacity, ready age and worker counts. Grafana provisions six inference panels. The repository includes Docker and CI checks, an API contract, fault injection and a runbook. |

[API contract](docs/api.md) · [Architecture](docs/architecture.md) · [Inference runbook](docs/runbooks/inference.md)

## Architecture and correctness review

The threaded HTTP gateway and supervised worker pool share a SQLite database containing job state, usage reservations and durable event counts. The queue serializes local write transactions with a process-local lock and uses SQLite’s `BEGIN IMMEDIATE` across connections and processes. Each claim increments the attempt count and issues a fresh token. The heartbeat renews at one third of the lease interval. State changes are committed together with the relevant accounting and event updates. Cancellation and completion compete for the same write lock, so whichever commits first determines the outcome.

The concurrent first-submit and replay tests found one reservation per accepted idempotency key. The live burst verified one settlement per job and zero reserved tokens after drain. The crash and cancellation checks rejected stale outcomes. Provider calls can repeat after lease loss or across retries. Durable accounting charges the winning completion once, while the in-memory token metric can lose an increment if the process exits after the SQLite commit.

Tenant predicates, separate customer/admin/scrape credentials, bounded request bodies and normalized metric labels limit cross-tenant access and disclosure. Prompts and results are retained in SQLite. The queue has no retention policy, so terminal jobs, idempotency records and usage history can grow indefinitely. SQLite connections close through context managers and worker heartbeat threads are joined after attempts. No resource leak surfaced in the exercised paths. The review did find the following operational risks by inspection.

| Finding | Effect |
| --- | --- |
| HTTP connections have no application-level concurrency cap or socket read timeout | Slow clients can retain handler threads before queue limits apply. The listener backlog limits pending accepts only. Keep this demo behind an ingress that enforces connection and request deadlines. |
| Shutdown checks the worker stop flag before entering `claim()` and then joins workers without a deadline | A worker already waiting to claim can acquire work during shutdown. A blocked provider attempt can delay exit until the container’s stop deadline. The tested finite simulated attempts drained successfully. |
| Lease recovery uses wall time and runs only during worker polling | A clock step or sustained write contention can cause premature or delayed recovery. API-only mode cannot recover leases on its own. Cancellation and lease loss fence writes but leave an in-flight provider call running. |
| An unexpected simulator exception can bypass half-open probe cleanup | The router handles expected provider failures, but an unexpected exception from a half-open probe can leave that process skipping the provider until restart. This path was identified by inspection. |
| Demo credentials and storage have a narrow operating model | The environment exposes one configured customer identity per tier. Direct HTTP has no TLS. SQLite stores prompt and result text, and `/health` checks HTTP liveness without testing storage or worker progress. |

## Fresh automated and integration results

| Command or check | Observed result |
| --- | --- |
| `python3 -m unittest discover -v` | 83 passed in 11.597 seconds, zero skips. |
| `python3 -u scripts/validate_inference.py` | All live scenarios passed in 15.781 seconds. The initial sandboxed attempt could not open a socket. The run with loopback access passed. |
| Concurrent HTTP admission and drain | 100 submissions across three tenants accepted in 1.065 seconds. Five workers drained all 100 on their first claim. Settled estimates were 1,170 Enterprise, 540 Business and 90 Starter tokens. |
| Replay, isolation and backpressure | Concurrent first-submit deduplication and replay, altered-payload conflict, cross-tenant access denial, queue-full responses and cancellation fencing all passed. |
| Recovery and scheduling | Worker crash, stale-token rejection, SIGKILL and restart, transient 503 retry, provider failover, durable dead letter, concurrent execution, graceful drain, and priority aging all passed. |
| Compilation and dashboard validation | `python3 -m compileall -q gateway scripts tests` passed. `python3 scripts/validate_incident_dashboard.py` confirmed 18 unique panels and all three incident metrics. The live harness checked the six inference panels. |
| Prometheus exposition | Prometheus 3.7.3 `promtool check metrics` accepted both the live and post-restart scrapes. |

## Docker validation

Docker Engine 29.9.3 built the current application image locally. All 30 checks in the isolated `scripts/ci_container_smoke.py` run passed. The run used four isolated containers and a disposable named volume and tested non-root execution, health, endpoint authentication, read-only root, no published ports, job completion and accounting, persistence across restart and replacement, and SIGTERM handling. The base Compose configuration passed `docker compose -f compose.yaml config --quiet`.

Live Grafana and Kubernetes deployments, real providers, sustained load, and hosted CI were outside this review. Historical monitoring and infrastructure runs are recorded in the validation log. [Validation log](docs/validation.md)

## Production scaling

- Put the API behind a bounded HTTP server and TLS ingress. Set concurrency and request deadlines, provision tenant credentials with rotation, and restrict metrics and admin traffic to their own access paths.
- Separate API and worker deployments. Move claims and accounting to a transactional shared store, initially keeping queue state and the ledger in the same database. Use database time for leases, row-level claims, and a recovery process independent of worker activity. If introducing a broker, use a transactional outbox and idempotent consumers.
- Set an end-to-end execution deadline and bounded shutdown budget. Retry transient failures with jitter, enforce per-provider concurrency limits, and propagate a stable upstream idempotency key where supported. Monitor lease expiry, saturation, and dead letters. Scale workers on ready age and completion rate.
- Define retention for results, ledger data and idempotency keys before pruning records. Encrypt storage and backups, verify restoration, and add dependency and image scanning. Load-test mixed tenants and long-running jobs under storage, clock and provider failures before setting capacity targets.

## Exact application startup commands

From the repository root, run in Bash with Python 3.10 or later. These commands use disposable demo credentials and a fresh loopback-only instance.

```bash
mkdir -p tmp
run_dir=$(mktemp -d "$PWD/tmp/final-review.XXXXXX")
export GATEWAY_BUSINESS_API_KEY=final-review-business-demo
export GATEWAY_METRICS_API_KEY=final-review-metrics-demo
python3 -m gateway.server --host 127.0.0.1 --port 8010 \
  --usage-db "$run_dir/usage.sqlite3" --workers 2
```

From a second terminal, check health and submit a job. Use the returned `Location` for status and append `/result` for the result. Stop the server with Ctrl+C.

```bash
curl -fsS http://127.0.0.1:8010/health
curl -fsSi http://127.0.0.1:8010/jobs \
  -H 'Authorization: Bearer final-review-business-demo' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: final-review-first-job' \
  -d '{"provider":"OpenAI","prompt":"hello","priority":7}'
```

To run the base Compose service from the same checkout:

```bash
export GATEWAY_STARTER_API_KEY=final-review-starter-demo
docker compose -f compose.yaml -p ai-inference-final-review up --build
```

The Compose service binds to `http://127.0.0.1:8002`, stores SQLite data in a named volume, and does not forward the metrics credential. [Deployment guide](docs/deployment.md)

## Git verification

The review began on `master` with a clean worktree and 11 reachable commits. `e139b8d` implements asynchronous inference on top of the seven staged gateway commits and subsequent CI and portfolio changes. No remotes were configured. `git fsck --full` reported no integrity errors and one pre-existing dangling blob. `git ls-files -ci --exclude-standard` found no tracked paths matching ignore rules. The [publication audit](docs/publication-audit.md) records historical private context and author metadata that remain in the local history.

The review was recorded in this report. No source changes, history rewrite, remote operations, or changes to the original project were made.
