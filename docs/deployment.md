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

The CI smoke test creates separate containers with no published host ports and a unique volume:

```bash
docker build -t ai-gateway:stage5 .
python3 scripts/ci_container_smoke.py
```

It retains stopped containers and its volume for review. Remove them manually only after reviewing their records. The stage4/stage5/stage6 image tags and Compose project names in existing assets are development identifiers, not released versions.

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

Traffic demonstrations can be run with `python3 scripts/simulate_traffic.py --help`; inspect the scenario and use only the isolated monitoring stack. Metrics include HTTP counts and latency, provider attempts/failures/retries/failovers, circuit states, estimated tokens, security rejections and incident state. Counters reset with the gateway process. Prometheus retains two days or 256 MB, whichever storage constraint applies. The Compose deployment does not install monitoring into Kubernetes.

## GitHub Actions and offline validation

CI currently targets `master` pushes and pull requests. If the destination default branch is different, update both workflow branch filters. Actions are SHA-pinned with read-only repository permissions and checkout credential persistence disabled. CI performs Python tests, compile checks, dashboard validation, detection-only incident checks, image build, container smoke tests, checksum-verified tool installation, infrastructure validation, alert checks and monitoring integration. It does not publish images or deploy a cluster. A workflow file alone is not evidence of a successful hosted run.

```bash
bash scripts/install_ci_tools.sh
python3 -m pip install PyYAML==6.0.3
python3 scripts/validate_infra.py
```

Tool installation is pinned for Linux amd64. Infrastructure validation needs network access to Kubernetes schemas and validates rendered configuration, not cluster scheduling or runtime storage behavior.
