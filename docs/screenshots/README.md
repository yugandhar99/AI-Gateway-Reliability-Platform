# Runtime screenshot automation

These captures are generated from the running local stack. Run the commands below from the repository root to reproduce them.

```bash
python3 -m venv .venv-screenshots
.venv-screenshots/bin/python -m pip install -r scripts/requirements-screenshots.txt
.venv-screenshots/bin/python -m playwright install --with-deps chromium
docker compose --env-file .env.monitoring -f compose.monitoring.yaml up -d --build --wait
python3 scripts/check_monitoring.py
.venv-screenshots/bin/python scripts/capture_screenshots.py
```

Chromium system dependencies may require sudo during installation. Docker must be accessible to your terminal user. Existing `.env.monitoring` and `secrets/monitoring/` files are reused without displaying their contents. If setting up a fresh checkout, run `python3 scripts/setup_monitoring.py` before starting Compose. Do not regenerate credentials for an already running stack.

For hosts without Chromium system libraries, the verified capture used the pinned browser image `mcr.microsoft.com/playwright/python:v1.56.0-noble` and a loopback Playwright server. Start this in a separate terminal:

```bash
docker run --rm --name portfolio-browser-review --user "$(id -u):$(id -g)" \
  -p 127.0.0.1:9323:9323 mcr.microsoft.com/playwright/python:v1.56.0-noble \
  sh -c 'python -m pip install --no-cache-dir --target /tmp/portfolio-pw playwright==1.56.0 && /tmp/portfolio-pw/playwright/driver/node /tmp/portfolio-pw/playwright/driver/package/cli.js run-server --host 0.0.0.0 --port 9323'
```

Then run `.venv-screenshots/bin/python scripts/capture_screenshots.py --browser-ws ws://127.0.0.1:9323/`. The connection exposes loopback service access to this trusted local browser. Stop it afterward with `docker stop portfolio-browser-review`; it receives no repository or Docker socket mount.

The script runs headless Chromium through Python Playwright with an explicit `en-US` locale, avoiding invalid host locale tags in Grafana. Use `--reuse-traffic` to capture existing telemetry after an earlier simulation run; the manifest records this choice. It waits for baseline scrapes, invokes the existing traffic simulator with 20 requests per phase, restores the original scenario through that simulator's `finally` block, and waits for recovery scrapes. This updates local metrics, usage accounting and incident records. It does not execute remediation or change application code.

Outputs are written here:

| File | Evidence |
|---|---|
| `grafana-dashboard.png` | Actual authenticated Grafana dashboard with all twelve panel titles checked before capture |
| `prometheus-targets.png` | Actual Prometheus targets page; gateway scrape must be UP |
| `prometheus-alerts.png` | Actual Prometheus alert rules and current state |
| `gateway-health.png` | Actual gateway `/health` response |
| `docker-health.png` | HTML report rendered by Chromium using narrowly selected live Docker state fields |
| `github-actions.png` | Optional public workflow run verified successful using GitHub's API |
| `capture.json` | Capture start time, source revision, dirty-tree flag, scenario, captured files, SHA-256 image digests, browser transport and actual container state |

GitHub capture uses a fresh browser context without account credentials or saved login state. If the public API or run page is inaccessible, it records the omission and integrates only the other verified captures. Use `--skip-github` to omit the attempt. An older PNG may remain after a rerun, but only images recorded in the current manifest are integrated into README.

README image blocks and descriptions are generated only after all five required local captures succeed. The script refuses images when known local secret values appear in visible text; it never stores authentication state, credentials, request traces, raw Docker configuration, or container logs. Grafana login uses an ephemeral context cookie scoped to its loopback origin; no login form is captured. The health response uses larger browser text for readability. Review image pixels and repository changes before committing. No commit, push, or publication is performed.

If a capture fails, check service readiness and browser dependencies privately, then rerun. Partial PNG files can exist after failure; the README is not updated by a failed run. A zero approval count, inactive alerts, or missing Docker health check must not be interpreted as an error or relabeled healthy. These captures demonstrate a local simulation, not cloud deployment, real provider inference or production readiness.
