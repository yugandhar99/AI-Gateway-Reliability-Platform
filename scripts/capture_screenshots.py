"""Capture genuine local monitoring evidence with Playwright headless Chromium.

Install dependencies and start the monitoring stack as documented in
 docs/screenshots/README.md. No credentials, browser state or raw Docker inspect
 output are saved. Run from a normal Ubuntu terminal with Docker access.
"""
import argparse
from datetime import datetime, timezone
import html
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/screenshots/v2'
COMPOSE = ['docker', 'compose', '--env-file', '.env.monitoring', '-f', 'compose.monitoring.yaml']
CAPTIONS = {
    'grafana-dashboard.png': ('Grafana AI Gateway dashboard', 'All eighteen provisioned panels from the local simulated stack.'),
    'prometheus-targets.png': ('Prometheus targets', 'Actual scrape target state for the authenticated AI Gateway metrics endpoint.'),
    'prometheus-alerts.png': ('Prometheus alert rules', 'Loaded alert rules and their current evaluation state; this does not imply an alert is firing.'),
    'gateway-health.png': ('AI Gateway health endpoint', 'Live /health response from the local gateway. This endpoint reports process liveness.'),
    'docker-health.png': ('Docker container health report', 'A generated visual report of actual Compose container state, restart count, exit code and configured health checks. Containers without a health check are labeled explicitly.'),
    'github-actions.png': ('Successful GitHub Actions workflow', 'A public GitHub Actions run verified as completed successfully through the GitHub API before capture. The run revision is recorded in provenance; local documentation commits require their own hosted run.'),
}


