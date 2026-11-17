# AI Model Gateway & Reliability Platform

Stage 5 adds Kubernetes manifests, a Helm chart, and GitHub Actions CI to the containerized
HTTP API that simulates OpenAI, Anthropic, and Gemini.
It uses only the Python standard library. No real provider API keys, paid services, package
installation, or external API calls are needed. Local demo bearer keys are required. Python 3.10+ is recommended.

## Stage 5: Kubernetes, Helm, and CI

Stage 5 changes infrastructure and documentation; the application, Dockerfile,
Compose deployment, and original smoke script keep their existing behavior.
No cluster is installed or contacted by the validation tools. Deployment commands
below are reference commands for a separately authorized, existing test cluster.

### Architecture and reliability decisions

```text
Client -> ClusterIP Service :8000 -> one gateway Pod :8000
                                      |-- ConfigMap (non-secret environment)
                                      |-- existing Secret (demo bearer keys)
                                      `-- PVC -> /data/usage.sqlite3
```

The Service gives the pod a stable internal address. It has no public load balancer
or Ingress. The Deployment replaces failed pods. A PVC (PersistentVolumeClaim)
asks the cluster for a disk that survives pod replacement. The ConfigMap carries
non-secret environment settings; a separately created Secret supplies credentials.
This remains a local learning HTTP server, without TLS or production ingress.

- **One writer:** `replicas: 1` and `strategy: Recreate` are deliberate. A rollout
  terminates the old pod before creating its replacement, causing brief downtime.
  Helm rejects replica counts other than one. Do not add an autoscaler or manually
  scale this service. Multi-replica availability needs a shared server database and
  a review of rate limiting and circuit state first.
- **Volume exclusivity:** the claim uses `ReadWriteOncePod`, which restricts a disk
  to one pod. Ordinary `ReadWriteOnce` only limits attachment to one *node*, so two
  pods on that node could still write together. Recreate covers upgrades;
  ReadWriteOncePod adds enforcement during other pod replacements. Never force
  delete a pod on an unreachable node without first confirming it has stopped.
  See the [Kubernetes rollout documentation](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#recreate-deployment)
  and [volume access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes).
- **Storage prerequisites:** target Kubernetes 1.34+ with a CSI StorageClass that
  supports ReadWriteOncePod and Linux volume permissions (`fsGroup`). The default
  requests 1 GiB using the cluster's default class. Use a filesystem with reliable
  SQLite locking; avoid assuming any arbitrary network filesystem is suitable.
  There is intentionally no fallback to a weaker access mode or ephemeral storage.
- **Permissions:** the pod and image use UID/GID 10001. `fsGroup: 10001` lets a
  compatible CSI volume be writable without a privileged root init container.
  The root filesystem is read-only; only `/data` is mounted writable. All Linux
  capabilities are dropped, privilege escalation is disabled, seccomp uses
  RuntimeDefault, and no Kubernetes API token is mounted.
- **Resources:** requests reserve 100m CPU (one tenth of a core) and 64 MiB RAM;
  limits cap CPU at 500m and RAM at 256 MiB. These are learning defaults to tune
  against measurements, not a capacity guarantee.
- **Probes:** startup allows roughly 60 seconds (30 failures, 2-second intervals).
  Once startup succeeds, readiness runs every 5 seconds and liveness every 10,
  each with a 2-second timeout and 3-failure threshold. Readiness controls routing;
  liveness restarts an unresponsive process. All use public `/health`, which checks
  HTTP responsiveness, not database writes or provider availability. An accounting
  failure can still return 503 while health is green; authenticated smoke tests
  cover writes separately.

### Create credentials safely

Use a dedicated namespace and a new Secret with only this application's keys.
These are mock gateway bearer keys, unrelated to actual AI provider credentials.
Do not edit `k8s/secret.yaml.example`: its empty fields show the structure only.
Do not put keys in Helm values, ConfigMaps, command-line `--set` arguments, or Git.

For an authorized test cluster, create an ignored local file with restrictive
permissions; the secret values will not appear in shell history or command arguments:

```bash
kubectl create namespace ai-gateway
(umask 077; python3 - <<'PYKEYS'
import secrets
with open('.env.kubernetes', 'x') as output:
    for tier in ('STARTER', 'BUSINESS', 'ENTERPRISE', 'ADMIN'):
        output.write(f'GATEWAY_{tier}_API_KEY={secrets.token_urlsafe(32)}\n')
PYKEYS
)
kubectl -n ai-gateway create secret generic ai-gateway-keys \
  --from-env-file=.env.kubernetes
