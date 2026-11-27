from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json 
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
from time import monotonic, sleep
import unittest

from gateway.jobs import JobConflict, JobQueue, QueueConfig
from gateway.providers import simulate_response
from gateway.reliability import Policy, Router, Simulator
from gateway.security import Customer, KeyRegistry
from gateway.server import create_server
from gateway.usage import LimitExceeded, UsageStore
from gateway.workers import WorkerPool

PAYLOAD = {"prompt": "hello", "provider": "OpenAI"}
ALICE = Customer("alice", "Enterprise")
BOB = Customer("bob", "Business")


def wait_for(predicate, timeout=5):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        value = predicate()
        if value:
            return value
        sleep(0.01)
    raise AssertionError("Timed out waiting for inference state")


class QueueTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=".")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "usage.db"
        self.now = 1_800_000_000
        self.usage = UsageStore(self.path, lambda: self.now)
        self.config = QueueConfig(capacity=4, tenant_capacity=3, max_attempts=3,
                                  lease_seconds=2, aging_seconds=5, retry_base=1, retry_cap=2)
        self.queue = JobQueue(self.usage, self.config)

    def test_concurrent_idempotency_and_tenant_scope(self):
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: self.queue.submit(ALICE, PAYLOAD, "same"), range(16)))
        job, _ = results[0]
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual({row["id"] for row, _ in results}, {job["id"]})
        self.assertEqual(self.usage.usage(ALICE)["requests"], 1)
        self.assertEqual(self.queue.get(BOB.customer_id, job["id"]), (None, None))
        self.assertIsNone(self.queue.cancel(BOB.customer_id, job["id"]))
        other, created = self.queue.submit(BOB, PAYLOAD, "same")
        self.assertTrue(created)
        self.assertNotEqual(other["id"], job["id"])
        with self.assertRaises(JobConflict):
            self.queue.submit(ALICE, dict(PAYLOAD, priority=9), "same")

    def test_atomic_claim_across_instances_and_accounting(self):
        job, _ = self.queue.submit(ALICE, PAYLOAD)
        second = JobQueue(UsageStore(self.path, lambda: self.now), self.config)
        with ThreadPoolExecutor(8) as pool:
            claims = list(pool.map(lambda i: (second if i % 2 else self.queue).claim(), range(12)))
        claims = [claim for claim in claims if claim]
        self.assertEqual(len(claims), 1)
        token = claims[0]["lease_token"]
        result = {"response": "ok"}
        self.assertTrue(second.finish(job["id"], token, result=result, tokens=2))
        self.assertFalse(self.queue.finish(job["id"], token, result=result, tokens=2))
        self.assertEqual(self.queue.get(ALICE.customer_id, job["id"])[1], result)
        usage = self.usage.usage(ALICE)
        self.assertEqual((usage["requests"], usage["successes"], usage["estimated_tokens"]), (1, 1, 2))

    def test_expired_and_reassigned_lease_fences_old_worker(self):
        row, _ = self.queue.submit(ALICE, PAYLOAD)
        job_id = row["id"]
        old = self.queue.claim()["lease_token"]
        self.now += 2
        self.assertFalse(self.queue.renew(job_id, old))
        self.assertFalse(self.queue.finish(job_id, old, result={}, tokens=1))
        self.assertIsNone(self.queue.claim())
        self.now += 1
        new = self.queue.claim()["lease_token"]
        self.assertNotEqual(old, new)
        self.assertFalse(self.queue.finish(job_id, old, result={}, tokens=1))
        self.assertTrue(self.queue.finish(job_id, new, result={}, tokens=3))
        self.assertEqual(self.usage.usage(ALICE)["estimated_tokens"], 3)
        self.assertIn(b'event="lease_expired"} 1', self.queue.prometheus())

    def test_process_kill_recovers_persisted_claim(self):
        job, _ = self.queue.submit(ALICE, PAYLOAD)
        script = """
import os, signal, sys
from gateway.jobs import JobQueue, QueueConfig
from gateway.usage import UsageStore

queue = JobQueue(UsageStore(sys.argv[1], lambda: 1_800_000_000),
                 QueueConfig(lease_seconds=2))
assert queue.claim()["id"] == sys.argv[2]
os.kill(os.getpid(), signal.SIGKILL)
"""
        process = subprocess.run([sys.executable, "-c", script, str(self.path), job["id"]], timeout=5)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        self.assertEqual(self.queue.get(ALICE.customer_id, job["id"])[0]["status"], "running")
        reopened = JobQueue(UsageStore(self.path, lambda: self.now), self.config)
        self.now += 2
        self.assertIsNone(reopened.claim())
        self.now += 1
        claim = reopened.claim()
        self.assertEqual((claim["id"], claim["attempts"]), (job["id"], 2))
        self.assertTrue(reopened.finish(job["id"], claim["lease_token"], result={"response": "ok"}, tokens=2))
        self.assertEqual(self.usage.usage(ALICE)["requests"], 1)

    def test_exponential_backoff_and_durable_dead_letter(self):
        row, _ = self.queue.submit(ALICE, PAYLOAD, "dead")
        job_id = row["id"]
        for delay in (1, 2, None):
            claim = self.queue.claim()
            self.assertTrue(self.queue.finish(job_id, claim["lease_token"], error="providers_unavailable"))
            row, result = self.queue.get(ALICE.customer_id, job_id)
            self.assertIsNone(result)
            if delay:
                self.assertEqual(row["available_at"], self.now + delay)
                self.assertIsNone(self.queue.claim())
                self.assertGreater(self.usage.usage(ALICE)["reserved_tokens"], 0)
                self.now += delay
        reopened = JobQueue(UsageStore(self.path, lambda: self.now), self.config)
        self.assertEqual(reopened.get(ALICE.customer_id, job_id)[0]["status"], "dead_letter")
        self.assertFalse(reopened.submit(ALICE, PAYLOAD, "dead")[1])
        usage = self.usage.usage(ALICE)
        self.assertEqual((usage["requests"], usage["failures"], usage["reserved_tokens"]), (1, 1, 0))

    def test_priority_and_aging(self):
        low, _ = self.queue.submit(ALICE, dict(PAYLOAD, priority=0))
        high, _ = self.queue.submit(ALICE, dict(PAYLOAD, priority=9))
        self.assertEqual(self.queue.claim()["id"], high["id"])
        self.now += 5
        self.queue.submit(BOB, dict(PAYLOAD, priority=9))
        self.assertEqual(self.queue.claim()["id"], low["id"])

    def test_capacity_cancel_and_replay(self):
        first, _ = self.queue.submit(ALICE, PAYLOAD, "first")
        self.queue.submit(ALICE, PAYLOAD)
        self.queue.submit(ALICE, PAYLOAD)
        with self.assertRaises(LimitExceeded) as error:
            self.queue.submit(ALICE, PAYLOAD)
        self.assertEqual(error.exception.code, "tenant_queue_full")
        self.queue.submit(BOB, PAYLOAD)
        with self.assertRaises(LimitExceeded) as error:
            self.queue.submit(BOB, PAYLOAD)
        self.assertEqual(error.exception.code, "queue_full")
        self.assertFalse(self.queue.submit(ALICE, PAYLOAD, "first")[1])
        claim = self.queue.claim()
        self.assertEqual(self.queue.cancel(ALICE.customer_id, claim["id"])["status"], "cancelled")
        self.assertEqual(self.queue.cancel(ALICE.customer_id, claim["id"])["status"], "cancelled")
        self.assertFalse(self.queue.finish(claim["id"], claim["lease_token"], result={}, tokens=1))
        self.queue.submit(BOB, PAYLOAD)
        self.assertEqual(self.usage.usage(ALICE)["requests"], 3)
        self.assertEqual(self.usage.usage(ALICE)["failures"], 1)
        self.assertEqual(self.queue.get(ALICE.customer_id, first["id"])[0]["id"], first["id"])

    def test_sync_and_async_share_rate_and_reservation(self):
        starter = Customer("starter", "Starter")
        for _ in range(4):
            request = self.usage.admit(starter, 5)
            self.usage.finish(request, 5)
        self.queue.submit(starter, PAYLOAD, "fifth")
        with self.assertRaises(LimitExceeded) as error:
            self.usage.admit(starter, 5)
        self.assertEqual(error.exception.code, "rate_limit_exceeded")
        self.assertFalse(self.queue.submit(starter, PAYLOAD, "fifth")[1])


class WorkerAndHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=".")
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.usage = UsageStore(self.folder / "usage.db")
        self.config = QueueConfig(lease_seconds=0.3, retry_base=0.05, retry_cap=0.1)

    def server(self, **kwargs):
        server = create_server(port=0, usage_store=self.usage, queue_config=self.config,
                               keys=KeyRegistry({"alice": ALICE, "bob": BOB}, "admin"),
                               metrics_key="metrics", **kwargs)
        thread = Thread(target=server.serve_forever)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join()
            server.server_close()

        self.addCleanup(cleanup)
        return server

    def request(self, server, method, path, key="alice", payload=None, idem=None):
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(conn.close)
        headers = {"Authorization": "Bearer " + key}
        if idem is not None:
            headers["Idempotency-Key"] = idem
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = conn.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), json.loads(body) if path != "/metrics/prometheus" else body

    def test_http_completed_job_and_metrics(self):
        server = self.server()
        status, headers, row = self.request(server, "POST", "/jobs", payload=PAYLOAD, idem="one")
        self.assertEqual(status, 202)
        path = headers["Location"]
        self.assertEqual(path, "/jobs/" + row["id"])
        self.assertEqual(self.request(server, "GET", path, key="invalid")[0], 401)
        self.assertEqual(self.request(server, "GET", path, key="admin")[0], 401)
        for method, endpoint in (("GET", path), ("DELETE", path), ("GET", path + "/result")):
            self.assertEqual(self.request(server, method, endpoint, key="bob")[0], 404)

        def result():
            status, _, body = self.request(server, "GET", path + "/result")
            self.assertIn(status, (200, 202))
            return body if status == 200 else None

        self.assertEqual(wait_for(result)["result"]["response"], simulate_response("hello", "OpenAI"))
        self.assertEqual(self.request(server, "DELETE", path)[0], 409)
        self.assertEqual(self.request(server, "POST", "/jobs", payload=PAYLOAD, idem="one")[0], 200)
        status, _, scrape = self.request(server, "GET", "/metrics/prometheus", key="metrics")
        self.assertEqual(status, 200)
        for value in (b'gateway_jobs{state="succeeded"} 1', b'gateway_workers_alive 2',
                      b'route="/jobs/{id}/result"', b"# HELP gateway_job_events_total ",
                      b"# HELP gateway_workers_alive "):
            self.assertIn(value, scrape)
        self.assertNotIn(row["id"].encode(), scrape)
        self.assertNotIn(b"alice", scrape)

    def test_http_pending_cancel_validation_and_backpressure(self):
        self.config = QueueConfig(capacity=1)
        server = self.server(worker_count=0)
        for payload in (dict(PAYLOAD, priority=True), dict(PAYLOAD, tenant="bob"),
                        dict(PAYLOAD, prompt="\ud800")):
            self.assertEqual(self.request(server, "POST", "/jobs", payload=payload)[0], 400)
        self.assertEqual(self.request(server, "POST", "/jobs", payload=PAYLOAD, idem="bad key")[0], 400)
        _, headers, _ = self.request(server, "POST", "/jobs", payload=PAYLOAD, idem="one")
        path = headers["Location"]
        self.assertEqual(self.request(server, "GET", path + "/result")[0], 202)
        status, headers, body = self.request(server, "POST", "/jobs", payload=PAYLOAD)
        self.assertEqual((status, headers["Retry-After"], body["code"]), (429, "1", "queue_full"))
        self.assertEqual(self.request(server, "POST", "/jobs", payload=dict(PAYLOAD, prompt="different"),
                                      idem="one")[0], 409)
        self.assertEqual(self.request(server, "DELETE", path)[2]["status"], "cancelled")
        self.assertEqual(self.request(server, "GET", path + "/result")[0], 409)
        self.assertEqual(self.request(server, "POST", "/jobs", payload=PAYLOAD)[0], 202)

    def test_worker_crash_and_provider_outage(self):
        marker = self.folder / "crash"
        marker.touch()
        scenario = self.folder / "providers.json"
        scenario.write_text('{"OpenAI":["503"],"Anthropic":["503"],"Gemini":["503"]}')
        router = Router({p: Policy(max_retries=0, failure_threshold=100)
                         for p in ("OpenAI", "Anthropic", "Gemini")}, Simulator(scenario))
        server = self.server(router=router, worker_crash_file=marker)
        _, headers, row = self.request(server, "POST", "/jobs", payload=PAYLOAD)
        wait_for(lambda: server.jobs.get(ALICE.customer_id, row["id"])[0]["status"] == "dead_letter")
        self.assertFalse(marker.exists())
        status, _, result = self.request(server, "GET", headers["Location"] + "/result")
        self.assertEqual((status, result["attempts"], result["error"]), (409, 3, "providers_unavailable"))
        self.assertIn(b'event="lease_expired"} 1', server.jobs.prometheus())
        self.assertEqual(self.usage.usage(ALICE)["estimated_tokens"], 0)
        scenario.write_text("{}")
        _, _, row = self.request(server, "POST", "/jobs", payload=PAYLOAD)
        wait_for(lambda: server.jobs.get(ALICE.customer_id, row["id"])[0]["status"] == "succeeded")

    def test_workers_execute_distinct_jobs_concurrently(self):
        overlap = Barrier(2)

        def simulator(provider, prompt, timeout):
            overlap.wait(3)
            return simulate_response(prompt, provider)

        queue = JobQueue(self.usage, self.config)
        jobs = [queue.submit(ALICE, dict(PAYLOAD, prompt=prompt))[0] for prompt in ("one", "two")]
        with_results = [job["id"] for job in jobs]
        pool = WorkerPool(queue, Router(simulator=simulator))
        self.addCleanup(pool.close)
        wait_for(lambda: all(queue.get(ALICE.customer_id, ident)[1] for ident in with_results))
        self.assertEqual(self.usage.usage(ALICE)["successes"], 2)

    def test_heartbeat_and_graceful_shutdown(self):
        entered, release = Event(), Event()

        def simulator(provider, prompt, timeout):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timed out")
            return simulate_response(prompt, provider)

        queue = JobQueue(self.usage, self.config)
        first, _ = queue.submit(ALICE, PAYLOAD)
        pool = WorkerPool(queue, Router(simulator=simulator), count=1)
        self.addCleanup(pool.close)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        sleep(self.config.lease_seconds * 1.5)
        self.assertIsNone(queue.claim())
        self.assertEqual(queue.get(ALICE.customer_id, first["id"])[0]["attempts"], 1)
        second, _ = queue.submit(ALICE, PAYLOAD)
        drain = Thread(target=pool.close)
        drain.start()
        try:
            self.assertTrue(pool.stopping.wait(1))
            self.assertTrue(drain.is_alive())
        finally:
            release.set()
            drain.join(5)
        self.assertFalse(drain.is_alive())
        self.assertEqual(queue.get(ALICE.customer_id, first["id"])[0]["status"], "succeeded")
        self.assertEqual(queue.get(ALICE.customer_id, second["id"])[0]["status"], "queued")
