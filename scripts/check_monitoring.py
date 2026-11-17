"""Check the running isolated monitoring stack without displaying credentials."""
import json
import subprocess
from http.client import RemoteDisconnected
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]


def get(url, token=None, body=None, timeout=5):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url, json.dumps(body).encode() if body else None, headers)
    with urlopen(req, timeout=timeout) as response:
        return response.read().decode()


SERVICES = {
    'gateway': 'http://127.0.0.1:8003/health',
    'prometheus': 'http://127.0.0.1:9090/-/ready',
    'grafana': 'http://127.0.0.1:3000/api/health',
}
TRANSIENT_ERRORS = (URLError, TimeoutError, ConnectionError, RemoteDisconnected)


def startup_diagnostics():
    """Print only allowlisted container state; never logs, env or health output."""
    compose = ['docker', 'compose', '--env-file', '.env.monitoring',
               '-f', 'compose.monitoring.yaml']
    for service in SERVICES:
        try:
            result = subprocess.run(compose + ['ps', '--all', '--quiet', service],
                                    cwd=ROOT, capture_output=True, text=True,
                                    timeout=10, check=True)
            ids = result.stdout.split()
            if not ids:
                print(f'{service}: no container found', flush=True)
            for container in ids:
                result = subprocess.run(
                    ['docker', 'inspect', '--format',
                     '{{json .State.Status}} {{.State.ExitCode}} {{.RestartCount}}', container],
                    cwd=ROOT, capture_output=True, text=True, timeout=10, check=True)
                # Validate fields before printing any external output.
                status, exit_code, restarts = result.stdout.split()
                if status not in ('"created"', '"running"', '"paused"', '"restarting"',
                                  '"removing"', '"exited"', '"dead"'):
                    raise ValueError('Unexpected container status')
                print(f'{service}: state={status} exit={int(exit_code)} '
                      f'restarts={int(restarts)}', flush=True)
        except (OSError, subprocess.SubprocessError, ValueError):
            print(f'{service}: container state unavailable', flush=True)


def wait_for_readiness(timeout=120, interval=1):
    """Bound HTTP startup probes by a monotonic deadline and request timeouts."""
    deadline = time.monotonic() + timeout
    failures = {service: 'not checked' for service in SERVICES}
    while time.monotonic() < deadline:
        failures = {}
        for service, url in SERVICES.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failures[service] = 'deadline reached'
                continue
            try:
                get(url, timeout=min(5, remaining))
            except HTTPError as error:
                error.close()
                if error.code not in (408, 425, 429, 500, 502, 503, 504):
                    raise
                failures[service] = f'HTTP {error.code}'
            except TRANSIENT_ERRORS as error:
                # Exception messages can contain URLs or other sensitive data.
                failures[service] = type(error).__name__
        if not failures:
            print('PASS: gateway, Prometheus and Grafana HTTP readiness')
            return
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    detail = ', '.join(f'{name} ({reason})' for name, reason in failures.items())
    raise AssertionError(f'Monitoring readiness timed out after {timeout}s: {detail}')


def main():
    try:
        wait_for_readiness()
    except Exception:
        startup_diagnostics()
        raise
    token = (ROOT / 'secrets/monitoring/customer').read_text().strip()
    get('http://127.0.0.1:8003/chat', token, {'provider':'OpenAI', 'prompt':'Monitoring smoke'})
    try:
        get('http://127.0.0.1:8003/metrics/prometheus')
    except HTTPError as error:
        assert error.code == 401
        error.close()
    else:
        raise AssertionError('Scraping must require authentication')
    scraper = (ROOT / 'secrets/monitoring/metrics').read_text().strip()
    metrics = get('http://127.0.0.1:8003/metrics/prometheus', scraper)
    assert 'gateway_tokens_total' in metrics
    for metric in ('gateway_incidents_active', 'gateway_incidents_resolved_total', 'gateway_incident_approvals_total'):
        assert metric in metrics
    subprocess.run(['docker', 'compose', '--env-file', '.env.monitoring', '-f',
                    'compose.monitoring.yaml', 'exec', '-T', 'prometheus',
                    'promtool', 'check', 'metrics'], input=metrics, text=True, check=True,
                   cwd=ROOT, capture_output=True)
    for _ in range(45):
        data = json.loads(get('http://127.0.0.1:9090/api/v1/query?query=gateway_tokens_total'))
        if data['data']['result'] and float(data['data']['result'][0]['value'][1]) > 0:
            break
        time.sleep(1)
    else:
        raise AssertionError('Prometheus did not scrape gateway tokens')
    # Grafana HTTP basic auth stays in memory, never command arguments or logs.
    import base64
    password = (ROOT / 'secrets/monitoring/grafana').read_text().strip()
    auth = base64.b64encode(('admin:' + password).encode()).decode()
    for path in ('/api/dashboards/uid/ai-gateway', '/api/datasources/uid/gateway-prometheus/health'):
        req = Request('http://127.0.0.1:3000' + path, headers={'Authorization':'Basic ' + auth})
        with urlopen(req, timeout=10) as response:
            result = json.load(response)
            if path.endswith('/health'):
                assert result['status'] == 'OK'
            else:
                assert len(result['dashboard']['panels']) >= 12
                assert {10,11,12} <= {panel['id'] for panel in result['dashboard']['panels']}
    print('PASS: authenticated metrics, Prometheus scrape, incident metrics, provisioned Grafana dashboard and data source')


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--diagnostics']:
        startup_diagnostics()
    else:
        main()