```

The exclusive file creation refuses to overwrite existing keys. `.env.*` is
ignored, and `.dockerignore` excludes credentials from build contexts. Protect
this local file and transfer credentials through your normal secret manager.
Kubernetes Secrets are base64 encoded, not automatically encrypted: cluster
operators should enable encryption at rest and restrict Secret RBAC. Do not dump
Secret YAML or pod environments into logs. At least one customer tier must be
configured; admin credentials must differ from customer credentials.

Environment Secrets load at process start. After updating the Secret through your
approved process, restart the deployment to pick up the new values:
`kubectl -n ai-gateway rollout restart deployment/ai-gateway`. This has downtime.
Helm automatically triggers a replacement when its ConfigMap changes; raw
ConfigMap changes need an explicit rollout restart too.

### Deploy with plain manifests

Choose either plain manifests or Helm in a namespace, since their default names
are the same. Do not install both against the same claim. Build the image using
`docker build -t ai-gateway:stage5 .` and make it available to the existing test
cluster through its approved image-loading process. A host Docker image is not
necessarily visible to cluster nodes. For a remote image, change the Deployment's
image reference to the approved repository and immutable tag or digest. This
assignment never pushes images.

```bash
# Review/edit image and, if needed, storageClassName in k8s/pvc.yaml first.
kubectl -n ai-gateway apply -f k8s/configmap.yaml -f k8s/pvc.yaml \
  -f k8s/service.yaml -f k8s/deployment.yaml
kubectl -n ai-gateway rollout status deployment/ai-gateway --timeout=120s
kubectl -n ai-gateway port-forward --address 127.0.0.1 service/ai-gateway 8003:8000
# In a second terminal:
curl --fail http://127.0.0.1:8003/health
```

The app explicitly binds `0.0.0.0:8000`; the ClusterIP Service forwards its port
8000 to the named container port `http`. Local forwarding uses 8003 to leave the
existing 8000/8001/8002 examples alone. No hostPort or NodePort is used. To change
the raw container port, change both the `--port` argument and `containerPort`;
the Service and probes use the named target. Kubernetes runs its own probes and
ignores the Dockerfile HEALTHCHECK (which stays fixed at 8000 for Compose).

To remove only the raw application while preserving accounting:

```bash
kubectl -n ai-gateway delete -f k8s/deployment.yaml -f k8s/service.yaml -f k8s/configmap.yaml
```

Keep the PVC, namespace, and Secret. Do not use `kubectl delete -f k8s/` or delete
the namespace to stop the app: those can remove storage. A PVC is persistence,
not a backup. Take consistent SQLite backups with the app stopped or SQLite's
backup API; copying an actively written database file alone is not sufficient.

### Deploy with Helm

`charts/ai-gateway` is an application chart. Its schema rejects invalid ports,
empty Secret names, invalid probe timings, and more than one replica. It also
rejects environment names containing `KEY`; credentials belong in the existing
Secret. The chart creates no Secret and never needs your credential values.
See [Helm chart schema validation](https://helm.sh/docs/topics/charts/#schema-files).

```bash
helm lint charts/ai-gateway --strict
helm template ai-gateway charts/ai-gateway --namespace ai-gateway
# Requires the namespace, existing Secret, and image availability described above:
helm install ai-gateway charts/ai-gateway --namespace ai-gateway \
  --set existingSecret=ai-gateway-keys --wait --timeout 2m
helm upgrade ai-gateway charts/ai-gateway --namespace ai-gateway \
  --set image.repository=ai-gateway --set-string image.tag=stage5 --wait --timeout 2m
