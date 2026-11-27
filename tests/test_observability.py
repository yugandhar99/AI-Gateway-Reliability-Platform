import http.client 
import json
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from gateway.observability import Metrics
from gateway.reliability import Router, Simulator, AllProvidersFailed
from gateway.security import KeyRegistry, Customer
from gateway.server import GatewayHandler
from gateway.usage import UsageStore
from tests.test_http import MemoryConnection


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.router = Router()
        self.server = SimpleNamespace(router=self.router,
            keys=KeyRegistry({'test-client': Customer('demo', 'Starter')}, 'test-admin'),
            metrics_keys=KeyRegistry(admin_key='test-scraper'),
            usage_store=UsageStore(self.temp.name + '/usage.sqlite3'))

    def request(self, path, token=None, payload=None):
        body = json.dumps(payload).encode() if payload else b''
        method = 'POST' if payload else 'GET'
        headers = f'{method} {path} HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n'
        if token:
            headers += f'Authorization: Bearer {token}\r\n'
        conn = MemoryConnection(headers.encode() + b'\r\n' + body)
        GatewayHandler(conn, ('127.0.0.1', 1), self.server)
        response = http.client.HTTPResponse(MemoryConnection(conn.output.getvalue()))
        response.begin()
        return response.status, response.getheader('Content-Type'), response.read().decode()

    def test_scoped_authentication_and_json_compatibility(self):
        for key in (None, 'invalid', 'test-admin', 'test-client'):
            self.assertEqual(self.request('/metrics/prometheus', key)[0], 401)
        status, content_type, body = self.request('/metrics/prometheus', 'test-scraper')
        self.assertEqual(status, 200)
        self.assertIn('version=0.0.4', content_type)
        self.assertIn('gateway_authentication_failures_total 4', body)
        self.assertEqual(self.request('/metrics', 'test-scraper')[0], 401)
        self.assertIn('providers', json.loads(self.request('/metrics', 'test-admin')[2]))
        self.assertEqual(self.request('/usage', 'test-scraper')[0], 401)

    def test_tokens_errors_limits_and_bounded_labels(self):
        for _ in range(6):
            self.request('/chat', 'test-client', {'prompt':'secret prompt', 'provider':'OpenAI'})
        self.request('/private-customer-path?key=secret')
        body = self.request('/metrics/prometheus', 'test-scraper')[2]
        self.assertIn('gateway_rate_limits_total 1', body)
        self.assertIn('route="/chat",method="POST",status="429"} 1', body)
        self.assertRegex(body, r'gateway_tokens_total [1-9][0-9]*')
        self.assertIn('route="unknown"', body)
        for private in ('secret prompt', 'private-customer', 'test-client', 'test-scraper', 'demo'):
            self.assertNotIn(private, body)

    def test_outage_retries_failover_and_circuits(self):
        from pathlib import Path
        path = Path(self.temp.name) / 'outcomes.json'
        path.write_text('{"OpenAI": ["503"]}')
        self.router.simulator = Simulator(path)
        self.router.route('hello', 'OpenAI')
        body = self.router.observability.render(self.router).decode()
        self.assertIn('gateway_provider_failures_total{provider="OpenAI"} 2', body)
        self.assertIn('gateway_retries_total{provider="OpenAI"} 1', body)
        self.assertIn('gateway_failovers_total{provider="OpenAI"} 1', body)
        path.write_text(json.dumps({p:['503'] for p in self.router.health}))
        for _ in range(3):
            with self.assertRaises(AllProvidersFailed):
                self.router.route('hello', 'OpenAI')
        body = self.router.observability.render(self.router).decode()
        self.assertIn('gateway_circuit_breaker_state{provider="OpenAI",state="OPEN"} 1', body)
        self.assertIn('gateway_provider_available{provider="OpenAI"} 0', body)

    def test_concurrent_histogram_cumulative_buckets(self):
        metrics = Metrics()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: metrics.observe('/chat', 'POST', 200, .02), range(100)))
        body = metrics.render(self.router).decode()
        self.assertIn('le="0.01"} 0', body)
        self.assertIn('le="0.025"} 100', body)
        self.assertIn('le="+Inf"} 100', body)
        self.assertIn('gateway_http_request_duration_seconds_count{route="/chat"} 100', body)

    def test_setup_is_idempotent_and_secrets_are_private(self):
        from pathlib import Path
        from scripts.setup_monitoring import main
        root = Path(self.temp.name)
        main(root)
        keys = {p.name: p.read_text() for p in (root / 'secrets/monitoring').iterdir()}
        self.assertEqual(len(set(keys.values())), 4)
        self.assertEqual((root / 'secrets/monitoring').stat().st_mode & 0o777, 0o700)
        main(root)
        self.assertEqual(keys, {p.name:p.read_text() for p in (root / 'secrets/monitoring').iterdir()})
        self.assertEqual(json.loads((root / 'monitoring/runtime/simulation/outcomes.json').read_text()), {})

    def test_file_credentials_and_reused_metrics_key_rejected_before_socket(self):
        import os
        from pathlib import Path
        from unittest.mock import patch
        from gateway.security import environment_key
        from gateway.server import create_server
        keyfile = Path(self.temp.name) / 'key'
        keyfile.write_text('fixture-key\n')
        with patch.dict(os.environ, {'FIXTURE_KEY_FILE':str(keyfile)}, clear=True):
            self.assertEqual(environment_key('FIXTURE_KEY'), 'fixture-key')
            os.environ['FIXTURE_KEY'] = 'duplicate'
            with self.assertRaises(ValueError):
                environment_key('FIXTURE_KEY')
        for key in ('test-client', 'test-admin'):
            with self.assertRaises(ValueError):
                create_server(keys=self.server.keys, usage_store=self.server.usage_store, metrics_key=key)
