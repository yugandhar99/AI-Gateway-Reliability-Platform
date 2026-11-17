# Validation record — 2026-11-17

Source baseline: `e56ed19`, with the portfolio documentation draft and the existing neutral infrastructure scenario rename in the working tree. Host Python: **3.14.4**. CI and the image target Python 3.13. These are local results, distinct from hosted GitHub Actions results. Unit tests, compilation, dashboard validation and documentation checks were rerun for finalization; infrastructure and isolated incident results below were obtained during the preceding review at the same baseline.

| Verification | Observed result |
|---|---|
| `python3 -m unittest discover -v` | **70 tests passed** in 6.116 seconds; zero skips |
| `python3 -m compileall -q gateway scripts tests` | Passed |
| `python3 scripts/validate_incident_dashboard.py` | Passed: 12 unique panels and three incident metrics |
| `python3 scripts/simulate_incident.py --directory monitoring/runtime/portfolio-review-e56ed19-approved` | Passed: HTTP detection, recommendations, execution without approval rejected |
| `python3 scripts/validate_infra.py` | Passed: Helm lint, 17 schema-valid resources, four deployment policy scenarios, eight unsafe overrides rejected |
| Local Markdown file links and banner XML | Passed |
| `git diff --check` | Passed |
| `git fsck --full` | Passed; historical privacy findings remain |

The isolated HTTP check required approved loopback socket access. Infrastructure validation required approved network access to fetch Kubernetes schemas. Initial sandbox restrictions prevented these checks; reruns passed. An initial incident invocation using an external artifact directory was correctly rejected by the script's repository-boundary guard.

The connected GitHub tool returned no push workflow runs for `e56ed19` during this review. No hosted success is inferred from that response. The README links the live status badge for the unchanged workflow on `master`.

Docker build, container smoke tests, Prometheus rule fixtures and live monitoring integration were **not rerun in this review**. They remain in the existing CI pipeline. Earlier draft results are not represented as fresh evidence. No live Kubernetes deployment, cloud deployment, load test, external provider call, or interactive operator approval demonstration was performed.

## Reproduce

Use the [development guide](development.md), [deployment guide](deployment.md), and [incident runbook](runbooks/incidents.md). Use a fresh repository-local artifact directory for each incident demonstration. Infrastructure validation requires Helm 3.19.0, kubeconform 0.8.0, PyYAML 6.0.3 and schema network access.

Automated incident tests cover execution, approval expiry/revision/replay guards, evidence-based verification and accounting reconciliation with controlled fixtures. These differ from an interactive operator approval demonstration. Runtime databases remain ignored and private. Results establish tested behavior in this environment, not production capacity or readiness.