helm history ai-gateway --namespace ai-gateway
# Select the desired revision shown by history:
helm rollback ai-gateway 1 --namespace ai-gateway --wait --timeout 2m
helm uninstall ai-gateway --namespace ai-gateway
```

Keep your non-secret overrides in a reviewed values file and pass the same
`-f my-values.yaml` on install and upgrade so settings remain deliberate.
Configurable values include image repository/tag/pull policy, existingSecret,
non-secret `env`, CPU/memory resources, `storage.size`, `storage.storageClassName`,
`containerPort`, `service.port`, and each probe's timings. For example,
`--set containerPort=8080,service.port=80` changes the process, named port, Service,
and probe routing together. Container ports must be unprivileged (1024–65535).

`storage.storageClassName: null` omits the field and selects the cluster default;
`""` explicitly requests no class (requires a matching pre-provisioned volume).
Pick the class before installation: PVC class/access modes are immutable. Storage
expansion requires driver support; never shrink a PVC. Rollback restores manifests
and the image reference, not SQLite data, keys, or disk size. Every upgrade and
rollback uses Recreate and can interrupt requests.

The Helm PVC has `helm.sh/resource-policy: keep`; uninstall leaves it and the
externally managed Secret behind. The claim is `<release>-usage`. Reinstallation
with the same release and namespace can encounter the retained claim: inspect its
ownership and data before reuse, and never delete it just to clear an install
error. A differently named release creates a separate empty database. Keep the
release name stable when you intend to preserve usage.

### CI flow and local validation

`.github/workflows/ci.yml` runs for pull requests targeting `master` and pushes to
`master`, the repository's current development branch. Update both filters if the
branch is renamed. It performs:

1. All Python unit tests using Python 3.13.
2. Docker build without registry login or image push.
3. `scripts/ci_container_smoke.py`: 13 checks using generated, disposable mock
   credentials, unique containers and a fresh named volume, with no published host
   ports. Checks include authentication, chat, metrics, security settings, SQLite
   writes, restart, and replacement persistence. Only its newly created containers
   are stopped. Test containers and storage are retained; no prune or delete runs.
   Hosted runners discard their own environment after the job.
4. Checksum-verified Helm 3.19.0 and kubeconform 0.8.0 downloads. Third-party actions
   are pinned to full commit SHAs; token permissions are only `contents: read`,
   checkout credentials are not persisted, and no repository secrets are consumed.
5. Strict Helm lint, default/custom renders, strict Kubernetes 1.34.0 schemas,
   cross-resource wiring/security assertions, and invalid-value rejection checks.
   Unknown schemas fail validation. This uses the maintained
   [kubeconform validator](https://github.com/yannh/kubeconform).

This is CI with deployment preparation. CD to a registry or cluster is deliberately
not enabled. Fork pull requests receive no deployment credentials. Validation
needs public downloads; no real AI APIs or cloud services are called.

To repeat locally on Linux amd64 with Python and Docker already available:

```bash
python3 -m unittest discover -v
docker build -t ai-gateway:stage5 .
python3 scripts/ci_container_smoke.py
bash scripts/install_ci_tools.sh
# Infrastructure checker only; the application itself still has no pip dependencies.
# Use an existing environment with PyYAML 6.0.3, or a project-local virtualenv.
python3 -m venv .venv
.venv/bin/python -m pip install PyYAML==6.0.3
.venv/bin/python scripts/validate_infra.py
```

The original `docker_smoke.py` intentionally operates on Stage 4 Compose and can
restart/recreate it. CI uses the separate isolated script instead. Run the isolated
script only when you intend to create new test resources; it reports their volume
name and retains them. It never touches the existing accounting database.

### Kubernetes troubleshooting

- **Pending PVC/pod:** inspect `kubectl -n ai-gateway get pods,pvc` and
  `kubectl -n ai-gateway describe pvc ai-gateway-usage`. Verify a default (or selected)
  CSI StorageClass exists and supports ReadWriteOncePod. Do not weaken access modes
  to bypass a storage incompatibility.
- **ImagePullBackOff:** check `kubectl -n ai-gateway describe pod <pod-name>`;
  confirm the image/tag exists on the nodes or in their approved registry. A local
  Docker build alone does not load cluster nodes.
- **CreateContainerConfigError:** the existing Secret name/namespace may be wrong.
  Inspect the reference and key names without displaying values.
- **CrashLoopBackOff:** run `kubectl -n ai-gateway logs deployment/ai-gateway --tail=50`
  (or `--previous`). Check missing/duplicate keys and SQLite permission errors.
  Verify the CSI driver honors fsGroup 10001 and the volume directory allows
  SQLite database and journal creation. Do not blindly chown a shared disk.
- **Service unreachable:** inspect `kubectl -n ai-gateway get service,endpointslices,pods`.
  Verify readiness, selectors, port arguments, and local port-forward status.
  ClusterIP is intentionally unreachable directly from outside the cluster.
- **Probe failures:** use pod events and logs to distinguish slow startup, wrong
  port, CPU throttling, and OOMKilled. Tune measured resource/probe settings.
  `/health` returning 200 does not certify database or authenticated request health.
- **Rollout waits on storage:** check the old pod is terminated and the driver has
  detached the disk. Recreate downtime is expected; do not force concurrent mounts.
- **Usage missing:** confirm the same namespace/release/PVC and `/data/usage.sqlite3`
  path. Provider metrics reset on restart; customer accounting should persist.
- **Helm upgrade fails:** use `helm status`, `helm history`, and Kubernetes events.
  Check immutable PVC fields and image availability. Rollback can restore the
  previous application version but is not a database restore.

### Stage 5 verification in this session

- Existing Python suite: **36 passed, 0 failures**.
- Helm strict lint: **passed** (only the optional icon recommendation).
- kubeconform: **17 resource validations passed**, zero invalid/errors/skips:
  four raw objects, one empty Secret example, and three four-object Helm renders.
- Deployment policy checks: **4 scenarios passed** (raw, default Helm, custom
  release/ports/settings, explicit empty storage class).
- Invalid Helm values: **8 rejected as expected**, including replica counts 0/2,
  privileged container port, out-of-range Service port, empty Secret, zero probe
  threshold, API key in env, and zero storage size.
- Python script compilation and shell syntax checks: **passed**.
- Docker build: **blocked** by Docker socket permission denied, including the
  approved escalated attempt. The new 13-check container script is implemented
  but its runtime checks were **not executed** here. Earlier Stage 4 smoke results
  do not establish that Stage 5 has passed container checks.
- No cluster, CSI volume provisioning, actual probes, install/upgrade/rollback,
  or hosted GitHub Actions execution was verified. Schema/render checks cannot
  prove those runtime behaviors. No image was pushed or Git commit created.

## Stage 4: Docker on localhost:8002

Run all commands from this project directory in Ubuntu WSL 2. Docker Desktop
must be running with integration enabled for this WSL distribution. The Stage 2
and Stage 3 servers on ports 8000 and 8001 can stay running.

New files and design choices:

- `Dockerfile` uses the official `python:3.13-slim-bookworm` image. The app needs
  only Python's standard library, so there are no pip dependencies, compiler tools,
  model downloads, or extra health-check packages. The version tag receives upstream
  updates; use an image digest if you later need identical base bytes on every build.
  Only `gateway/` is copied. Python runs directly as user/group 10001, with no root
  startup script. `/data` is owned by that user and has mode 700. A fresh named
  volume inherits these permissions, allowing SQLite to create its database and
  journal files. `EXPOSE 8000` documents the internal port; it does not publish it.
- `.dockerignore` uses an allowlist: only the Dockerfile and gateway Python files
  enter the build context. Git metadata, credential directories, `.env` files,
  local databases and sidecars, caches, tests, and simulation files stay outside.
- `compose.yaml` builds `ai-gateway:stage4`, passes keys at runtime, publishes
  `127.0.0.1:8002:8000`, and mounts the project-scoped named volume
  `ai-gateway-stage4_usage` at `/data`. It drops Linux capabilities and prevents
  privilege escalation. Docker can restart the service unless you explicitly stop it.
  At least the Starter key is required by this Compose configuration; other keys
  are optional. The application itself still supports any configured customer tier.
- `docker_smoke.py` is a manual integration checker. It verifies Docker health,
  the container user, localhost publishing, missing/invalid/valid authentication,
  a simulated chat, and SQLite usage after both restart and container replacement.
  It records one real simulated request in the Stage 4 database and leaves the
  service and volume in place. It does not print credentials.

The container listens on `0.0.0.0:8000` so Docker can forward requests to it.
The host publishes only localhost port 8002. The health check calls `/health`
using Python every 10 seconds, with a 2-second HTTP timeout, a 3-second Docker
timeout, a 5-second startup grace period, and three failed checks before unhealthy.
This endpoint checks HTTP liveness; authenticated chat and SQLite writes are
checked separately by the smoke script. Unhealthy status alone does not trigger
Docker's restart policy. This remains a local learning server using `http.server`;
containerization does not turn it into a public production web server.

### Configure keys, build, and start with Compose

Generate local demo credentials in your current shell. They are unrelated to real
AI provider keys. Keep this shell open for the later commands; do not use `set -x`.
Do not print `docker inspect` or `docker compose config` output: those can contain
runtime environment secrets. Environment variables avoid baking keys into image
layers, but people with Docker daemon access can still inspect them.

```bash
cd ~/projects/ai-platform-mock
umask 077
export GATEWAY_STARTER_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GATEWAY_BUSINESS_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GATEWAY_ENTERPRISE_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export GATEWAY_ADMIN_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker build --tag ai-gateway:stage4 .
docker compose up -d --wait --wait-timeout 60
docker compose ps
curl --fail http://127.0.0.1:8002/health
```

No keys are build arguments or stored in the Dockerfile. Keep the same credentials
when recreating containers. Generating new keys changes credentials but retains
usage for the fixed demo customer IDs. Named-volume storage is separate from all
existing host database files, which are neither copied nor modified.

### Verify and restart

```bash
python3 -m unittest discover -v
python3 docker_smoke.py
docker compose restart gateway
docker compose up -d --wait --wait-timeout 60
```

The smoke script exits with an error if any check fails. If the existing Starter
customer has already reached its five-requests-per-minute limit, wait one minute
and retry. It assumes no other client modifies Stage 4 usage during the check and
that the check does not cross a UTC month boundary. Provider metrics and circuit
state reset on restart; SQLite usage survives. Restarting the same container alone
does not prove volume storage, so the script also recreates the container.

### Equivalent plain Docker run

Use this as an alternative to Compose, after stopping Compose to free port 8002.
The separate named volume below intentionally keeps the two deployment examples
independent. Environment flags pass values from your current shell without putting
their literal values into command history.

```bash
docker compose down
docker volume create ai-gateway-stage4-manual-usage
docker run -d --name ai-gateway-stage4-manual \
  --publish 127.0.0.1:8002:8000 \
  --mount type=volume,source=ai-gateway-stage4-manual-usage,target=/data \
  --env GATEWAY_STARTER_API_KEY \
  --env GATEWAY_BUSINESS_API_KEY \
  --env GATEWAY_ENTERPRISE_API_KEY \
  --env GATEWAY_ADMIN_API_KEY \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --restart unless-stopped ai-gateway:stage4
