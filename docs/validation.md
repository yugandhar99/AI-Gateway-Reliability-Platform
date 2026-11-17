# Validation record — 2026-11-17

Source baseline: `ab4ada0`, with the portfolio finalization changes in the working tree. Host Python: **3.14.4**; CI and the container target Python 3.13. These are fresh local results, separate from hosted GitHub Actions status.

| Verification | Observed result |
|---|---|
| `python3 -m unittest discover -v` | **70 tests passed** in 6.565 seconds; zero skips |
| `python3 -m compileall -q gateway scripts tests` | Passed |
| `python3 scripts/validate_incident_dashboard.py` | Passed: 12 unique panels and three incident metrics |
| `python3 scripts/simulate_incident.py --directory monitoring/runtime/portfolio-final-review-approved` | Passed: HTTP detection, recommendations, execution without approval rejected |
| `python3 scripts/validate_infra.py` | Passed: Helm lint, 17 schema-valid resources, four deployment policy scenarios, eight unsafe overrides rejected |
| `docker build --tag ai-gateway:portfolio-review .` | Passed; no push |
| `SMOKE_IMAGE=ai-gateway:portfolio-review python3 scripts/ci_container_smoke.py` | All 13 container, authorization and persistence checks passed |
| Monitoring Compose configuration | Passed |
| Prometheus `check config`, `check rules`, `test rules` | Passed: three rules and their fixtures |
| `python3 scripts/check_monitoring.py` | Passed: HTTP readiness, authenticated metrics, scrape, incident metrics, Grafana dashboard and datasource |
| Documentation links and assets | Local file links and heading anchors resolve; banner XML parses; all linked public pages and seven badge URLs returned HTTP 200 |
| Architecture diagrams | All five Mermaid diagrams rendered in Chromium; recovery labels shortened after visual review |
| Live GitHub Actions status badge | Returned `CI - passing` for `master` at review time; status may change |
| Runtime screenshots | Six genuine browser captures; twelve Grafana panels, UP target, three inactive rules, live health, Docker state and successful public CI run; pixels, SHA-256 digests and PNG metadata reviewed |
| `git diff --check` | Passed |
| `git fsck --full` | No integrity errors; one dangling blob remains; historical privacy findings remain |

Loopback, Docker and schema checks required approved execution outside the editing sandbox. The host lacks pip/venv support and Chromium system libraries; screenshot tooling was installed in an ignored environment, and the browser ran in the pinned Playwright container. The browser was exposed only on loopback, without repository or Docker socket mounts. See [capture instructions and provenance](screenshots/README.md).

