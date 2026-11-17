# Changelog

Changes are grouped by capability. No release tags or deployed versions are implied.

## Unreleased

- Replaced working notes with a portfolio README, local project banner, technology badges, a live CI status badge and Mermaid diagrams.
- Added architecture, API, deployment, validation, security and incident documentation.
- Added screenshot capture guidance and a redacted publication audit.
- Removed unrelated working context from current documentation and neutralized a validation scenario name.

## Existing implementation

- Simulated provider gateway and JSON HTTP validation.
- Bounded retry, deterministic failover and per-provider circuit breakers.
- Tiered bearer identities, transactional SQLite quota and usage tracking.
- Non-root Docker packaging and persistent local Compose deployment.
- Single-replica Kubernetes manifests, Helm schema and GitHub Actions verification.
- Authenticated Prometheus metrics, alert fixtures and twelve Grafana panels.
- Deterministic incident detection, revision-bound expiring approvals, allowlisted recovery and durable audit records.
