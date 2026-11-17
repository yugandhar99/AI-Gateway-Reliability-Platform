"""Check the running isolated monitoring stack without displaying credentials."""
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]


def get(url, token=None, body=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url, json.dumps(body).encode() if body else None, headers)
    with urlopen(req, timeout=5) as response:
        return response.read().decode()


def main():
    for _ in range(60):
        try:
            get('http://127.0.0.1:8003/health')
            get('http://127.0.0.1:3000/api/health')
            get('http://127.0.0.1:9090/-/ready')
            break
        except (URLError, TimeoutError):
            time.sleep(1)
    else:
        raise AssertionError('Monitoring services did not become ready')
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
    import subprocess
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
    main()