def command(args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Required local command failed; check Docker access and stack readiness privately.')
    return result.stdout.strip()


def container_rows():
    rows = []
    for service in ('gateway', 'prometheus', 'grafana'):
        ids = command(COMPOSE + ['ps', '--all', '--quiet', service]).split()
        if len(ids) != 1:
            raise RuntimeError('Expected one existing container for ' + service)
        raw = command(['docker', 'inspect', '--format',
                       '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}not configured{{end}}|{{.RestartCount}}|{{.State.ExitCode}}', ids[0]])
        state, health, restarts, exit_code = raw.split('|')
        if state not in ('created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead') or health not in ('healthy', 'unhealthy', 'starting', 'not configured'):
            raise RuntimeError('Unexpected Docker state')
        rows.append([service, state, health, int(restarts), int(exit_code)])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-github', action='store_true', help='Skip optional public GitHub Actions capture')
    parser.add_argument('--reuse-traffic', action='store_true', help='Capture existing telemetry without generating another synthetic workload')
    parser.add_argument('--browser-ws', help='Optional local Playwright browser server WebSocket URL')
    args = parser.parse_args()
    if args.browser_ws:
        endpoint = urlparse(args.browser_ws)
        if endpoint.scheme != "ws" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1") or endpoint.username or endpoint.password:
            parser.error("Browser server must use an unauthenticated loopback ws:// endpoint")
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    # Read only known local credentials; never print them or put them in argv.
    sensitive = [p.read_text().strip() for p in (ROOT / 'secrets/monitoring').iterdir() if p.is_file()]
    password = (ROOT / 'secrets/monitoring/grafana').read_text().strip()
    customer_key = (ROOT / 'secrets/monitoring/customer').read_text().strip()
    if not password:
        raise RuntimeError('Grafana password is empty')
    container_rows()  # Fail before generating traffic if Docker is inaccessible.
    revision = command(['git', 'rev-parse', 'HEAD'])
    working_tree_dirty = bool(command(['git', 'status', '--porcelain']))
    captured = []
    stamp = datetime.now(timezone.utc).isoformat()
    with sync_playwright() as pw:
        browser = pw.chromium.connect(args.browser_ws, expose_network="<loopback>") if args.browser_ws else pw.chromium.launch(headless=True)
        context = browser.new_context(locale='en-US', viewport={'width': 1920, 'height': 2600}, device_scale_factor=1)
        page = context.new_page()
        for url in ('http://127.0.0.1:8003/health', 'http://127.0.0.1:9090/-/ready', 'http://127.0.0.1:3000/api/health'):
            response = context.request.get(url)
            if not response.ok:
                raise RuntimeError('Local service is not ready')
        if not args.reuse_traffic:
            print('Generating synthetic traffic; original simulation configuration will be restored.', flush=True)
            # Obtain baseline scrapes before counters change, then allow recovery scrapes.
            page.wait_for_timeout(20000)
            subprocess.run([sys.executable, 'scripts/simulate_traffic.py', '--requests', '20', '--interval', '1'], cwd=ROOT, check=True)
            headers = {'Authorization': 'Bearer ' + customer_key}
            jobs = []
            for priority in range(5):
                response = context.request.post(
                    'http://127.0.0.1:8003/jobs', headers=headers,
                    data={'prompt': 'Synthetic monitoring job', 'provider': 'OpenAI',
                          'priority': priority},
                )
                if response.status != 202:
                    raise RuntimeError('Synthetic job admission failed')
                jobs.append(response.json()['id'])
            page.wait_for_timeout(30000)
            for job in jobs:
                result = context.request.get(
                    f'http://127.0.0.1:8003/jobs/{job}/result', headers=headers,
                )
                if result.status != 200:
                    raise RuntimeError('Synthetic job did not complete')
        query = context.request.get('http://127.0.0.1:9090/api/v1/query', params={'query': 'up{job="ai-gateway"}'}).json()
        values = query.get('data', {}).get('result', [])
        if not values or any(item['value'][1] != '1' for item in values):
            raise RuntimeError('Gateway scrape target is not UP')

        def save(target, name):
            text = target.locator('body').inner_text()
            if any(secret and secret in text for secret in sensitive):
                raise RuntimeError('Sensitive text detected; screenshot refused')
            target.screenshot(path=str(OUT / name), full_page=True, animations='disabled')
            captured.append(name)
            print('Captured ' + name, flush=True)

        grafana = browser.new_context(locale='en-US', viewport={'width': 1920, 'height': 3000})
        login = grafana.request.post('http://127.0.0.1:3000/login', data={'user': 'admin', 'password': password})
        if not login.ok:
            raise RuntimeError('Grafana login failed')
        dashboard_response = grafana.request.get('http://127.0.0.1:3000/api/dashboards/uid/ai-gateway')
        if not dashboard_response.ok:
            raise RuntimeError('Grafana dashboard API unavailable')
        dashboard_data = dashboard_response.json()
        dashboard = dashboard_data['dashboard']
        if len(dashboard['panels']) != 18:
            raise RuntimeError('Expected exactly eighteen dashboard panels')
        gp = grafana.new_page()
        gp.goto('http://127.0.0.1:3000' + dashboard_data['meta']['url'] + '?orgId=1&from=now-15m&to=now&kiosk', wait_until='domcontentloaded')
        for panel in dashboard['panels']:
            gp.get_by_text(panel['title'], exact=True).first.scroll_into_view_if_needed()
        gp.wait_for_timeout(10000)
        gp.evaluate('window.scrollTo(0, 0)')
        for panel in dashboard['panels']:
            if not gp.get_by_text(panel['title'], exact=True).count():
                raise RuntimeError('Dashboard panel missing')
        if gp.get_by_text('No data', exact=True).count():
            raise RuntimeError('Dashboard contains panels without data; wait for scrapes and retry')
        save(gp, 'grafana-dashboard.png')
        grafana.close()
        page.set_viewport_size({'width': 1600, 'height': 1000})
        for route, filename, expected in [
            ('http://127.0.0.1:9090/targets', 'prometheus-targets.png', 'ai-gateway'),
            ('http://127.0.0.1:9090/alerts', 'prometheus-alerts.png', 'Gateway'),
            ('http://127.0.0.1:8003/health', 'gateway-health.png', 'status'),
        ]:
            page.set_viewport_size({'width': 1600, 'height': 360 if filename == 'prometheus-targets.png' else 430})
            response = page.goto(route, wait_until='domcontentloaded')
            if not response.ok:
                raise RuntimeError('Capture endpoint failed')
            page.get_by_text(expected, exact=False).first.wait_for()
            if filename == 'gateway-health.png':
                page.set_viewport_size({'width': 1000, 'height': 180})
                page.add_style_tag(content='body, pre { font-size: 24px; line-height: 1.6; }')
            save(page, filename)
        page.set_viewport_size({'width': 1600, 'height': 760})
        rows = container_rows()
        cells = ''.join('<tr>' + ''.join('<td>' + html.escape(str(value)) + '</td>' for value in row) + '</tr>' for row in rows)
        page.set_content('<html><head><title>Docker health report</title><style>body{font:24px system-ui;background:#101827;color:#e5edf8;padding:64px}h1{color:#67d5ff}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:24px;border-bottom:1px solid #46556c}p{color:#a9bad0}</style></head><body><h1>AI Gateway · Docker health report</h1><p>Actual local Docker Compose state · ' + html.escape(stamp) + '</p><table><tr><th>Service</th><th>State</th><th>Health check</th><th>Restarts</th><th>Exit code</th></tr>' + cells + '</table><p>Generated visualization of Docker state; not a Docker Desktop screenshot.<br>“not configured” means Docker has no health check for that service.</p></body></html>')
        save(page, 'docker-health.png')
        github_note = 'Skipped by request'
        if not args.skip_github:
            try:
                repo = 'yugandhar99/AI-Gateway-Reliability-Platform'
                response = context.request.get(f'https://api.github.com/repos/{repo}/actions/workflows/ci.yml/runs?status=success&per_page=1')
                runs = response.json().get('workflow_runs', []) if response.ok else []
                if not runs or runs[0]['conclusion'] != 'success' or runs[0]['status'] != 'completed':
                    raise RuntimeError('No accessible successful run')
                run = runs[0]
                if run['head_sha'] != revision or working_tree_dirty:
                    raise RuntimeError('Successful run does not match the capture revision')
                page.set_viewport_size({'width': 1600, 'height': 1000})
                response = page.goto(run['html_url'], wait_until='networkidle', timeout=60000)
                if not response.ok:
                    raise RuntimeError('Run page inaccessible')
                page.get_by_text('Success', exact=True).first.wait_for(timeout=15000)
                save(page, 'github-actions.png')
                github_note = run['html_url'] + ' · revision ' + run['head_sha']
            except Exception:
                github_note = 'Unavailable: no verified public successful run could be captured. No authenticated browser state was used.'
                print(github_note)
        browser.close()
    manifest = {'started_utc': stamp, 'source_revision': revision, 'working_tree_dirty': working_tree_dirty, 'environment': 'local Docker Compose, simulated providers, headless Chromium', 'scenario': ('Existing telemetry reused without generating traffic' if args.reuse_traffic else '20 chat requests per phase: normal, preferred outage, all-provider timeout, recovery; original scenario restored; five asynchronous jobs submitted and results checked'), 'command': 'python scripts/capture_screenshots.py' + (' --skip-github' if args.skip_github else '') + (' --reuse-traffic' if args.reuse_traffic else '') + (' --browser-ws ' + args.browser_ws if args.browser_ws else ''), 'images': captured, 'sha256': {name: hashlib.sha256((OUT / name).read_bytes()).hexdigest() for name in captured}, 'browser_transport': 'local Playwright server' if args.browser_ws else 'local Chromium', 'docker_state': rows, 'github_actions': github_note}
    (OUT / 'capture.json').write_text(json.dumps(manifest, indent=2) + '\n')
    block = '<!-- BEGIN GENERATED SCREENSHOTS -->\n\n'
    for name in captured:
        title, caption = CAPTIONS[name]
        block += f'**{title}**\n\n{caption}\n\n![{title}](docs/screenshots/v2/{name})\n\n'
    block += '[Capture provenance](docs/screenshots/v2/capture.json). Screenshots show a local simulated workload, not a production deployment.\n\n<!-- END GENERATED SCREENSHOTS -->'
    readme = ROOT / 'README.md'
    content = readme.read_text()
    start = content.index('<!-- BEGIN GENERATED SCREENSHOTS -->')
    end = content.index('<!-- END GENERATED SCREENSHOTS -->', start) + len('<!-- END GENERATED SCREENSHOTS -->')
    readme.write_text(content[:start] + block + content[end:])
    print('Capture complete. Review image pixels before committing; no changes were pushed.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Capture interrupted; README was not updated.', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        # Playwright errors may include request headers; never emit raw exceptions.
        print('Capture failed (' + type(error).__name__ + '). Check service readiness, Docker access and browser installation. README was not updated.', file=sys.stderr)
        sys.exit(1)
