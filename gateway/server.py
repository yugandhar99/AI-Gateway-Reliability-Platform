"""JSON HTTP adapter implemented with Python's standard library."""

import argparse
import json
import logging
import re
import signal
import sqlite3
from threading import Thread
from time import perf_counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from gateway.jobs import JobConflict, JobQueue, QueueConfig
from gateway.workers import WorkerPool
from gateway.incidents import IncidentEngine, IncidentError
from gateway.providers import PROVIDERS
from gateway.service import DEFAULT_ROUTER, ValidationError, customer_chat
from gateway.reliability import AllProvidersFailed, Policy, Router, Simulator

from gateway.security import AuthenticationError, KeyRegistry, environment_key
from gateway.usage import LimitExceeded, UsageStore

MAX_BODY_BYTES = 65_536
ROUTES = {"/health": "GET", "/providers": "GET", "/chat": "POST", "/metrics": "GET", "/usage": "GET", "/metrics/prometheus": "GET", "/jobs": "POST"}
JOB_PATH = re.compile(r"/jobs/([0-9a-f]{32})(/result)?")

ROUTES.update({"/incidents":"GET", "/incidents/recommendations":"GET",
               "/incidents/evaluate":"POST", "/incidents/approve":"POST",
               "/incidents/execute":"POST", "/incidents/verify":"POST"})


