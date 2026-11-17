# Changelog

## v2.0.0

- Added asynchronous job submission, status, result retrieval, and cancellation alongside synchronous chat.
- Added persistent SQLite priority scheduling with aging, configurable worker concurrency, renewable leases, expired-claim recovery, and stale-worker fencing.
- Added tenant-scoped idempotency and transactional job admission, result settlement, and quota release. Retries retain the original reservation.
- Added capped exponential retries, durable dead-letter state, and tenant and global queue backpressure.
- Added six inference panels to the Grafana dashboard, bringing the total to 18, and exposed authenticated queue and worker metrics.
- Extended validation to 83 Python tests, 30 isolated Docker smoke checks, and a 100-job, five-worker integration with crash and contention scenarios. CI runs the advanced validator and expanded smoke suite alongside the existing checks.
- Updated the portfolio banner, architecture diagrams, API, deployment and operational documentation. See the [release notes](docs/release-notes/v2.0.0.md).

## v1.0.0 gateway baseline

- Simulated provider routing, bounded retries, failover, and per-provider circuit breakers.
- Tiered bearer credentials and transactional SQLite quota and usage accounting.
- Non-root Docker images, persistent Compose deployment, and single-replica Kubernetes and Helm assets.
- Authenticated Prometheus metrics, alert fixtures, and twelve Grafana panels.
- Deterministic incident detection with expiring, revision-bound approvals and durable recovery audit records.
