"""Exercise inference on disposable local gateways, with deterministic scheduler checks."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
from tempfile import mkdtemp
from threading import Barrier
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.jobs import JobQueue, QueueConfig
from gateway.providers import PROVIDERS, simulate_response
from gateway.security import Customer
from gateway.usage import UsageStore, estimate_tokens

CUSTOMERS = ("ENTERPRISE", "BUSINESS", "STARTER")
PENDING = {"queued", "running"}
METRIC_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? ([^ ]+)$")
METRIC_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def until(check, description, timeout=12):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        value = check()
        if value:
            return value
        sleep(0.02)
    raise AssertionError(f"Timed out waiting for {description}")


class Gateway:
    def __init__(self, folder):
        self.folder = folder
        folder.mkdir()
        self.db = folder / "gateway.sqlite3"
        self.scenario = folder / "providers.json"
        self.policy = folder / "policies.json"
        self.marker = folder / "crash-next"
        self.keys = {name: secrets.token_hex(24) for name in (*CUSTOMERS, "ADMIN", "METRICS")}
        self.process = None
        self.starts = 0
        self.outcomes({})

    def outcomes(self, outcomes):
        replacement = self.scenario.with_suffix(".next")
        replacement.write_text(json.dumps(outcomes), encoding="utf-8")
        replacement.replace(self.scenario)

    def policies(self, **overrides):
        self.policy.write_text(json.dumps({p: overrides for p in PROVIDERS}), encoding="utf-8")

    def start(self, workers, *flags):
        require(self.process is None, "Gateway is already running")
        env = {key: value for key, value in os.environ.items()
               if not (key.startswith("GATEWAY_") and key.endswith(("_API_KEY", "_API_KEY_FILE")))}
        env.update({f"GATEWAY_{name}_API_KEY": value for name, value in self.keys.items()})
        cmd = [sys.executable, "-m", "gateway.server", "--host", "127.0.0.1", "--port", "0",
               "--usage-db", str(self.db), "--workers", str(workers),
               "--simulation-file", str(self.scenario), "--worker-crash-file", str(self.marker)]
        if self.policy.exists():
            cmd += ["--config", str(self.policy)]
        self.starts += 1
        log = self.folder / f"gateway-{self.starts}.log"
        with log.open("wb") as output:
            self.process = subprocess.Popen(cmd + list(flags), cwd=ROOT, env=env, stdout=output,
                                            stderr=subprocess.STDOUT)

        def ready():
            require(self.process.poll() is None, f"Gateway startup failed. See {log}")
            with log.open(encoding="utf-8") as source:
                for line in source:
                    match = re.fullmatch(r"AI gateway listening on http://127\.0\.0\.1:(\d+)\n", line)
                    if match:
                        return int(match[1])

        self.port = until(ready, f"gateway startup ({log})")
        require(self.request("GET", "/health", tenant=None)[0] == 200, "Health endpoint failed")

    def stop(self, hard=False):
        if self.process is None:
            return
        process, self.process = self.process, None
        if hard:
            process.kill()
        else:
            process.terminate()
        try:
            status = process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise AssertionError(f"Gateway shutdown timed out. See {self.folder}")
        expected = -signal.SIGKILL if hard else 0
        require(status == expected, f"Unexpected gateway exit {status}. See {self.folder}")

    def request(self, method, path, tenant="ENTERPRISE", payload=None, idem=None):
        headers = {}
        if tenant is not None:
            headers["Authorization"] = "Bearer " + self.keys[tenant]
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if idem is not None:
            headers["Idempotency-Key"] = idem
        conn = HTTPConnection("127.0.0.1", self.port, timeout=12)
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            body = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode()
            return response.status, response.headers, body
        finally:
            conn.close()

    def submit(self, tenant, prompt, idem=None, priority=5):
        return self.request("POST", "/jobs", tenant,
                            {"provider": "OpenAI", "prompt": prompt, "priority": priority}, idem)

    def job(self, location, tenant="ENTERPRISE"):
        status, _, body = self.request("GET", location, tenant)
        require(status == 200, f"Job lookup returned {status}: {body}")
        return body

    def terminal(self, location, tenant="ENTERPRISE"):
        def check():
            row = self.job(location, tenant)
            return row if row["status"] not in PENDING else None

        return until(check, f"terminal state for {location}")

    def scrape(self):
        status, headers, body = self.request("GET", "/metrics/prometheus", "METRICS")
        require(status == 200 and headers.get_content_type() == "text/plain", "Scrape failed")
        return body


@contextmanager
def timed(name):
    started = monotonic()
    try:
        yield
    except BaseException:
        print(f"FAIL {name} ({monotonic() - started:.3f}s)", flush=True)
        raise
    print(f"PASS {name} ({monotonic() - started:.3f}s)", flush=True)


def load(gateway):
    gateway.policies(timeout_seconds=0.1)
    gateway.start(5)
    until(lambda: "gateway_workers_alive 5\n" in gateway.scrape(), "five worker threads")
    tenants = ["ENTERPRISE"] * 65 + ["BUSINESS"] * 30 + ["STARTER"] * 5
    barrier = Barrier(100)

    def submit(index):
        tenant = tenants[index]
        prompt = f"concurrency-{index:03d}"
        barrier.wait(timeout=12)
        status, headers, body = gateway.submit(tenant, prompt, idem=f"load-{index}")
        require(status == 202, f"Submission {index}: {status} {body}")
        return tenant, prompt, headers["Location"], body["id"]

    with timed("100 simultaneous HTTP submissions across three tenants"):
        with ThreadPoolExecutor(100) as clients:
            batch = list(clients.map(submit, range(100)))
        require(len({job_id for _, _, _, job_id in batch}) == 100, "Job IDs were duplicated")

    with timed("Five-worker drain and per-job atomic claiming"):
        def verify(job):
            tenant, prompt, path, _ = job
            row = gateway.terminal(path, tenant)
            status, _, result = gateway.request("GET", path + "/result", tenant)
            require((row["status"], row["attempts"]) == ("succeeded", 1),
                    "A job failed or used more than one claim")
            require(status == 200 and result["result"]["response"] == simulate_response(prompt, "OpenAI"),
                    "Job result did not match its submitted prompt")

        with ThreadPoolExecutor(20) as clients:
            list(clients.map(verify, batch))
        log = (gateway.folder / "gateway-1.log").read_text(encoding="utf-8")
        require(len(re.findall(r'"event": "provider_attempt"', log)) == 100, "Expected one provider call per job")
        require("gateway_jobs{state=\"succeeded\"} 100\n" in gateway.scrape(), "Incorrect success gauge")

    with timed("Concurrent duplicate replays, cross-tenant access and exact accounting"):
        tenant, prompt, path, _ = batch[0]

        def duplicate(_):
            status, headers, _ = gateway.submit(tenant, prompt, idem="load-0")
            require((status, headers["Location"]) == (200, path), "Replay changed status or location")

        with ThreadPoolExecutor(20) as clients:
            list(clients.map(duplicate, range(40)))
        require(gateway.submit(tenant, "changed", idem="load-0")[0] == 409, "Idempotency conflict was accepted")
        require(gateway.submit("BUSINESS", "rate-limit")[2].get("code") == "rate_limit_exceeded",
                "Business should be at its request limit")
        for method, endpoint in (("GET", path), ("GET", path + "/result"), ("DELETE", path)):
            require(gateway.request(method, endpoint, "BUSINESS")[0] == 404, "Cross-tenant job access")
        for caller in (None, "ADMIN", "METRICS"):
            require(gateway.request("GET", path, caller)[0] == 401, "Non-customer accessed job")
        for name in CUSTOMERS:
            prompts = [prompt for tenant, prompt, _, _ in batch if tenant == name]
            expected = sum(estimate_tokens(prompt) + estimate_tokens(simulate_response(prompt, "OpenAI"))
                           for prompt in prompts)
            status, _, usage = gateway.request("GET", "/usage", name)
            observed = (usage["requests"], usage["successes"], usage["estimated_tokens"], usage["reserved_tokens"])
            require(status == 200 and observed == (len(prompts), len(prompts), expected, 0),
                    f"{name} ledger mismatch: {observed}")
            print(f"  {name}: {len(prompts)} jobs, {expected} estimated tokens")


def values(scrape):
    parsed = {}
    for line in scrape.splitlines():
        if not line or line.startswith("#"):
            continue
        match = METRIC_SAMPLE.fullmatch(line)
        require(match is not None, f"Invalid sample: {line}")
        name, labels, raw = match.groups()
        label_matches = list(METRIC_LABEL.finditer(labels or ""))
        require(",".join(match[0] for match in label_matches) == (labels or ""), f"Invalid labels: {line}")
        series = name, tuple(sorted(match.groups() for match in label_matches))
        require(series not in parsed, f"Duplicate series: {series}")
        parsed[series] = float(raw)
    return parsed


def metric(samples, name, **labels):
    return samples[name, tuple(sorted(labels.items()))]


def persisted_claim(gateway, location):
    with closing(sqlite3.connect(gateway.db)) as db:
        return db.execute("SELECT status, lease_token, lease_until FROM inference_jobs WHERE id=?",
                          (location.rsplit("/", 1)[1],)).fetchone()


def crash_and_restart(gateway):
    flags = ("--job-lease-seconds", "0.6", "--job-retry-base", "0.15")
    gateway.start(1, *flags)
    with timed("Worker thread crash, expired lease recovery and stale result rejection"):
        gateway.marker.touch()
        status, headers, _ = gateway.submit("ENTERPRISE", "crash marker", idem="marker")
        require(status == 202, "Crash job was rejected")
        path = headers["Location"]

        def running():
            row = persisted_claim(gateway, path)
            return row if row[0] == "running" and row[1] else None

        _, old_token, _ = until(running, "abandoned claim")
        row = gateway.terminal(path)
        require((row["status"], row["attempts"]) == ("succeeded", 2), "Abandoned claim was not recovered")
        status, _, result = gateway.request("GET", path + "/result")
        require(status == 200 and result["result"]["response"].endswith("crash marker"),
                "Recovered result was missing")
        queue = JobQueue(UsageStore(gateway.db))
        require(not queue.finish(row["id"], old_token, result={"response": "stale"}, tokens=1),
                "Expired worker replaced recovered result")
        require(not queue.renew(row["id"], old_token), "Expired worker renewed its lease")
        require(gateway.request("GET", path + "/result")[2] == result, "Recovered result changed")
        states = values(gateway.scrape())
        require(metric(states, "gateway_job_events_total", event="lease_expired") == 1,
                "Expected one lease expiration")

    with timed("SIGKILL during provider execution and recovery on application restart"):
        gateway.stop()
        gateway.policies(timeout_seconds=5, max_retries=0, failure_threshold=100)
        gateway.outcomes({"OpenAI": ["timeout"]})
        gateway.start(1, *flags)
        status, headers, _ = gateway.submit("ENTERPRISE", "restart crash", idem="killed")
        require(status == 202, "Restart job was rejected")
        killed = headers["Location"]
        log = gateway.folder / "gateway-2.log"
        until(lambda: '"event": "provider_attempt"' in log.read_text(encoding="utf-8"),
              "worker inside simulated provider timeout")
        require(gateway.job(killed)["status"] == "running", "Kill target finished prematurely")
        gateway.stop(hard=True)
        gateway.outcomes({})
        gateway.start(5, *flags)
        row = gateway.terminal(killed)
        require((row["status"], row["attempts"]) == ("succeeded", 2), "Restart failed to recover running job")
        require(gateway.request("GET", path + "/result")[2] == result, "Historical result was lost")
        status, headers, replay = gateway.submit("ENTERPRISE", "restart crash", idem="killed")
        require((status, headers["Location"], replay["attempts"]) == (200, killed, 2),
                "Idempotency did not survive restart")
        _, _, usage = gateway.request("GET", "/usage")
        require((usage["requests"], usage["successes"], usage["reserved_tokens"]) == (2, 2, 0),
                "Settlements did not survive restart")


def failures(gateway):
    gateway.policies(max_retries=1, failure_threshold=100)
    gateway.outcomes({"OpenAI": ["503", "success"]})
    gateway.start(5, "--job-retry-base", "0.15", "--job-retry-cap", "0.3")
    with timed("Simulated 503, provider retry and cross-provider failover"):
        for suffix, expected in (("retry", "OpenAI"), ("failover", "Anthropic")):
            if suffix == "failover":
                gateway.outcomes({"OpenAI": ["503"]})
            status, headers, _ = gateway.submit("ENTERPRISE", f"scenario-{suffix}")
            require(status == 202, "Failure scenario was rejected")
            path = headers["Location"]
            row = gateway.terminal(path)
            status, _, body = gateway.request("GET", path + "/result")
            require(row["attempts"] == 1 and status == 200 and body["result"]["provider"] == expected,
                    f"Unexpected outcome for {suffix}: {body}")
        samples = values(gateway.scrape())
        for name, count in (("retries_total", 2), ("provider_failures_total", 3), ("failovers_total", 1)):
            require(metric(samples, "gateway_" + name, provider="OpenAI") == count, f"Incorrect {name}")

    with timed("All providers down, exponential retries and durable dead-letter"):
        gateway.outcomes({p: ["503"] for p in PROVIDERS})
        started = monotonic()
        status, headers, _ = gateway.submit("ENTERPRISE", "all providers offline", idem="dead")
        require(status == 202, "Dead-letter scenario was rejected")
        path = headers["Location"]
        dead = gateway.terminal(path)
        elapsed = monotonic() - started
        require((dead["status"], dead["attempts"], dead["error"]) == ("dead_letter", 3, "providers_unavailable"),
                f"Incorrect dead-letter state: {dead}")
        require(dead["updated_at"] - dead["created_at"] >= 0.45 - 1e-5,
                "Job exhausted attempts before exponential backoff elapsed")
        require(gateway.request("GET", path + "/result")[0] == 409, "Dead letter exposed a result")
        require(gateway.request("DELETE", path)[0] == 409, "Dead letter was overwritten by cancellation")
        status, _, usage = gateway.request("GET", "/usage")
        require(status == 200 and (usage["requests"], usage["failures"], usage["reserved_tokens"]) == (3, 1, 0),
                "Dead letter was double-counted or left a reservation")
        print(f"  Dead-letter terminal state after {elapsed:.3f}s")
        gateway.stop()
        gateway.start(0)
        require(gateway.job(path) == dead, "Dead letter changed after restart")
        status, headers, replay = gateway.submit("ENTERPRISE", "all providers offline", idem="dead")
        require((status, headers["Location"], replay) == (200, path, dead), "Dead-letter replay changed identity")

    return gateway.scrape()


def capacity_and_overlap(gateway):
    gateway.policies(timeout_seconds=2, max_retries=0, failure_threshold=100)
    gateway.outcomes({"OpenAI": ["timeout"]})
    gateway.start(0, "--queue-capacity", "5", "--tenant-queue-capacity", "3")
    with timed("Concurrent first-submission deduplication and queue backpressure"):
        barrier = Barrier(20)

        def duplicate(_):
            barrier.wait(timeout=12)
            return gateway.submit("ENTERPRISE", "first duplicate race", idem="same")

        with ThreadPoolExecutor(20) as clients:
            duplicates = list(clients.map(duplicate, range(20)))
        paths = {headers["Location"] for _, headers, _ in duplicates}
        require(len(paths) == 1 and sorted(s for s, _, _ in duplicates) == [200] * 19 + [202],
                "Concurrent first submissions did not produce exactly one job")
        path = paths.pop()
        status, headers, _ = gateway.submit("BUSINESS", "first duplicate race", idem="same")
        require(status == 202 and headers["Location"] != path, "Idempotency crossed tenant boundaries")
        pending = [("ENTERPRISE", path), ("BUSINESS", headers["Location"])]
        for tenant in ("ENTERPRISE", "ENTERPRISE", "BUSINESS"):
            status, headers, _ = gateway.submit(tenant, f"backlog-{len(pending)}")
            require(status == 202, "Queue rejected a request below capacity")
            pending.append((tenant, headers["Location"]))
            if len(pending) == 4:
                status, headers, body = gateway.submit("ENTERPRISE", "tenant overflow")
                require((status, headers["Retry-After"], body["code"]) == (429, "1", "tenant_queue_full"),
                        "Missing tenant-capacity backpressure")
        status, headers, body = gateway.submit("BUSINESS", "global overflow")
        require((status, headers["Retry-After"], body["code"]) == (429, "1", "queue_full"),
                "Missing global-capacity backpressure")
        require(gateway.request("GET", path + "/result")[0] == 202, "Queued job should be pending")
        _, _, usage = gateway.request("GET", "/usage")
        require((usage["requests"], usage["pending"]) == (3, 3), "Duplicate or rejected requests reserved quota")

    with timed("Five jobs held concurrently, fencing cancellation and graceful drain"):
        gateway.stop()
        gateway.start(5, "--queue-capacity", "5", "--tenant-queue-capacity", "3",
                      "--job-lease-seconds", "0.6")

        def active():
            samples = values(gateway.scrape())
            return (metric(samples, "gateway_workers_alive") == 5
                    and metric(samples, "gateway_jobs", state="running") == 5)

        until(active, "all five workers holding distinct jobs")
        require(all(gateway.job(location, tenant)["attempts"] == 1 for tenant, location in pending),
                "Concurrent claims were duplicated")
        status, _, body = gateway.request("DELETE", path)
        require(status == 200 and body["status"] == "cancelled", "Running job was not cancelled")
        status, headers, _ = gateway.submit("STARTER", "capacity reclaimed after cancellation")
        require(status == 202, "Cancellation did not release capacity")
        late = headers["Location"]
        gateway.stop()
        gateway.start(5)
        require(gateway.job(path)["status"] == "cancelled", "Late provider result overwrote cancellation")
        require(gateway.request("GET", path + "/result")[0] == 409, "Cancelled job exposed a result")
        for tenant, location in pending[1:] + [("STARTER", late)]:
            row = gateway.terminal(location, tenant)
            require((row["status"], row["attempts"]) == ("succeeded", 1), "Job was lost or lease expired during drain")
        _, _, usage = gateway.request("GET", "/usage")
        require((usage["requests"], usage["successes"], usage["failures"], usage["reserved_tokens"]) == (3, 2, 1, 0),
                "Cancellation accounting mismatch")
        require(metric(values(gateway.scrape()), "gateway_job_events_total", event="lease_expired") == 0,
                "Heartbeat missed a live claim")


def scheduling(folder):
    with timed("Priority ordering and starvation protection with a controlled clock"):
        now = 1_800_000_000.0
        usage = UsageStore(folder / "schedule.sqlite3", lambda: now)
        queue = JobQueue(usage, QueueConfig(aging_seconds=5))
        tenant = Customer("priority-test", "Enterprise")

        def submit(priority):
            return queue.submit(tenant, {"provider": "OpenAI", "prompt": "age test", "priority": priority})[0]["id"]

        oldest = submit(0)
        for _ in range(4):
            now += 1
            high = submit(9)
            claim = queue.claim()
            require(claim["id"] == high, "Fresh high-priority job lost to unaged low-priority work")
            require(queue.finish(high, claim["lease_token"], result={"response": "mock"}, tokens=1),
                    "Priority control result could not commit")
        now += 1
        submit(9)
        claim = queue.claim()
        require(claim["id"] == oldest, "Old low-priority job was starved by high-priority arrivals")


def monitoring(gateway, durable_scrape, artifacts):
    with timed("Authenticated Prometheus exposition and Grafana dashboard configuration"):
        for caller in (None, *CUSTOMERS, "ADMIN"):
            require(gateway.request("GET", "/metrics/prometheus", caller)[0] == 401, "Scrape authorization bypass")
        scrape = gateway.scrape()
        require(all(key not in scrape for key in gateway.keys.values()), "Prometheus exposed an API key")
        require(not re.search(r"/jobs/[a-f0-9]{32}|concurrency-\d{3}|enterprise-demo|business-demo|starter-demo", scrape),
                "Prometheus exposed customer data or high-cardinality job IDs")
        samples = values(scrape)
        require(metric(samples, "gateway_workers_configured") == 5, "Worker gauge mismatch")
        require(metric(samples, "gateway_queue_capacity") == 1000, "Capacity gauge mismatch")
        require(metric(samples, "gateway_job_events_total", event="submitted") == 100, "Submitted counter mismatch")
        events = values(durable_scrape)
        require(metric(events, "gateway_job_events_total", event="retried") == 2, "Retry counter did not persist")
        require(metric(events, "gateway_job_events_total", event="dead_letter") == 1, "Dead-letter counter did not persist")
        require(metric(events, "gateway_jobs", state="dead_letter") == 1, "Dead-letter gauge mismatch")
        (artifacts / "gateway.prom").write_text(scrape, encoding="utf-8")
        (artifacts / "restarted-gateway.prom").write_text(durable_scrape, encoding="utf-8")

        dashboard = json.loads((ROOT / "monitoring/grafana/dashboards/gateway.json").read_text())
        panels = dashboard["panels"]
        require(dashboard["uid"] == "ai-gateway" and len({p["id"] for p in panels}) == len(panels),
                "Invalid Grafana UID or duplicate panel ID")
        for panel in panels:
            require(panel["datasource"]["uid"] == "gateway-prometheus" and bool(panel["targets"]),
                    f"Invalid panel configuration: {panel['title']}")
        inference = [p for p in panels if p["title"].startswith(("Inference ", "Oldest ready inference"))]
        require(len(inference) == 6, "Incorrect number of inference panels")
        names = {name for name, _ in samples}
        for panel in inference:
            for target in panel["targets"]:
                referenced = set(re.findall(r"\bgateway_[a-zA-Z_0-9]+", target["expr"]))
                require(referenced and referenced <= names, f"Missing scraped metrics for panel {panel['title']}")
        subprocess.run([sys.executable, "scripts/validate_incident_dashboard.py"], cwd=ROOT, check=True)


@contextmanager
def instance(path):
    gateway = Gateway(path)
    try:
        yield gateway
    finally:
        gateway.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promtool", help="Optional path to a Prometheus promtool executable")
    options = parser.parse_args()
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    artifacts = Path(mkdtemp(prefix="inference-validation-", dir=scratch))
    print(f"Validation artifacts: {artifacts}", flush=True)
    started = monotonic()
    with instance(artifacts / "load") as gateway:
        load(gateway)
        with instance(artifacts / "crash") as crash_gateway:
            crash_and_restart(crash_gateway)
        with instance(artifacts / "provider") as provider_gateway:
            durable_scrape = failures(provider_gateway)
        with instance(artifacts / "capacity") as capacity_gateway:
            capacity_and_overlap(capacity_gateway)
        scheduling(artifacts)
        monitoring(gateway, durable_scrape, artifacts)
    if options.promtool:
        with timed("Promtool exposition validation"):
            for filename in ("gateway.prom", "restarted-gateway.prom"):
                with (artifacts / filename).open("rb") as source:
                    subprocess.run([options.promtool, "check", "metrics"], stdin=source, check=True)
    print(f"PASS all live integration scenarios ({monotonic() - started:.3f}s)", flush=True)


if __name__ == "__main__":
    main()