class GatewayHandler(BaseHTTPRequestHandler):
    def _router(self):
        return getattr(self.server, "router", DEFAULT_ROUTER)

    def log_message(self, format, *args):
        # The default access log includes user-controlled URLs.
        pass

    def _json(self, status, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._response_status = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status, message, headers=None):
        self._json(status, {"status": "error", "error": message}, headers)

    def send_error(self, code, message=None, explain=None):
        """Keep errors from the underlying HTTP parser in JSON too."""
        self._error(code, message or HTTPStatus(code).phrase)

    def _dispatch(self):
        started = perf_counter()
        self._response_status = 500
        try:
            self._dispatch_errors()
        finally:
            path = urlsplit(self.path).path
            match = JOB_PATH.fullmatch(path)
            route = path if path in ROUTES else 'unknown'
            if match:
                route = '/jobs/{id}' + (match[2] or '')
            method = self.command if self.command in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS') else 'OTHER'
            self._router().observability.observe(route, method, self._response_status, perf_counter() - started)
            if path == '/chat' and getattr(self.server, 'incidents', None) is not None:
                try:
                    self.server.incidents.evaluate()
                except sqlite3.Error:
                    logging.getLogger('gateway.incidents').error('Incident persistence unavailable')

    def _dispatch_errors(self):
        try:
            self._dispatch_authenticated()
        except AuthenticationError:
            self._error(401, "Missing or invalid bearer credentials.", {"WWW-Authenticate": "Bearer"})
        except LimitExceeded as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            self._json(429, {"status": "error", "error": str(exc), "code": exc.code}, headers)
        except ValidationError as exc:
            self._error(400, str(exc))
        except (IncidentError, JobConflict) as exc:
            self._error(409, str(exc))
        except sqlite3.Error:
            self._error(503, "Persistent application storage is unavailable. Retry later.")

    def _authenticate(self, admin=False):
        registry = getattr(self.server, "keys", None)
        if registry is None:
            raise AuthenticationError()
        return registry.authenticate(self.headers, admin=admin)

    def _dispatch_authenticated(self):
        path = urlsplit(self.path).path
        allowed = ROUTES.get(path)
        job_path = JOB_PATH.fullmatch(path)
        if job_path:
            self._handle_job(self._authenticate(), job_path)
        elif allowed is None:
            self._error(404, "Endpoint not found.")
        elif self.command != allowed:
            self._error(405, f"Use {allowed} for {path}.", {"Allow": allowed})
        elif path.startswith('/incidents'):
            self._authenticate(admin=True)
            if getattr(self.server, 'incidents', None) is None:
                self._error(503, 'Incident engine unavailable.')
            elif self.command == 'GET':
                params = parse_qs(urlsplit(self.path).query)
                raw = params.get('id', [])
                if path == '/incidents' and not raw:
                    self._json(200, {'incidents':self.server.incidents.list()})
                elif (len(raw) != 1 or len(raw[0]) > 19 or not raw[0].isascii()
                      or not raw[0].isdigit() or not 1 <= int(raw[0]) <= 2**63-1):
                    self._error(400, 'Provide one positive incident id.')
                else:
                    ident = int(raw[0])
                    data = (self.server.incidents.get(ident) if path == '/incidents'
                            else self.server.incidents.recommendations(ident))
                    self._json(200, {'result':data})
            else:
                self._handle_chat(None, incident_path=path)
        elif path == "/health":
            self._json(200, {"status": "healthy", "service": "AI Model Gateway & Reliability Platform"})
        elif path == "/providers":
            self._json(200, {"providers": list(PROVIDERS), "simulated": True})
        elif path == "/metrics/prometheus":
            registry = getattr(self.server, 'metrics_keys', None)
            if registry is None:
                raise AuthenticationError()
            registry.authenticate(self.headers, admin=True)
            if getattr(self.server, 'incidents', None) is not None:
                self.server.incidents.evaluate()
            data = self._router().observability.render(self._router())
            if getattr(self.server, "jobs", None) is not None:
                data += self.server.jobs.prometheus() + self.server.workers.prometheus()
            self._response_status = 200
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
        elif path == "/metrics":
            self._authenticate(admin=True)
            self._json(200, self._router().metrics())
        elif path == "/usage":
            customer = self._authenticate()
            self._json(200, self.server.usage_store.usage(customer))
        elif path == "/jobs":
            self._submit_job(self._authenticate())
        else:
            customer = self._authenticate()
            self._handle_chat(customer)

    def _submit_job(self, customer):
        queue = getattr(self.server, "jobs", None)
        if queue is None:
            self._error(503, "Inference queue unavailable.")
            return
        keys = self.headers.get_all("Idempotency-Key", [])
        if len(keys) > 1:
            raise ValidationError("Provide at most one Idempotency-Key.")
        valid, payload = self._read_json()
        if valid:
            job, created = queue.submit(customer, payload, keys[0] if keys else None)
            self._json(202 if created else 200, job, {"Location": "/jobs/" + job["id"]})

    def _handle_job(self, customer, match):
        job_id, result_path = match.groups()
        allowed = ("GET",) if result_path else ("GET", "DELETE")
        if self.command not in allowed:
            self._error(405, "Method not allowed.", {"Allow": ", ".join(allowed)})
            return
        queue = getattr(self.server, "jobs", None)
        if queue is None:
            self._error(503, "Inference queue unavailable.")
            return
        if self.command == "DELETE":
            job = queue.cancel(customer.customer_id, job_id)
            result = None
        else:
            job, result = queue.get(customer.customer_id, job_id)
        if job is None:
            self._error(404, "Job not found.")
        elif not result_path:
            self._json(200, job)
        elif job["status"] == "succeeded":
            self._json(200, {"id": job_id, "result": result})
        elif job["status"] in ("queued", "running"):
            self._json(202, job, {"Retry-After": "1"})
        else:
            self._json(409, job)

    def _read_json(self):
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(400, "Transfer-Encoding is not supported; use Content-Length.")
            return False, None
        if self.headers.get_content_type() != "application/json":
            self._error(415, "Content-Type must be application/json.")
            return False, None
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._error(411, "Content-Length is required.")
            return False, None
        if len(lengths) != 1 or len(lengths[0]) > 20 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._error(400, "Content-Length must be a non-negative integer.")
            return False, None
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            self._error(413, f"Request body must be at most {MAX_BODY_BYTES} bytes.")
            return False, None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._error(400, "Incomplete request body.")
            return False, None
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=self._reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._error(400, "Request body must be valid UTF-8 JSON.")
            return False, None
        return True, payload

    def _handle_chat(self, customer, incident_path=None):
        valid, payload = self._read_json()
        if not valid:
            return
        if incident_path is not None:
            if not isinstance(payload, dict):
                self._error(400, 'Request body must be an object.')
                return
            engine = self.server.incidents
            if incident_path == '/incidents/evaluate':
                if payload:
                    self._error(400, 'Evaluation takes an empty object.')
                    return
                result = engine.evaluate()
            else:
                ident = payload.get('incident_id')
                if type(ident) is not int or not 1 <= ident <= 2**63-1:
                    self._error(400, 'incident_id must be a positive integer.')
                    return
                if incident_path == '/incidents/approve':
                    if set(payload) != {'incident_id','action'} or not isinstance(payload['action'],str):
                        self._error(400, 'Provide incident_id and an allowlisted action.')
                        return
                    result = {'approval_id':engine.approve(ident,payload['action'])}
                elif incident_path == '/incidents/execute':
                    approval = payload.get('approval_id')
                    if set(payload) != {'incident_id','approval_id'} or type(approval) is not int or not 1 <= approval <= 2**63-1:
                        self._error(400, 'Provide incident_id and positive approval_id.')
                        return
                    result = engine.execute(ident,approval)
                else:
                    if set(payload) != {'incident_id'}:
                        self._error(400, 'Provide only incident_id.')
                        return
                    result = engine.verify(ident)
            self._json(200, {'result':result})
            return
        try:
            result = customer_chat(payload, self._router(), self.server.usage_store, customer)
        except ValidationError as exc:
            self._error(400, str(exc))
            return
        except AllProvidersFailed as exc:
            self._json(503, {"status": "error", "error": str(exc), "providers": exc.failures})
            return
        self._json(200, result)

    @staticmethod
    def _reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch
    do_PATCH = _dispatch
    do_HEAD = _dispatch
    do_OPTIONS = _dispatch


