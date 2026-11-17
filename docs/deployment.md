# Installation and deployment

## Local Python

Use Python 3.13 to match CI. There are no pip runtime dependencies. Clone the repository, create a virtual environment, export at least one customer key, and run the quick start in the [README](../README.md). Bind to loopback and keep the SQLite file in an ignored, private location. Keys load at startup; restart after rotation. The optional Business and Enterprise variables follow the same `GATEWAY_<TIER>_API_KEY` pattern. Each key also supports a `_FILE` variant, mutually exclusive with its environment value.

For repeatable failure demonstrations, create an ignored `simulation.json` containing `{"OpenAI":["503"],"Anthropic":["success"]}` and start with `--simulation-file simulation.json`. Use `--config` for startup provider policy overrides; see [architecture](architecture.md).

## Docker and Compose

Prerequisites: Docker Engine and Compose v2 or compatible. Export a generated Starter key as shown in the README.

```bash
docker compose config --quiet
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8002/health
python3 docker_smoke.py
docker compose stop
```

`docker_smoke.py` makes a synthetic request and restarts/recreates the Compose gateway to verify persistence. Run it only against a demonstration stack you can interrupt. Compose keeps the named usage volume; do not use `down -v` if you need accounting records. Environment-backed keys can be viewed by Docker administrators; monitoring Compose instead mounts key files.

### Inference workers in the image

The image runs the gateway as UID and GID 10001 with an exec-form command.
The command binds port 8000 inside the container and stores both usage and
inference state in `/data/usage.sqlite3`. A newly initialized named volume
inherits the image directory's ownership. For a bind mount, provision a
private directory writable by UID 10001 before startup.

The default command starts two inference workers in the gateway process.
To override the count, replace the image command with
`python -m gateway.server --host 0.0.0.0 --port 8000 --usage-db /data/usage.sqlite3 --workers N`.
Use `0` for API-only mode or `1`–`64` to run workers alongside HTTP.
The same command accepts the queue, retry, and lease flags described in the
[inference runbook](runbooks/inference.md). The image health check probes
`/health`. Use authenticated Prometheus worker and queue metrics to verify
processing. When increasing provider timeouts or retries, set the container
stop grace period long enough for active jobs to finish. SIGTERM signals workers to stop
claiming and gives active workers time to settle. A claim already entering
a transaction can still finish acquisition. Queued work stays
in SQLite.

