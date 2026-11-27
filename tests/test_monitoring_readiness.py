import io 
from contextlib import redirect_stdout
from http.client import RemoteDisconnected
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from scripts import check_monitoring as monitoring


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.clock = patch.object(monitoring.time, 'monotonic', side_effect=lambda: self.now)
        self.sleep = patch.object(monitoring.time, 'sleep', side_effect=self.advance)
        self.clock.start()
        self.sleep.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.sleep.stop)

    def advance(self, seconds):
        self.now += seconds

    def test_transient_failures_recover_and_check_every_service(self):
        for error in (ConnectionResetError('private-token'), ConnectionRefusedError(),
                      URLError(ConnectionRefusedError()), TimeoutError(), RemoteDisconnected()):
            with self.subTest(error=type(error).__name__), patch.object(
                    monitoring, 'get', side_effect=[error, 'ok', 'ok', 'ok', 'ok', 'ok']) as get:
                monitoring.wait_for_readiness(timeout=5)
                self.assertEqual(get.call_count, 6)
                self.assertEqual([c.args[0] for c in get.call_args_list],
                                 list(monitoring.SERVICES.values()) * 2)

    def test_timeout_is_bounded_and_does_not_print_exception_text(self):
        output = io.StringIO()
        with patch.object(monitoring, 'get', side_effect=ConnectionResetError('private-token')) as get:
            with redirect_stdout(output), self.assertRaises(AssertionError) as error:
                monitoring.wait_for_readiness(timeout=2)
        self.assertEqual(self.now, 2)
        self.assertEqual(get.call_count, 6)
        self.assertIn('gateway', str(error.exception))
        self.assertIn('prometheus', str(error.exception))
        self.assertIn('grafana', str(error.exception))
        self.assertNotIn('private-token', str(error.exception) + output.getvalue())

    def test_probes_respect_remaining_deadline(self):
        def slow_get(url, timeout):
            self.assertLessEqual(timeout, 2)
            self.advance(timeout)
            raise TimeoutError()
        with patch.object(monitoring, 'get', side_effect=slow_get) as get:
            with self.assertRaisesRegex(AssertionError, 'timed out'):
                monitoring.wait_for_readiness(timeout=2)
        self.assertEqual(self.now, 2)
        self.assertEqual(get.call_count, 1)

    def test_unready_http_service_is_retried(self):
        error = HTTPError('http://localhost', 503, 'unavailable', {}, None)
        with patch.object(monitoring, 'get', side_effect=[error, 'ok', 'ok', 'ok', 'ok', 'ok']):
            monitoring.wait_for_readiness(timeout=2)
        self.assertEqual(self.now, 1)

    def test_permanent_http_errors_are_not_hidden(self):
        error = HTTPError('http://localhost', 401, 'unauthorized', {}, None)
        with patch.object(monitoring, 'get', side_effect=error) as get:
            with self.assertRaises(HTTPError):
                monitoring.wait_for_readiness(timeout=2)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.now, 0)

    def test_main_diagnoses_timeout_and_preserves_failure(self):
        with patch.object(monitoring, 'wait_for_readiness', side_effect=AssertionError('timeout')):
            with patch.object(monitoring, 'startup_diagnostics') as diagnostics:
                with self.assertRaisesRegex(AssertionError, 'timeout'):
                    monitoring.main()
        diagnostics.assert_called_once_with()

    def test_diagnostics_do_not_disclose_external_error_output(self):
        import subprocess
        output = io.StringIO()
        with patch.object(monitoring.subprocess, 'run', side_effect=subprocess.CalledProcessError(
                1, ['docker'], output='private-token', stderr='private-password')):
            with redirect_stdout(output):
                monitoring.startup_diagnostics()
        self.assertEqual(output.getvalue().count('state unavailable'), 3)
        self.assertNotIn('private-', output.getvalue())

    def test_diagnostics_allow_only_container_state(self):
        output = io.StringIO()
        results = [SimpleNamespace(stdout='container-id'),
                   SimpleNamespace(stdout='"running" 0 2')] * 3
        with patch.object(monitoring.subprocess, 'run', side_effect=results) as run:
            with redirect_stdout(output):
                monitoring.startup_diagnostics()
        self.assertEqual(output.getvalue().count('state="running" exit=0 restarts=2'), 3)
        for call in run.call_args_list:
            self.assertNotIn('logs', call.args[0])
            self.assertTrue(call.kwargs['capture_output'])
            self.assertEqual(call.kwargs['timeout'], 10)


if __name__ == '__main__':
    unittest.main()
