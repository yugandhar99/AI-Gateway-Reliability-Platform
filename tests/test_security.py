from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gateway.reliability import Router, ProviderFailure
from gateway.security import AuthenticationError, Customer, KeyRegistry, TIERS
from gateway.usage import LimitExceeded, UsageStore, estimate_tokens
from tests import test_http


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.now = 1_767_225_600  # January 2026, UTC
        self.path = Path(self.temp.name) / 'usage.sqlite3'
        self.store = UsageStore(self.path, lambda: self.now)
        self.a = Customer('customer-a', 'Starter')
        self.b = Customer('customer-b', 'Starter')
        self.keys = KeyRegistry({'test-a': self.a, 'test-b': self.b}, 'test-admin')
        self.http = test_http.HTTPTests()
        self.http.gateway = SimpleNamespace(keys=self.keys, usage_store=self.store, router=Router())

    def request(self, method, path, key=None, payload=None):
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['Authorization'] = 'Bearer ' + key
        return self.http.request(method, path, json.dumps(payload) if payload is not None else None,
                                 headers, authenticated=False)

    def chat(self, key='test-a', prompt='hello'):
        return self.request('POST', '/chat', key, {'prompt': prompt, 'provider': 'OpenAI'})

    def test_authentication_and_admin_metrics(self):
        for key in (None, '', 'wrong', 'test-admin'):
            self.assertEqual(self.chat(key)[0], 401)
        self.assertEqual(self.request('GET', '/usage')[0], 401)
        self.assertEqual(self.request('GET', '/metrics', 'test-a')[0], 401)
        self.assertEqual(self.request('GET', '/metrics', 'test-admin')[0], 200)
        self.assertEqual(self.request('GET', '/health')[0], 200)
        self.assertEqual(self.request('GET', '/providers')[0], 200)
        for value in ('Basic test-a', 'Bearer test-a extra', 'Bearer café'):
            headers = Message()
            headers['Authorization'] = value
            with self.assertRaises(AuthenticationError):
                self.keys.authenticate(headers)
        headers = Message()
        headers['Authorization'] = 'Bearer test-a'
        headers['Authorization'] = 'Bearer test-b'
        with self.assertRaises(AuthenticationError):
            self.keys.authenticate(headers)

    def test_environment_configuration(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                KeyRegistry.from_environment()
        with patch.dict('os.environ', {'GATEWAY_STARTER_API_KEY': 'test-a',
                                      'GATEWAY_BUSINESS_API_KEY': 'test-a'}, clear=True):
            with self.assertRaises(ValueError):
                KeyRegistry.from_environment()
        with patch.dict('os.environ', {'GATEWAY_STARTER_API_KEY': 'test-a'}, clear=True):
            registry = KeyRegistry.from_environment()
            self.assertEqual(registry.customers[0][1].tier, 'Starter')
        with self.assertRaises(ValueError):
            KeyRegistry({'same': self.a}, 'same')

    def test_rate_limits_and_tenant_isolation(self):
        for _ in range(5):
            self.assertEqual(self.chat()[0], 200)
        status, headers, body = self.chat()
        self.assertEqual(status, 429)
        self.assertEqual(body['code'], 'rate_limit_exceeded')
        self.assertEqual(headers['Retry-After'], '60')
        self.assertEqual(self.chat('test-b')[0], 200)
        self.assertEqual(self.request('GET', '/usage', 'test-b')[2]['requests'], 1)
        self.now += 60
        self.assertEqual(self.chat()[0], 200)

    def test_tier_limits(self):
        for tier_name, tier in TIERS.items():
            customer = Customer(tier_name, tier_name)
            for _ in range(tier.requests_per_minute):
                self.store.finish(self.store.admit(customer, 1), 1)
            with self.assertRaises(LimitExceeded):
                self.store.admit(customer, 1)
            self.assertEqual(self.store.usage(customer)['monthly_token_quota'], tier.monthly_tokens)

    def test_customer_identity_cannot_be_overridden_by_request(self):
        status, _, _ = self.request('POST', '/chat', 'test-a', {
            'prompt': 'hello', 'provider': 'OpenAI',
            'customer_id': self.b.customer_id, 'tier': 'Enterprise'})
        self.assertEqual(status, 200)
        status, _, usage = self.request(
            'GET', '/usage?customer_id=customer-b&tier=Enterprise', 'test-a')
        self.assertEqual(status, 200)
        self.assertEqual(usage['customer_id'], self.a.customer_id)
        self.assertEqual(usage['tier'], 'Starter')
        self.assertEqual(usage['requests'], 1)
        other = self.request('GET', '/usage', 'test-b')[2]
        self.assertEqual(other['requests'], 0)
        self.assertEqual(other['estimated_tokens'], 0)

    def test_quota_http_and_month_rollover(self):
        self.store.finish(self.store.admit(self.a, 10_000), 10_000)
        status, _, body = self.chat()
        self.assertEqual(status, 429)
        self.assertEqual(body['code'], 'quota_exhausted')
        self.assertEqual(self.chat('test-b')[0], 200)
        self.now += 32 * 86400
        self.assertEqual(self.chat()[0], 200)
        self.assertEqual(self.store.usage(self.a)['requests'], 1)

    def test_concurrent_quota_and_persistence(self):
        # Separate store objects model separate threads/processes sharing the file.
        def admit(_):
            store = UsageStore(self.path, lambda: self.now)
            try:
                return store.admit(self.a, 6000)
            except LimitExceeded:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            tickets = list(pool.map(admit, range(8)))
        self.assertEqual(sum(t is not None for t in tickets), 1)
        usage = UsageStore(self.path, lambda: self.now).usage(self.a)
        self.assertEqual(usage['reserved_tokens'], 6000)
        self.assertEqual(usage['remaining_tokens'], 4000)
        ticket = next(t for t in tickets if t is not None)
        self.store.finish(ticket, 100)
        self.assertEqual(self.store.usage(self.a)['remaining_tokens'], 9900)

    def test_concurrent_rate_limit(self):
        def admit(_):
            try:
                self.store.admit(self.a, 1)
                return True
            except LimitExceeded:
                return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            self.assertEqual(sum(pool.map(admit, range(15))), 5)

    def test_usage_estimates_no_sensitive_storage_and_restart(self):
        status, _, result = self.chat(prompt='private Unicode 世界')
        self.assertEqual(status, 200)
        usage = self.request('GET', '/usage', 'test-a')[2]
        tokens = estimate_tokens('private Unicode 世界') + estimate_tokens(result['response'])
        self.assertEqual(usage['estimated_tokens'], tokens)
        self.assertEqual(usage['estimated_cost_usd'], f'{tokens / 1_000_000:.6f}')
        self.assertTrue(usage['cost_is_estimate'])
        self.assertEqual(usage['reserved_tokens'], 0)
        self.assertEqual(UsageStore(self.path, lambda: self.now).usage(self.a), usage)
        self.assertNotIn(b'private Unicode', self.path.read_bytes())
        self.assertNotIn(b'test-a', self.path.read_bytes())
        self.assertEqual(self.request('GET', '/usage', 'test-b')[2]['estimated_tokens'], 0)

    def test_authenticated_failover_and_failed_request_accounting(self):
        def fail_openai(provider, prompt, timeout):
            if provider == 'OpenAI':
                raise ProviderFailure('upstream_503')
            from gateway.providers import simulate_response
            return simulate_response(prompt, provider)
        self.http.gateway.router = Router(simulator=fail_openai)
        self.assertEqual(self.chat()[2]['provider'], 'Anthropic')
        def fail_all(*args):
            raise ProviderFailure('upstream_503')
        self.http.gateway.router = Router(simulator=fail_all)
        before = self.store.usage(self.a)['estimated_tokens']
        self.assertEqual(self.chat()[0], 503)
        usage = self.store.usage(self.a)
        self.assertEqual(usage['failures'], 1)
        self.assertEqual(usage['requests'], 2)
        self.assertEqual(usage['estimated_tokens'], before)
        self.assertEqual(usage['reserved_tokens'], 0)

    def test_concurrent_settlement_and_exact_quota_boundary(self):
        tickets = [self.store.admit(self.a, 2000) for _ in range(5)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda ticket: self.store.finish(ticket, 2000), tickets))
        usage = self.store.usage(self.a)
        self.assertEqual(usage['estimated_tokens'], 10_000)
        self.assertEqual(usage['successes'], 5)
        self.assertEqual(usage['pending'], 0)
        self.now += 60
        with self.assertRaises(LimitExceeded) as caught:
            self.store.admit(self.a, 1)
        self.assertEqual(caught.exception.code, 'quota_exhausted')
        with self.assertRaises(ValueError):
            self.store.finish(tickets[0], 2000)

    def test_persistent_rate_and_completion_across_month_boundary(self):
        tickets = [self.store.admit(self.a, 10) for _ in range(5)]
        restarted = UsageStore(self.path, lambda: self.now)
        with self.assertRaises(LimitExceeded):
            restarted.admit(self.a, 10)
        self.now += 32 * 86400
        self.store.finish(tickets[0], 5)
        self.assertEqual(self.store.usage(self.a)['estimated_tokens'], 0)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT tokens FROM requests WHERE id=?',
                                        (tickets[0],)).fetchone()[0], 5)

    def test_authenticated_logs_do_not_contain_credentials_or_prompt(self):
        with self.assertLogs('gateway.routing', level='INFO') as logs:
            self.assertEqual(self.chat(prompt='sensitive private prompt')[0], 200)
        output = ''.join(logs.output)
        for secret in ('test-a', 'Bearer', 'sensitive private prompt'):
            self.assertNotIn(secret, output)

    def test_validation_and_database_failure_fail_closed(self):
        self.assertEqual(self.chat(prompt='')[0], 400)
        self.assertEqual(self.store.usage(self.a)['requests'], 0)
        self.http.gateway.usage_store.path = str(Path(self.temp.name) / 'missing' / 'db')
        self.assertEqual(self.chat()[0], 503)
        self.assertEqual(self.http.gateway.router.metrics()['providers']['OpenAI']['requests'], 0)