The captured public [CI run](https://github.com/yugandhar99/AI-Gateway-Reliability-Platform/actions/runs/44137949807) succeeded at revision `e56ed19`. It precedes the local portfolio documentation commits. Its annotation warns that pinned actions target deprecated Node.js 20 and are forced onto Node.js 24; review action upgrades separately. The workflow was preserved.

The test container workflow retains its stopped containers and isolated volume by design. Existing monitoring services, credentials and persistent data were preserved. Screenshot traffic creates synthetic accounting, metrics and incident evidence; it does not approve remediation.

No live Kubernetes deployment, cloud deployment, load test, external provider call, or interactive operator approval demonstration was performed. Automated tests cover approval execution, expiry, revision, replay protection and recovery verification using controlled fixtures. Results establish tested behavior in this environment, not production capacity or readiness.

## Reproduce

Use the [development guide](development.md), [deployment guide](deployment.md), [incident runbook](runbooks/incidents.md), and [screenshot guide](screenshots/README.md). Use a fresh repository-local artifact directory for each incident demonstration. Infrastructure validation requires Helm 3.19.0, kubeconform 0.8.0, PyYAML 6.0.3 and schema network access. Runtime databases and credentials remain ignored and private.

## Asynchronous inference engineering validation

Measured on Linux with Python 3.14.4. The load and fault harness launches
the current gateway through its CLI, with synthetic provider outcomes and
disposable databases. The scheduling test controls the queue clock to
check the aging boundary exactly.

| Scenario | Observed result | Wall time |
|---|---|---:|
| 100 simultaneous HTTP submissions | 100 accepted with unique IDs. Enterprise 65, Business 30, Starter 5. | 4.675 s |
| Five-worker drain and atomic claims | Every job succeeded on its first claim, each result matched its prompt, and exactly 100 provider calls occurred. | 0.207 s after submission |
| Replay and tenant isolation | 40 concurrent replays returned the original ID. Altered replay returned 409, wrong-tenant reads and cancellation returned 404, and non-customer credentials returned 401. The ledgers settled 1,170, 540, and 90 estimated tokens respectively, with zero reserved tokens. | 0.068 s |
| Worker crash and stale result fencing | The crash marker abandoned one claim. Recovery succeeded on attempt 2. The expired token could neither renew nor change the stored result. | 0.940 s |
| Hard crash and restart | SIGKILL interrupted a simulated provider timeout. After restart the running job succeeded on attempt 2. Prior results, usage, and idempotency survived. | 1.561 s |
| Provider 503 retry and failover | A transient 503 recovered on OpenAI. A persistent OpenAI failure fell back to Anthropic. Both succeeded in one queue attempt. | 0.192 s |
| Exhausted retries and dead letter | A three-provider outage exhausted three attempts in 0.702 s with exponential backoff. Result and cancellation returned 409. Restart preserved the dead letter and its idempotency key. | 1.285 s including restart |
| First-submit race and backpressure | 20 concurrent submissions with one key produced one 202, 19 replays, and one reservation. Another tenant could use the same key independently. Both tenant and global limits returned 429 with `Retry-After`. | 0.102 s |
| Worker overlap, cancellation, and drain | Five distinct claims were running concurrently. Cancellation freed capacity and fenced a late provider outcome. Graceful shutdown preserved all remaining work. | 4.800 s including restart and simulated timeouts |
| Priority and aging | Four fresh high-priority jobs ran first. At the aging boundary the oldest low-priority job ran ahead of a fresh high-priority arrival. | 0.169 s |
| Authenticated exposition and dashboard checks | Scrape authentication and label restrictions held. Dashboard checks found 18 unique panels and six inference panels. | 0.025 s |

The harness passed in **16.757 seconds** including gateway startup and
shutdown. A repeat passed in **16.752 seconds**, with **4.483 seconds**
for the 100-client burst. The documented interactive demo also passed. The full suite passed **83 tests in 11.877 seconds**, with zero
skips. In a separate Compose project on ephemeral loopback ports,
Prometheus 3.7.3 scraped a completed job and Grafana 12.3.0 provisioned
18 panels. All 19 configured PromQL queries ran through Grafana's
Prometheus proxy. The isolated stack became ready in 7.813 seconds and
the live checks completed in 12.104 seconds. This check used cached
container images with the current gateway source mounted read-only.
Promtool passed configuration, rule, and alert fixture checks and accepted
both captured inference scrapes. The test stack and its volume were removed afterward.

The first load run hit TCP connection resets with the inherited listen
backlog of five. The gateway now sets a backlog of 128 and the same
100-client burst passed without client retries. Promtool also rejected
the new metric families for missing `HELP` records. Every queue and
worker metric now includes help text. The harness parser was also corrected
to accept braces inside quoted route labels such as `/jobs/{id}`.

The checks cover a local burst and simulated failures on one host.
Sustained throughput, multi-host operation, and real provider behavior
remain unmeasured. The [runbook](runbooks/inference.md#repeat-the-engineering-validation-on-ubuntu)
contains the exact replay commands.

## Final container integration review

Reviewed the exec-form Docker command, UID/GID 10001, the port 8000 HTTP
health probe, and writable SQLite storage under `/data`. The smoke script
uses a fresh uniquely named volume and sequential containers with networking
disabled, zero published ports, a read-only root filesystem, and dropped
capabilities. It removes the containers and volume it creates. The mounted
fixture contains only synthetic timeout settings and is read-only.

| Final verification | Observed result |
|---|---|
| Docker Engine 29.9.3 image build and smoke, Python 3.13.18 | Build passed. All 30 smoke checks passed across four isolated containers. The checks covered HTTP submission and completion, restart and replacement persistence, idempotency, scoped metrics, worker counts of two, one, and zero, and SIGTERM drain with queued work resumed by the replacement. |
| Python 3.14.4 unit suite | 83 tests passed in 10.257 seconds |
| Live inference validator | Two complete passes in 14.587 and 14.719 seconds. Both accepted all 100 jobs and completed each on its first claim with five workers. |
| Prometheus 3.7.3 `promtool check metrics` | Accepted both final live scrapes, including the post-restart scrape. |
| Python compilation and dashboard validation | Passed. The dashboard contains 18 panels and all three incident metrics. |

One preliminary live burst returned a storage-related HTTP 503 after
concurrent queue writes reached SQLite's busy timeout. Queue write
transactions now serialize within each gateway process. SQLite still
coordinates transactions across processes. Both final runs used the same
100-client burst with no client retries.

The GitHub Actions workflow now runs `scripts/validate_inference.py` after
unittest discovery and runs the expanded smoke script against the CI-built
image. Hosted CI for this revision has not run.

The Docker build context permits only the Dockerfile and top-level gateway
Python source. A final image inspection verified the application directory
contains only the gateway Python files, including the queue and worker modules.
The first image inspection found host bytecode under `gateway/__pycache__`.
The build context now explicitly excludes nested gateway paths.
Git ignore checks covered credential directories, environment files, SQLite
databases and sidecars, runtime logs, and validator scratch output. No tracked
paths matched ignore rules, and a targeted working-tree scan found no private
key headers or AWS, GitHub, or OpenAI token patterns tested. This scan covered
the current working tree. The smoke script generates separate disposable
customer, administrator, and metrics credentials. Docker administrators can
inspect container environment variables while the containers exist.

The final review exercises a single Docker host and local named-volume
storage with simulated providers. The smoke test exercises a 15-second
stop grace period and one slow claim. Deployment-specific grace periods,
external providers, multi-host storage, and sustained throughput remain
unverified. The historical monitoring and infrastructure results above apply
to their recorded runs.

### Repeat the container smoke on Ubuntu

Run these commands from the repository root on Ubuntu with Docker Engine
running and daemon access for your shell. The editing sandbox denied Docker
socket access, so the local review used approved execution outside it.

```bash
(
  set -eu
  image="ai-gateway:advanced-smoke-$(cat /proc/sys/kernel/random/uuid)"
  trap 'docker image rm "$image" >/dev/null' EXIT
  docker build --tag "$image" .
  SMOKE_IMAGE="$image" python3 -u scripts/ci_container_smoke.py
)
```

The test uses container-local loopback HTTP. It requires no host port or
existing application credentials.

To reproduce the two Prometheus exposition checks, run the live validator
and use the artifact directory it reports.

```bash
python3 scripts/validate_inference.py
read -r -p 'Validation artifact directory: ' artifacts
for scrape in gateway.prom restarted-gateway.prom; do
  docker run --rm -i --network none --entrypoint promtool \
    prom/prometheus:v3.7.3 check metrics < "$artifacts/$scrape"
done
```

## v2.0.0 publication verification

The publication pass reran the Python 3.14.4 suite and all **83 tests**
passed in 10.329 seconds. All **30 Docker smoke checks** passed against the
fresh image built for the isolated monitoring stack. A separate run against
the cached final review image also passed.

The live inference harness passed in **15.329 seconds**. All 100 concurrent
submissions were accepted across Enterprise (65), Business (30), and Starter
(5) in 0.803 seconds. Five workers drained all jobs on their first claim,
with 100 simulator calls. The estimates settled to 1,170, 540, and 90 tokens
respectively, with zero outstanding reservations. The replay, crash,
restart, backpressure, cancellation, dead-letter, and scheduling checks
also passed. These measurements describe a local simulated workload.

The isolated Compose capture used five workers, ephemeral loopback ports,
and disposable credentials and storage. The authenticated Prometheus scrape
was UP, Grafana provisioned all **18 panels**, and all **19 PromQL queries**
succeeded through the Grafana datasource proxy. Headless Chromium captured
the dashboard and Prometheus pages after synthetic chat and job traffic.
Both inference scrapes passed `promtool check metrics`, and the alert rule
fixtures passed. The README quick start returned a completed simulated job.
[Capture manifest and image hashes](screenshots/v2/capture.json)
