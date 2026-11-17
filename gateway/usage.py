"""SQLite admission reservations make quota checks atomic across threads/processes."""

from contextlib import contextmanager
from datetime import datetime, timezone
import math
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
            return db.execute('INSERT INTO requests(customer,month,admitted,reserved) VALUES (?,?,?,?)',
                              (customer.customer_id, month, now, reservation)).lastrowid

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