class GatewayServer(ThreadingHTTPServer):
    request_queue_size = 128

    def server_close(self):
        if getattr(self, "workers", None) is not None:
            self.workers.close()
        super().server_close()


def create_server(host="127.0.0.1", port=8000, router=None, keys=None, usage_store=None,
                  metrics_key=None, mock_remediation=False, queue_config=None,
                  worker_count=2, worker_crash_file=None):
    keys = keys if keys is not None else KeyRegistry.from_environment()
    usage_store = usage_store if usage_store is not None else UsageStore("usage.sqlite3")
    metrics_keys = KeyRegistry(admin_key=metrics_key or environment_key("GATEWAY_METRICS_API_KEY"))
    if metrics_keys.admin is not None and (metrics_keys.admin == keys.admin or any(
            digest == metrics_keys.admin for digest, _ in keys.customers)):
        raise ValueError("Metrics credential must differ from customer and admin credentials")
    server = GatewayServer((host, port), GatewayHandler)
    server.router = router or Router()
    server.metrics_keys = metrics_keys
    server.keys = keys
    server.usage_store = usage_store
    try:
        server.incidents = IncidentEngine(usage_store.path, server.router, usage_store,
                                          mock_remediation=mock_remediation)
        server.router.observability.incidents = server.incidents
        server.jobs = JobQueue(usage_store, queue_config)
        server.workers = WorkerPool(server.jobs, server.router, worker_count, worker_crash_file)
    except Exception:
        server.server_close()
        raise
    return server


def main():
    parser = argparse.ArgumentParser(description="Run the simulated AI gateway locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", help="JSON provider policies (loaded at startup)")
    parser.add_argument("--simulation-file", help="Live JSON provider outcome sequences")
    parser.add_argument("--usage-db", default="usage.sqlite3", help="Persistent SQLite accounting file")
    parser.add_argument("--enable-mock-remediation", action="store_true",
                        help="Allow administrator-approved process-local mock success override")
    parser.add_argument("--workers", type=int, default=2, help="Inference workers (0 for API-only)")
    parser.add_argument("--queue-capacity", type=int, default=1000)
    parser.add_argument("--tenant-queue-capacity", type=int, default=100)
    parser.add_argument("--job-max-attempts", type=int, default=3)
    parser.add_argument("--job-lease-seconds", type=float, default=15)
    parser.add_argument("--job-aging-seconds", type=float, default=60)
    parser.add_argument("--job-retry-base", type=float, default=1)
    parser.add_argument("--job-retry-cap", type=float, default=60)
    parser.add_argument("--worker-crash-file", help="Opt-in: consume this marker and abandon one claimed job")
    args = parser.parse_args()
    policies = {}
    if args.config:
        try:
            with open(args.config, encoding="utf-8") as source:
                policies = {name: Policy(**values) for name, values in json.load(source).items()}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            parser.error(f"Invalid policy config: {exc}")
    try:
        router = Router(policies, Simulator(args.simulation_file))
    except ValueError as exc:
        parser.error(str(exc))
    try:
        keys = KeyRegistry.from_environment()
        usage_store = UsageStore(args.usage_db)
        queue_config = QueueConfig(args.queue_capacity, args.tenant_queue_capacity,
                                   args.job_max_attempts, args.job_lease_seconds,
                                   args.job_aging_seconds, args.job_retry_base, args.job_retry_cap)
        if not 0 <= args.workers <= 64:
            raise ValueError("Worker count must be between 0 and 64.")
    except (ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with create_server(args.host, args.port, router, keys, usage_store,
                       mock_remediation=args.enable_mock_remediation, queue_config=queue_config,
                       worker_count=args.workers, worker_crash_file=args.worker_crash_file) as server:
        def stop(_signum, _frame):
            server.workers.stopping.set()
            Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        print(f"AI gateway listening on http://{args.host}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