To test a newly built image without interrupting Compose, use the
[isolated Ubuntu smoke commands](validation.md#repeat-the-container-smoke-on-ubuntu).

The CI smoke test creates separate containers with no published host ports and a unique volume:

```bash
docker build -t ai-gateway:stage5 .
python3 scripts/ci_container_smoke.py
```

It removes the test containers and volume when the run finishes. The stage4/stage5/stage6 image tags and Compose project names in existing assets are development identifiers, not released versions.

## Kubernetes prerequisites

Use a test cluster, kubectl, Helm 3, and a CSI driver/storage class supporting ReadWriteOncePod (Kubernetes 1.29+ feature stability; supplied schemas target 1.34). No cluster is provisioned by this repository. Confirm your current kubectl context before applying resources. Application state requires one replica and Recreate updates; expect downtime during replacement.

Build an image and make it available to the cluster. For a local kind cluster:

```bash
docker build -t ai-gateway:stage5 .
kind load docker-image ai-gateway:stage5
kubectl create namespace ai-gateway
```

Alternatively, build and upload an image to your own registry as a separate authorized operation and set the repository/tag in reviewed manifests or Helm values. These instructions do not publish an image.

Create an ignored private key file, then a namespace-scoped Secret. Never fill credentials into the tracked Secret example.

```bash
mkdir -p secrets
chmod 700 secrets
python3 - <<'PY'
import os, secrets
with open('secrets/kubernetes.env', 'x', opener=lambda p, f: os.open(p, f, 0o600)) as out:
    for role in ('STARTER', 'ADMIN', 'METRICS'):
        out.write(f'GATEWAY_{role}_API_KEY={secrets.token_urlsafe(32)}\n')
PY
kubectl -n ai-gateway create secret generic ai-gateway-keys --from-env-file=secrets/kubernetes.env
```

This refuses to overwrite an existing local key file. Keep keys in a secret manager for long-lived environments. Configure cluster Secret RBAC and encryption at rest separately. Values are loaded through `envFrom` at process start; restart the deployment after rotating the Secret.

## Raw manifests

Inspect the default storage class. If necessary, set a supported `storageClassName` in `k8s/pvc.yaml` before applying. Raw manifests reference `ai-gateway:stage5`.

```bash
kubectl -n ai-gateway apply -f k8s/
kubectl -n ai-gateway rollout status deployment/ai-gateway --timeout=120s
kubectl -n ai-gateway get pods,pvc,service
kubectl -n ai-gateway port-forward service/ai-gateway 8000:8000
```

The empty `secret.yaml.example` is not applied by the directory command. Use another shell for health and authenticated API requests. Stop port forwarding with Ctrl-C. Do not delete the namespace or PVC to stop the service; scaling to zero preserves data. Raw PVCs have no Helm retention annotation.

## Helm

Use either raw manifests or Helm in a namespace, not both with the same resource names. For Helm, start with a fresh namespace/Secret as above and let the chart create its own PVC.

```bash
helm lint charts/ai-gateway --strict
helm template ai-gateway charts/ai-gateway
helm upgrade --install ai-gateway charts/ai-gateway \
  --namespace ai-gateway --set existingSecret=ai-gateway-keys \
  --set image.repository=ai-gateway --set image.tag=stage5 \
  --wait --timeout 2m
kubectl -n ai-gateway port-forward service/ai-gateway 8000:8000
```

Set `storage.storageClassName` if the default is unsuitable. Preserve reviewed non-secret overrides in a values file and reuse it on upgrades. The schema rejects multiple replicas, invalid ports and API-key environment overrides. Back up SQLite consistently before migrations or upgrades; an image rollback does not roll back database contents.

```bash
helm -n ai-gateway history ai-gateway
# Replace 1 with a reviewed compatible revision
helm -n ai-gateway rollback ai-gateway 1 --wait --timeout 2m
```

The Helm PVC is annotated `helm.sh/resource-policy: keep`. Uninstalling leaves storage; do not delete retained claims without a backup and an explicit data-deletion decision. Plan claim reuse before reinstalling. No automatic backup, migration service, ingress or TLS certificate is provisioned.

## Prometheus and Grafana

The monitoring Compose stack is separate from the basic gateway. Setup is idempotent for credentials and creates ignored runtime directories. It binds gateway 8003, Prometheus 9090 and Grafana 3000 to loopback.

```bash
python3 scripts/setup_monitoring.py
docker compose --env-file .env.monitoring -f compose.monitoring.yaml config --quiet
docker compose --env-file .env.monitoring -f compose.monitoring.yaml up -d --build
python3 scripts/check_monitoring.py
```

Visit Grafana at `http://127.0.0.1:3000` as `admin`, using the generated `secrets/monitoring/grafana` password privately. The provisioned AI Gateway dashboard uses the Prometheus datasource. Prometheus target status is at `http://127.0.0.1:9090/targets`. Both UIs are demonstration services; no public ingress is configured. Alert rules evaluate in Prometheus; Alertmanager and external notifications are not included.

```bash
docker compose --env-file .env.monitoring -f compose.monitoring.yaml run --rm --no-deps \
  --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
docker run --rm --entrypoint promtool -v "$PWD/monitoring:/etc/prometheus:ro" \
  -w /etc/prometheus prom/prometheus:v3.7.3 check rules alerts.yml
docker run --rm --entrypoint promtool -v "$PWD/monitoring:/etc/prometheus:ro" \
  -w /etc/prometheus prom/prometheus:v3.7.3 test rules alerts.test.yml
docker compose --env-file .env.monitoring -f compose.monitoring.yaml stop
```

Traffic demonstrations can be run with `python3 scripts/simulate_traffic.py --help`; inspect the scenario and use only the isolated monitoring stack. Metrics include HTTP counts and latency, provider attempts/failures/retries/failovers, circuit states, estimated tokens, security rejections and incident state. HTTP and routing counters reset with the gateway process. Durable queue transition counters remain in SQLite. Prometheus retains two days or 256 MB, whichever storage constraint applies. The Compose deployment does not install monitoring into Kubernetes.

## GitHub Actions and offline validation

CI currently targets `master` pushes and pull requests. If the destination default branch is different, update both workflow branch filters. Actions are SHA-pinned with read-only repository permissions and checkout credential persistence disabled. CI performs Python tests, the live asynchronous inference validator, compile checks, dashboard validation, detection-only incident checks, image build, container smoke tests, checksum-verified tool installation, infrastructure validation, alert checks and monitoring integration. It does not publish images or deploy a cluster. A workflow file alone is not evidence of a successful hosted run.

```bash
bash scripts/install_ci_tools.sh
python3 -m pip install PyYAML==6.0.3
python3 scripts/validate_infra.py
```

Tool installation is pinned for Linux amd64. Infrastructure validation needs network access to Kubernetes schemas and validates rendered configuration, not cluster scheduling or runtime storage behavior.

## Upgrade and rollback

1. Pause submissions. Drain active jobs if the deployment can wait. Record
   outstanding job IDs and queue state before stopping the gateway.
2. Stop writers or use SQLite's online backup API to take a consistent,
   private backup. Keep the existing volume and tenant credentials.
3. Upgrade one replica with a Recreate rollout. Allow time for a graceful
   stop based on the provider retry and failover budget. Startup initializes
   the job tables alongside the existing usage data.
4. Confirm health, worker slots, authenticated submission and polling, result
   settlement, and queue progress. Check the [inference runbook](runbooks/inference.md)
   for lease recovery and backpressure.
5. Review database compatibility before rolling back the image. A synchronous
   gateway cannot process queued jobs. Retain a backup of the upgraded state
   before restoring an older snapshot, and reconcile any work and usage
   recorded after the snapshot.
