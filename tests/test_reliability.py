import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gateway.providers import PROVIDERS
from gateway.reliability import AllProvidersFailed, Policy, ProviderFailure, Router, Simulator
from gateway.service import chat
from tests import test_http


class ReliabilityTests(unittest.TestCase):
    def router(self, outcomes, **policy):
        self.calls = []
        self.now = 0

        def attempt(provider, prompt, timeout):
            self.calls.append(provider)
            value = outcomes.get(provider, 'success')
            if value != 'success':
                raise ProviderFailure(value)
            return 'ok'

        return Router({p: Policy(**policy) for p in PROVIDERS}, attempt, lambda: self.now)

    def test_retry_limit_and_order(self):
        router = self.router({'OpenAI': 'upstream_503'}, max_retries=2, failure_threshold=10)
        result = chat({'prompt': 'secret', 'provider': 'OpenAI'}, router)
        self.assertEqual(self.calls, ['OpenAI'] * 3 + ['Anthropic'])
        self.assertEqual(set(result), {'response', 'provider', 'status', 'response_time_ms'})
        self.assertEqual(result['provider'], 'Anthropic')
        self.assertEqual(router.metrics()['providers']['OpenAI']['failures'], 3)

    def test_timeout_and_live_recovery(self):
        with TemporaryDirectory(dir='.') as directory:
            path = Path(directory) / 'simulation.json'
            path.write_text(json.dumps({'OpenAI': ['timeout']}))
            sleeps = []
            router = Router(simulator=Simulator(path, sleeps.append))
            self.assertEqual(router.route('hi', 'OpenAI')[1], 'Anthropic')
            self.assertEqual(sleeps, [0.2, 0.2])
            path.write_text('{}')
            self.assertEqual(router.route('hi', 'OpenAI')[1], 'OpenAI')
            self.assertEqual(router.metrics()['providers']['OpenAI']['consecutive_failures'], 0)

    def test_circuit_transitions_and_failed_probe(self):
        outcomes = {'OpenAI': 'upstream_503'}
        router = self.router(outcomes, failure_threshold=1, recovery_seconds=5)
        router.route('hi', 'OpenAI')
        self.assertEqual(router.health['OpenAI'].state, 'OPEN')
        before = self.calls.count('OpenAI')
        router.route('hi', 'OpenAI')
        self.assertEqual(self.calls.count('OpenAI'), before)
        self.now = 5
        states = []
        original = router.simulator

        def probe(*args):
            if args[0] == 'OpenAI':
                states.append(router.health['OpenAI'].state)
            return original(*args)

        router.simulator = probe
        router.route('hi', 'OpenAI')
        self.assertEqual(states, ['HALF_OPEN'])
        self.assertEqual(router.health['OpenAI'].state, 'OPEN')
        outcomes['OpenAI'] = 'success'
        self.now = 10
        self.assertEqual(router.route('hi', 'OpenAI')[1], 'OpenAI')
        self.assertEqual(router.health['OpenAI'].state, 'CLOSED')
        self.assertTrue(router.metrics()['providers']['OpenAI']['healthy'])

    def test_single_probe_admission(self):
        router = self.router({})
        router.health['OpenAI'].state = 'HALF_OPEN'
        router.health['OpenAI'].probe_active = True
        self.assertEqual(router.route('hi', 'OpenAI')[1], 'Anthropic')
        self.assertNotIn('OpenAI', self.calls)

    def test_all_fail_and_http_metrics(self):
        router = self.router({p: 'upstream_503' for p in PROVIDERS}, max_retries=0)
        with self.assertRaises(AllProvidersFailed) as caught:
            router.route('hi', 'OpenAI')
        self.assertEqual(set(caught.exception.failures), set(PROVIDERS))
        with patch('gateway.server.DEFAULT_ROUTER', router):
            status, _, body = test_http.HTTPTests().request('POST', '/chat',
                '{"prompt":"hi","provider":"OpenAI"}', {'Content-Type': 'application/json'})
            self.assertEqual(status, 503)
            self.assertEqual(body['status'], 'error')
            status, _, body = test_http.HTTPTests().request('GET', '/metrics')
            self.assertEqual(status, 200)
            for values in body['providers'].values():
                self.assertEqual(values['requests'], values['successes'] + values['failures'])

    def test_logs_exclude_prompt(self):
        router = self.router({'OpenAI': 'upstream_503'})
        with self.assertLogs('gateway.routing', level='INFO') as logs:
            router.route('private credential prompt', 'OpenAI')
        self.assertNotIn('private credential prompt', ''.join(logs.output))
        self.assertTrue(any('failover' in entry for entry in logs.output))

    def test_stale_success_does_not_close_newly_opened_circuit(self):
        router = self.router({}, max_retries=0, failure_threshold=1)
        nested = False

        def attempt(provider, prompt, timeout):
            nonlocal nested
            if provider == 'OpenAI':
                if nested:
                    raise ProviderFailure('upstream_503')
                nested = True
                router.route('another request', 'OpenAI')
            return 'ok'

        router.simulator = attempt
        router.route('first request', 'OpenAI')
        self.assertEqual(router.health['OpenAI'].state, 'OPEN')
        self.assertEqual(router.health['OpenAI'].consecutive_failures, 1)
        self.assertEqual(router.health['OpenAI'].successes, 1)

    def test_invalid_policy(self):
        for values in ({'timeout_seconds': 0}, {'max_retries': -1},
                       {'max_retries': True}, {'recovery_seconds': float('nan')},
                       {'failure_threshold': 0}, {'max_retries': 11}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Policy(**values)

    def test_simulation_sequence_and_bad_config(self):
        with TemporaryDirectory(dir='.') as directory:
            path = Path(directory) / 'simulation.json'
            path.write_text('{"OpenAI":["503","success"]}')
            simulator = Simulator(path)
            with self.assertRaises(ProviderFailure):
                simulator('OpenAI', 'hi', 1)
            for _ in range(2):
                self.assertIn('Simulated', simulator('OpenAI', 'hi', 1))
            path.write_text('{bad')
            with self.assertRaisesRegex(ProviderFailure, 'simulation_config_error'):
                simulator('OpenAI', 'hi', 1)
