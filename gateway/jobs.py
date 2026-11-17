"""Durable inference queue. Every state transition is serialized by SQLite."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import re
import sqlite3
from threading import Lock
from uuid import uuid4

from gateway.service import ValidationError, token_reservation, validate_chat
from gateway.usage import LimitExceeded

STATES = ("queued", "running", "succeeded", "cancelled", "dead_letter")
EVENTS = ("submitted", "retried", "lease_expired", "succeeded", "cancelled", "dead_letter")


class JobConflict(Exception):
    pass


@dataclass(frozen=True)
class QueueConfig:
    capacity: int = 1000
    tenant_capacity: int = 100
    max_attempts: int = 3
    lease_seconds: float = 15
    aging_seconds: float = 60
    retry_base: float = 1
    retry_cap: float = 60

    def __post_init__(self):
        for value in (self.capacity, self.tenant_capacity):
            if type(value) is not int or not 1 <= value <= 1_000_000:
                raise ValueError("Queue capacities must be integers between 1 and 1000000.")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 100:
            raise ValueError("Maximum job attempts must be an integer between 1 and 100.")
        for value in (self.lease_seconds, self.aging_seconds, self.retry_base, self.retry_cap):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Queue durations must be finite and positive.")
        if self.retry_cap < self.retry_base or self.lease_seconds < 0.1:
            raise ValueError("Retry cap must cover the base delay and lease must be at least 0.1 seconds.")


class JobQueue:
    def __init__(self, usage, config=None):
        self.usage = usage
        self.config = config or QueueConfig()
        self._write_lock = Lock()
        with usage.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inference_jobs (
                    id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL,
                    idem_key TEXT,
                    prompt TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    usage_id INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    available_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    lease_token TEXT,
                    lease_until REAL,
                    result TEXT,
                    error TEXT,
                    UNIQUE (tenant, idem_key)
                );
                CREATE INDEX IF NOT EXISTS inference_ready
                    ON inference_jobs(status, available_at);
                CREATE INDEX IF NOT EXISTS inference_active_tenant
                    ON inference_jobs(tenant, status);
                CREATE INDEX IF NOT EXISTS inference_lease
                    ON inference_jobs(lease_until) WHERE status='running';
                CREATE TABLE IF NOT EXISTS inference_events (
                    event TEXT PRIMARY KEY,
                    count INTEGER NOT NULL
                );
            """)

    @contextmanager
    def transaction(self):
        # Let one local writer at a time contend for SQLite's cross-process lock.
        with self._write_lock, self.usage.connection() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db, self.usage.clock()

    @staticmethod
    def _event(db, event):
        db.execute("""INSERT INTO inference_events VALUES (?, 1)
                      ON CONFLICT(event) DO UPDATE SET count=count+1""", (event,))

    @staticmethod
    def _public(row):
        fields = ("id", "status", "provider", "priority", "attempts", "max_attempts",
                  "created_at", "updated_at", "available_at", "error")
        return {field: row[field] for field in fields}

    def submit(self, customer, payload, key=None):
        prompt, provider = validate_chat(payload)
        try:
            prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise ValidationError("Prompt must contain valid Unicode.") from None
        priority = payload.get("priority", 5)
        if set(payload) - {"prompt", "provider", "priority"}:
            raise ValidationError("Use only prompt, provider, and priority.")
        if type(priority) is not int or not 0 <= priority <= 9:
            raise ValidationError("'priority' must be an integer from 0 to 9.")
        if key is not None and not re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", key):
            raise ValidationError("Invalid Idempotency-Key.")
        tenant = customer.customer_id
        with self.transaction() as (db, now):
            if key is not None:
                existing = db.execute("SELECT * FROM inference_jobs WHERE tenant=? AND idem_key=?",
                                      (tenant, key)).fetchone()
                if existing:
                    if (prompt, provider, priority) != tuple(existing[k] for k in ("prompt", "provider", "priority")):
                        raise JobConflict("Idempotency-Key already used with a different request.")
                    return self._public(existing), False
            active = "FROM inference_jobs WHERE status IN ('queued', 'running')"
            if db.execute("SELECT COUNT(*) " + active + " AND tenant=?", (tenant,)).fetchone()[0] >= self.config.tenant_capacity:
                raise LimitExceeded("tenant_queue_full", "Tenant queue is full.", 1)
            if db.execute("SELECT COUNT(*) " + active).fetchone()[0] >= self.config.capacity:
                raise LimitExceeded("queue_full", "Inference queue is full.", 1)
            usage_id = self.usage.reserve(db, customer, token_reservation(prompt), now)
            job_id = uuid4().hex
            db.execute("""INSERT INTO inference_jobs
                (id, tenant, idem_key, prompt, provider, priority, usage_id, status, created_at,
                 updated_at, available_at, max_attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                       (job_id, tenant, key, prompt, provider, priority, usage_id, now, now, now,
                        self.config.max_attempts))
            self._event(db, "submitted")
            return self._public(db.execute("SELECT * FROM inference_jobs WHERE id=?", (job_id,)).fetchone()), True

    def get(self, tenant, job_id):
        with self.usage.connection() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM inference_jobs WHERE tenant=? AND id=?", (tenant, job_id)).fetchone()
            if row is None:
                return None, None
            return self._public(row), json.loads(row["result"]) if row["result"] else None

    def cancel(self, tenant, job_id):
        with self.transaction() as (db, now):
            row = db.execute("SELECT * FROM inference_jobs WHERE tenant=? AND id=?", (tenant, job_id)).fetchone()
            if row is None:
                return None
            if row["status"] in ("queued", "running"):
                self._transition(db, row, now, "cancelled")
                row = db.execute("SELECT * FROM inference_jobs WHERE id=?", (job_id,)).fetchone()
            elif row["status"] != "cancelled":
                raise JobConflict("Job already finished.")
            return self._public(row)

    def _transition(self, db, row, now, status, *, available=None, result=None, error=None, tokens=None):
        db.execute("""UPDATE inference_jobs SET status=?, updated_at=?, available_at=?, result=?,
                      error=?, lease_token=NULL, lease_until=NULL WHERE id=?""",
                   (status, now, row["available_at"] if available is None else available,
                    result, error, row["id"]))
        if status == "queued":
            self._event(db, "retried")
        else:
            self.usage.settle(db, row["usage_id"], tokens)
            self._event(db, status)

    def _fail(self, db, row, now, error):
        if row["attempts"] >= row["max_attempts"]:
            self._transition(db, row, now, "dead_letter", error=error)
        else:
            delay = min(self.config.retry_cap, self.config.retry_base * 2 ** (row["attempts"] - 1))
            self._transition(db, row, now, "queued", available=now + delay, error=error)

    def claim(self):
        with self.transaction() as (db, now):
            expired = db.execute("""SELECT * FROM inference_jobs WHERE status='running' AND lease_until<=?
                                    ORDER BY lease_until LIMIT 100""", (now,)).fetchall()
            for row in expired:
                self._fail(db, row, now, "lease_expired")
                self._event(db, "lease_expired")
            row = db.execute("""SELECT * FROM inference_jobs WHERE status='queued' AND available_at<=?
                               ORDER BY CASE WHEN available_at<=? THEN 10 ELSE priority END DESC,
                                        available_at, created_at, rowid LIMIT 1""",
                             (now, now - self.config.aging_seconds)).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            db.execute("""UPDATE inference_jobs SET status='running', attempts=attempts+1, updated_at=?,
                          lease_token=?, lease_until=? WHERE id=?""",
                       (now, token, now + self.config.lease_seconds, row["id"]))
            return dict(row, lease_token=token, attempts=row["attempts"] + 1)

    def renew(self, job_id, token):
        with self.transaction() as (db, now):
            return db.execute("""UPDATE inference_jobs SET lease_until=?
                 WHERE id=? AND status='running' AND lease_token=? AND lease_until>?""",
                              (now + self.config.lease_seconds, job_id, token, now)).rowcount == 1

    def finish(self, job_id, token, *, result=None, tokens=None, error=None):
        if error is None and (result is None or tokens is None):
            raise ValueError("Successful jobs require a result and token count.")
        with self.transaction() as (db, now):
            row = db.execute("""SELECT * FROM inference_jobs WHERE id=? AND status='running'
                                 AND lease_token=? AND lease_until>?""", (job_id, token, now)).fetchone()
            if row is None:
                return False
            if error is not None:
                self._fail(db, row, now, error)
            else:
                self._transition(db, row, now, "succeeded", result=json.dumps(result), tokens=tokens)
            return True

    def prometheus(self):
        with self.usage.connection() as db:
            db.execute("BEGIN")
            counts = dict(db.execute("SELECT status, COUNT(*) FROM inference_jobs GROUP BY status"))
            events = dict(db.execute("SELECT event, count FROM inference_events"))
            now = self.usage.clock()
            oldest = db.execute("""SELECT MIN(available_at) FROM inference_jobs
                                   WHERE status='queued' AND available_at<=?""", (now,)).fetchone()[0]
        lines = ["# HELP gateway_jobs Persisted inference jobs by state.",
                 "# TYPE gateway_jobs gauge"]
        lines.extend(f'gateway_jobs{{state="{state}"}} {counts.get(state, 0)}' for state in STATES)
        lines.extend(("# HELP gateway_job_events_total Persisted inference queue transitions by event.",
                      "# TYPE gateway_job_events_total counter"))
        lines.extend(f'gateway_job_events_total{{event="{event}"}} {events.get(event, 0)}' for event in EVENTS)
        age = 0 if oldest is None else now - oldest
        lines.extend(("# HELP gateway_queue_capacity Configured global capacity for active inference jobs.",
                      "# TYPE gateway_queue_capacity gauge",
                      f"gateway_queue_capacity {self.config.capacity}",
                      "# HELP gateway_queue_oldest_ready_seconds Age of the oldest eligible queued attempt.",
                      "# TYPE gateway_queue_oldest_ready_seconds gauge",
                      f"gateway_queue_oldest_ready_seconds {age}"))
        return ("\n".join(lines) + "\n").encode()
