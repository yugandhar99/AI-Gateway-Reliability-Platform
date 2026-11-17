# Security policy

This is an independent portfolio demonstration with simulated providers. No production security guarantee or supported release window is offered. Fixes are considered against the latest source.

Report vulnerabilities privately through this repository's GitHub Security Advisories reporting feature when enabled. If private reporting is unavailable, use a private channel listed on the maintainer's GitHub profile to request a reporting channel. Do not open public issues containing credentials, customer data or exploit details.

Never commit API keys, `.env` files, local databases, runtime monitoring data, screenshots with secrets, or cloud/tool configuration. Use distinct randomly generated customer, admin and metrics keys. Environment keys and `_FILE` variants are mutually exclusive. Rotate compromised credentials, restart affected processes, and inspect audit records without exposing key values. Kubernetes Secrets require cluster RBAC and encryption-at-rest configuration; base64 is not encryption.

The application does not implement TLS, OIDC, distributed tenant isolation, request read timeouts, or a production concurrency cap. Bind locally for demonstrations; a production adaptation needs an authenticated TLS edge, resource/request limits and additional review. Prompts are echoed in simulated responses. Avoid sensitive inputs even though prompt text is excluded from application accounting and metric labels.

Incident execution is admin-only and allowlisted. Mock restoration requires explicit startup enablement. Shared admin credentials are not individual audit identities or two-person approval. Preserve the SQLite database and never execute arbitrary commands based on incident text.

See [publication audit](docs/publication-audit.md) for unresolved historical privacy findings. Ignore rules prevent accidental additions; they cannot remove existing Git history or protect files from local readers. No license has been granted by this policy.