docker inspect --format '{{.State.Health.Status}}' ai-gateway-stage4-manual
curl --fail http://127.0.0.1:8002/health
docker stop ai-gateway-stage4-manual
docker start ai-gateway-stage4-manual
```

Health status can initially be `starting`; check again after about 10 seconds.

### Stop and cleanup without deleting data

```bash
# Compose: stop, then optionally remove its containers and network; retain volume.
docker compose stop
docker compose down
# Plain Docker alternative: stop and optionally remove only this example container.
docker stop ai-gateway-stage4-manual
docker rm ai-gateway-stage4-manual
# Optional: remove only the Stage 4 image after removing containers that use it.
docker image rm ai-gateway:stage4
unset GATEWAY_STARTER_API_KEY GATEWAY_BUSINESS_API_KEY GATEWAY_ENTERPRISE_API_KEY GATEWAY_ADMIN_API_KEY
```

Use only the cleanup commands for the deployment you created. These commands do
not delete databases or named volumes. Avoid `docker compose down -v`, volume
removal, and Docker prune commands when you want to retain accounting data.

### Troubleshooting

- Docker socket permission denied: run `docker version` in your normal WSL terminal.
  Check Docker Desktop is running and Settings → Resources → WSL Integration is
  enabled for this distribution. Do not make the socket world-writable. If Codex
  alone is blocked, execute the build/start/test commands above manually there.
- Missing key error: export `GATEWAY_STARTER_API_KEY` in the same shell before
  starting. A server without any customer key intentionally refuses to start.
  After changing keys, use `docker compose up -d --force-recreate --wait`.
- Port already allocated: inspect `docker compose ps` and `ss -ltn 'sport = :8002'`.
  Stop only your Stage 4 deployment that owns this port. Leave 8000/8001 alone.
- Startup or unhealthy status: use `docker compose logs --tail=50 gateway` and
  `docker compose ps`. The app omits credentials and prompts from its logs.
  Check the health endpoint with `curl --fail http://127.0.0.1:8002/health`.
- SQLite permission errors: use the fresh named volume from these examples.
  Binding an existing host directory hides the image's `/data` ownership; its
  directory and database must be writable by UID/GID 10001, including journal
  creation. Do not change ownership or delete an existing database blindly.
- Health succeeds but chat fails: `/health` is public. Chat needs a customer
  bearer key and JSON; `/metrics` needs the separate admin key. Rate limits and
  quotas still apply. Use `python3 docker_smoke.py` for the authenticated check.

### Actual Stage 4 verification in Codex

The full host Python suite passed: **36 tests, 0 failures**. The smoke script
passed Python syntax compilation. Docker daemon access returned `permission
denied` both inside the sandbox and on an approved escalated retry. Therefore
the image build, Compose runtime configuration, container startup, health,
container authentication, non-root filesystem access, and volume persistence
could not be verified in this session. The commands above are the exact manual
steps to complete those checks; they are not claimed as passing results.

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
