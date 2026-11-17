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
