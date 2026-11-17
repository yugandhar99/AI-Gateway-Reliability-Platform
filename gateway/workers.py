"""Concurrent, supervised workers for the simulated inference queue."""

import logging
from pathlib import Path
import sqlite3
from threading import Event, Thread

from gateway.reliability import AllProvidersFailed
from gateway.service import chat
from gateway.usage import estimate_tokens

logger = logging.getLogger("gateway.workers")


class WorkerPool:
    def __init__(self, queue, router, count=2, crash_file=None):
        if type(count) is not int or not 0 <= count <= 64:
            raise ValueError("Worker count must be between 0 and 64.")
        self.queue = queue
        self.router = router
        self.count = count
        self.crash_file = Path(crash_file) if crash_file else None
        self.stopping = Event()
        self.workers = [None] * count
        self.supervisor = Thread(target=self._supervise, name="inference-supervisor", daemon=True)
        if count:
            self.supervisor.start()

    def _supervise(self):
        while not self.stopping.is_set():
            for slot, worker in enumerate(self.workers):
                if worker is None or not worker.is_alive():
                    worker = Thread(target=self._work, name=f"inference-{slot}", daemon=True)
                    self.workers[slot] = worker
                    worker.start()
            self.stopping.wait(0.1)

    def _heartbeat(self, job, done):
        interval = self.queue.config.lease_seconds / 3
        while not done.wait(interval):
            try:
                if not self.queue.renew(job["id"], job["lease_token"]):
                    return
            except sqlite3.Error:
                logger.warning("Inference heartbeat could not reach SQLite")

    def _crash_requested(self):
        if self.crash_file is None:
            return False
        try:
            self.crash_file.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            logger.warning("Inference crash marker could not be consumed")
            return False
        return True

    def _work(self):
        while not self.stopping.is_set():
            try:
                job = self.queue.claim()
                if job is None:
                    self.stopping.wait(min(0.1, self.queue.config.lease_seconds / 3))
                    continue
                if self._crash_requested():
                    logger.warning("Simulated inference worker crash after claim")
                    return
                self._execute(job)
            except sqlite3.Error:
                logger.warning("Inference worker could not reach SQLite")
                self.stopping.wait(0.25)

    def _execute(self, job):
        done = Event()
        heartbeat = Thread(target=self._heartbeat, args=(job, done), daemon=True)
        heartbeat.start()
        try:
            try:
                result = chat({"prompt": job["prompt"], "provider": job["provider"]}, self.router)
                tokens = estimate_tokens(job["prompt"]) + estimate_tokens(result["response"])
            except AllProvidersFailed:
                self.queue.finish(job["id"], job["lease_token"], error="providers_unavailable")
            except Exception:
                logger.error("Unexpected inference worker error")
                self.queue.finish(job["id"], job["lease_token"], error="execution_error")
            else:
                if self.queue.finish(job["id"], job["lease_token"], result=result, tokens=tokens):
                    self.router.observability.add_tokens(tokens)
        finally:
            done.set()
            heartbeat.join()

    def close(self):
        self.stopping.set()
        if self.count:
            self.supervisor.join()
        for worker in self.workers:
            if worker is not None:
                worker.join()

    def prometheus(self):
        alive = sum(worker is not None and worker.is_alive() for worker in self.workers)
        return ("# HELP gateway_workers_alive Live inference worker threads in this process.\n"
                "# TYPE gateway_workers_alive gauge\n"
                f"gateway_workers_alive {alive}\n"
                "# HELP gateway_workers_configured Configured inference worker slots in this process.\n"
                "# TYPE gateway_workers_configured gauge\n"
                f"gateway_workers_configured {self.count}\n").encode()
