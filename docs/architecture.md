# Architecture and design

The HTTP adapter (`gateway/server.py`) validates methods, content type, body size (65,536 bytes) and JSON before dispatch. Chat validation limits prompts to 10,000 characters. Public routes expose health and the simulated provider catalog. Customer, admin and scrape routes use separate bearer roles.

`security.py` hashes configured keys in memory and compares digests with constant-time comparison. The environment registry maps one configured key per tier to a fixed demo customer ID. `usage.py` uses SQLite `BEGIN IMMEDIATE` to reserve token capacity atomically and settle each admission once. Prompts and keys are not persisted by accounting; the database does contain customer IDs, timestamps, counters, process metadata and incident evidence and must remain private.

`reliability.py` keeps policy and per-provider state in one process. The preferred provider precedes remaining registry entries. Attempts are bounded by `max_retries + 1`; failures count per attempt. Locks prevent simultaneous half-open probes. Generation checks keep stale in-flight outcomes from overwriting newer circuit state. Retries have no backoff. The timeout is a simulated sleep and failure, not a real HTTP deadline.

```mermaid
stateDiagram-v2
  Closed --> Open: Consecutive failures reach threshold
  Open --> HalfOpen: Cooldown expires on a new request
  HalfOpen --> Closed: Probe succeeds
  HalfOpen --> Open: Probe fails
```

Live simulation files accept provider names with outcome sequences: `success`, `503`, or `timeout`. The final outcome repeats. Updating the JSON resets sequence positions. Missing/unreadable or invalid configured files produce a fixed simulation error; no upstream traffic occurs.

Example startup policies (`--config`, loaded once):

```json
{"OpenAI":{"timeout_seconds":0.2,"max_retries":1,"failure_threshold":3,"recovery_seconds":5}}
```

Example live scenario (`--simulation-file`):

```json
{"OpenAI":["503","503","success"],"Anthropic":["success"]}
```

`observability.py` records bounded route/method/status/provider labels without prompt, bearer or customer labels. Prometheus scrape authentication is independent of admin authentication. Incident evaluation runs after chats, on authenticated scrapes, or through the explicit evaluate endpoint; it is not a background scheduler. Incident state persists in the accounting database, while recent telemetry is process-local.

The incident engine correlates one active routing incident, rather than creating an incident per request. Recommendations are fixed allowlisted actions. Approvals bind incident revision, action and expiry, and execution is durably claimed before side effects. Interrupted execution is not automatically replayed. Recovery requires a separate verification step.

## Deployment boundaries

Kubernetes and Helm intentionally require a single replica, a Recreate strategy and ReadWriteOncePod storage. This avoids unsupported distributed circuit/metric state and concurrent deployments against the same local database. The chart rejects multiple replicas; its PVC has a keep annotation. Raw manifests have no Helm retention behavior. `/health` is used for probes but does not establish upstream or database readiness.

Containers run as UID/GID 10001. Kubernetes drops capabilities, disables privilege escalation and service-account token mounting, enables RuntimeDefault seccomp and mounts a writable data volume under a read-only root. Compose monitoring uses file-mounted secrets and loopback host bindings. These controls do not provide TLS, encrypted secrets, tenant-specific network isolation, or production traffic hardening.

## Asynchronous inference

```mermaid
flowchart LR
    C[Customer client] -->|Bearer key| H[Job HTTP API]
    H -->|Submit, read, cancel| Q[(SQLite)]
    Q --- J[Job state, leases, and dead letters]
    Q --- U[Shared quota and usage ledger]
    W[Concurrent worker threads] -->|Claim, heartbeat, settle| Q
    W --> R[Existing router and simulated providers]
    P[Prometheus] -->|Dedicated bearer key| M[Metrics endpoint]
    M --> Q
    M --> W
    G[Grafana] --> P
```

```mermaid
stateDiagram-v2
    [*] --> queued: Submit and reserve usage
    queued --> running: Claim with fresh lease token
    running --> running: Renew live lease
    running --> succeeded: Commit result and charge
    running --> queued: Failure or expired lease, attempts remaining
    running --> dead_letter: Failure or expired lease, attempts exhausted
    queued --> cancelled: Cancel and release
    running --> cancelled: Cancel and fence claim
```

SQLite serializes submissions, claims, and terminal transitions using
`BEGIN IMMEDIATE`. Each claim increments the durable attempt count and
assigns a fresh token. Heartbeats run every third of a lease. Worker
heartbeats and completions require a matching token on a running job
with a lease still live when the transaction checks it. Workers recover
expired leases while polling, including after a full process restart.
A claim lost before it calls a provider still uses an attempt.

Execution can happen more than once if a lease expires while a worker is
still executing. A winning success commits its result and token charge
in one transaction. Each accepted submission reserves usage once and has
at most one settled charge. Retries reuse the same reservation and
admission-month attribution. Cancellation and dead-lettering settle one
failed request with zero charged tokens. All accepted submissions count
toward the request rate limit. Rejected submissions and idempotent replays
consume neither requests nor tokens. The process-local token metric can
miss a committed settlement if the process exits immediately after commit.
Use `/usage` for the persisted ledger.

A failed attempt is eligible again after
`min(retry_cap, retry_base × 2^(attempts−1))` seconds. Lease-expiry backoff
starts at recovery time. An attempt can include router-level retries and
provider failover. Eligible jobs are ordered by priority, then by
eligibility time. Once a job has been eligible for the aging interval, it
outranks every unaged job and competes by eligibility time. This prevents
new arrivals from repeatedly overtaking it while workers make progress.
Running jobs and delayed retries count toward admission capacity.

The queue and usage ledger share one local SQLite file. Run on a persistent
local volume with synchronized system time. SQLite serializes writes and
this deployment targets a single host. Terminal jobs and idempotency
keys are retained indefinitely. Capacity bounds active jobs, while database
size grows with request history. The dead-letter queue is a durable terminal
state retrievable through the same tenant job endpoints. There is no replay
endpoint. To rerun a dead letter, submit with a fresh idempotency key.

### Submission and settlement

```mermaid
sequenceDiagram
    participant C as Customer
    participant A as HTTP API
    participant D as SQLite queue and ledger
    participant W as Worker
    participant R as Provider router
    C->>A: POST /jobs with bearer and idempotency key
    A->>D: Check replay, capacity and quota
    alt New job admitted
        D-->>A: Commit job and reservation
        A-->>C: 202 with Location
    else Matching replay
        D-->>A: Original job metadata
        A-->>C: 200 with Location
    else Queue at capacity
        A-->>C: 429 with Retry-After
    end
    W->>D: Claim next eligible job and receive lease token
    W->>R: Execute a simulated attempt
    W->>D: Renew lease while attempt runs
    R-->>W: Result or failure
    W->>D: Verify live token and settle or reschedule
    C->>A: GET job result
    A->>D: Read tenant-scoped state
    A-->>C: 200 result, 202 pending, or 409 terminal failure
```

A supervisor replaces a worker thread that exits. Its abandoned claim
remains running until expiry and polling recovery. Recovery writes a durable
transition and either reschedules the job or releases the reservation into
dead letter. A worker using the expired claim cannot publish a late outcome.
