"""SQLite admission reservations make quota checks atomic across threads/processes."""

from contextlib import contextmanager
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sqlite3
from time import time

from gateway.security import TIERS


def estimate_tokens(text):
    """Mock tokenizer: one token per four Unicode characters, rounded up."""
    return (len(text) + 3) // 4


class LimitExceeded(Exception):
    def __init__(self, code, message, retry_after=None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(message)


class UsageStore:
    def __init__(self, path, clock=time):
        self.path, self.clock = str(path), clock
        self.owner = self.process_identity()
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY,
                    customer TEXT NOT NULL,
                    month TEXT NOT NULL,
                    admitted REAL NOT NULL,
                    reserved INTEGER NOT NULL,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS requests_customer_time ON requests(customer, admitted);
                CREATE INDEX IF NOT EXISTS requests_customer_month ON requests(customer, month);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in db.execute('PRAGMA table_info(requests)')}
            for name, kind in [('owner_pid','INTEGER'),('owner_start','TEXT'),('owner_scope','TEXT')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE requests ADD COLUMN {name} {kind}')

    @staticmethod
    def process_identity(pid=None):
        # Conservative same-host, same-boot, same-PID-namespace proof. Unknown
        # environments and legacy rows are never eligible for reclamation.
        pid = os.getpid() if pid is None else pid
        try:
            start = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
            scope = (Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':'
                     + os.readlink('/proc/self/ns/pid') + ':' + os.uname().nodename)
            return pid, start, scope
        except (OSError, IndexError):
            return pid, None, None

    def reconcile_stale(self, minimum_age=300):
        if not isinstance(minimum_age, (int,float)) or not math.isfinite(minimum_age) or minimum_age < 300:
            raise ValueError('Minimum stale age is 300 seconds')
        released = skipped = 0
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id,owner_pid,owner_start,owner_scope FROM requests WHERE status='pending' AND admitted<?",
                              (self.clock()-minimum_age,)).fetchall()
            for ident,pid,start,scope in rows:
                if not pid or not start or not scope or scope != self.owner[2]:
                    skipped += 1
                    continue
                current = self.process_identity(pid)
                # Missing/unreadable process metadata alone is not evidence of exit.
                dead = (current[1] is not None and current[1] != start)
                if current[1] is None:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        dead = True
                    except (PermissionError, OSError):
                        dead = False
                if not dead:
                    skipped += 1
                    continue
                db.execute("UPDATE requests SET status='failed',reserved=0 WHERE id=? AND status='pending'",(ident,))
                released += 1
        return {'released':released,'skipped':skipped}

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def month(self, now):
        return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m')

    def admit(self, customer, reservation):
        now = self.clock()
        month = self.month(now)
        tier = TIERS[customer.tier]
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            count, oldest = db.execute(
                'SELECT COUNT(*), MIN(admitted) FROM requests WHERE customer=? AND admitted>?',
                (customer.customer_id, now - 60)).fetchone()
            if count >= tier.requests_per_minute:
                raise LimitExceeded('rate_limit_exceeded', 'Customer request rate limit exceeded.',
                                    max(1, math.ceil(oldest + 60 - now)))
            used = db.execute('''SELECT COALESCE(SUM(CASE WHEN status='pending'
                THEN reserved ELSE tokens END),0) FROM requests WHERE customer=? AND month=?''',
                (customer.customer_id, month)).fetchone()[0]
            if used + reservation > tier.monthly_tokens:
                raise LimitExceeded('quota_exhausted',
                                    'Monthly token quota cannot cover this request. Try a shorter prompt or wait for the next UTC month.')
            return db.execute('INSERT INTO requests(customer,month,admitted,reserved,owner_pid,owner_start,owner_scope) VALUES (?,?,?,?,?,?,?)',
                              (customer.customer_id, month, now, reservation, *self.process_identity())).lastrowid

    def finish(self, request_id, tokens=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT reserved FROM requests WHERE id=? AND status='pending'", (request_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown or already settled reservation')
            if tokens is not None and not 0 <= tokens <= row[0]:
                raise ValueError('Usage exceeds reservation')
            db.execute('UPDATE requests SET tokens=?,status=? WHERE id=?',
                       (tokens or 0, 'success' if tokens is not None else 'failed', request_id))

    def usage(self, customer):
        now = self.clock()
        month = self.month(now)
        with self.connection() as db:
            row = db.execute('''SELECT COUNT(*), COALESCE(SUM(status='success'),0),
                COALESCE(SUM(status='failed'),0), COALESCE(SUM(status='pending'),0),
                COALESCE(SUM(tokens),0), COALESCE(SUM(CASE WHEN status='pending' THEN reserved ELSE 0 END),0)
                FROM requests WHERE customer=? AND month=?''', (customer.customer_id, month)).fetchone()
        requests, successes, failures, pending, tokens, reserved = row
        tier = TIERS[customer.tier]
        return dict(customer_id=customer.customer_id, tier=customer.tier, month=month,
                    requests=requests, successes=successes, failures=failures, pending=pending,
                    estimated_tokens=tokens, reserved_tokens=reserved,
                    monthly_token_quota=tier.monthly_tokens,
                    remaining_tokens=max(0, tier.monthly_tokens-tokens-reserved),
                    requests_per_minute=tier.requests_per_minute,
                    estimated_cost_usd=f'{tokens // 1_000_000}.{tokens % 1_000_000:06d}',
                    cost_is_estimate=True, estimated_usd_per_million_tokens=1)
