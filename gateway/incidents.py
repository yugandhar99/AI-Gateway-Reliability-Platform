"""Deterministic, local incident management. No shell, network or LLM execution."""
import json
import sqlite3
from contextlib import contextmanager
from threading import RLock
from time import time


class IncidentError(ValueError):
    pass


class IncidentEngine:
    """One active routing incident correlates chat and provider symptoms.

    Evaluation runs after chat requests and on explicit administrator requests.
    Approvals bind an action to an incident revision and expire after five minutes.
    Execution is claimed durably before any side effect; interrupted execution is
    never replayed automatically. Recovery is a separate evidence-based step.
    """
    def __init__(self, path, router, usage, clock=time, mock_remediation=False):
        self.path, self.router, self.usage = str(path), router, usage
        self.clock, self.mock_remediation = clock, mock_remediation
        self.lock = RLock()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL,
                    severity TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
                    updated REAL NOT NULL, evidence TEXT NOT NULL, summary TEXT NOT NULL,
                    recovery_after REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_incident
                    ON incidents(fingerprint) WHERE status != 'resolved';
                CREATE TABLE IF NOT EXISTS incident_audit (
                    id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL,
                    at REAL NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incident_approvals (
                    id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL, action TEXT NOT NULL,
                    expires REAL NOT NULL, status TEXT NOT NULL DEFAULT 'approved'
                );
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, db, incident, event, detail, actor='system'):
        db.execute('INSERT INTO incident_audit(incident_id,at,actor,event,detail) VALUES (?,?,?,?,?)',
                   (incident, self.clock(), actor, event, json.dumps(detail, sort_keys=True)))

    def evidence(self):
        metrics = self.router.observability.window()
        providers = self.router.metrics()['providers']
        unavailable = sorted(p for p, h in providers.items() if not h['healthy'])
        opened = sorted(p for p, h in providers.items() if h['state'] == 'OPEN')
        symptoms = []
        if metrics['requests'] >= 5 and metrics['error_rate'] > .1:
            symptoms.append('elevated_error_rate')
        if metrics['requests'] >= 5 and metrics['p95_seconds'] > .5:
            symptoms.append('latency_spike')
        if unavailable:
            symptoms.append('provider_outage')
        if opened:
            symptoms.append('open_circuit')
        return dict(metrics, unavailable=unavailable, open_circuits=opened,
                    provider_errors={p:h['last_error'] for p,h in providers.items() if h['last_error']},
                    symptoms=symptoms)

    @staticmethod
    def analysis(e):
        if len(e['unavailable']) == 3:
            severity, cause = 'P1', 'All simulated providers unavailable'
        elif e['unavailable']:
            severity, cause = 'P2', 'Simulated provider failures with degraded redundancy'
        elif 'elevated_error_rate' in e['symptoms']:
            severity = 'P1' if e['error_rate'] >= .5 else 'P2'
            cause = 'Chat server errors without provider failure evidence; inspect application/accounting'
        else:
            severity, cause = 'P3', 'Chat latency exceeds 500ms; provider fault not established'
        errors = set(e['provider_errors'].values())
        if 'simulation_config_error' in errors:
            cause = 'Local simulation configuration is invalid or unreadable'
        elif 'timeout' in errors:
            cause = 'Simulated provider timeouts; circuit isolation may amplify unavailability'
        elif 'upstream_503' in errors:
            cause = 'Simulated upstream 503 failures; circuit isolation may amplify unavailability'
        return severity, cause + '; evidence: ' + ', '.join(e['symptoms'])

    @staticmethod
    def decode(row):
        result = dict(row)
        result['evidence'] = json.loads(result['evidence'])
        result['root_cause'] = result['summary'].split('; evidence:')[0]
        return result

    def evaluate(self):
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            e = self.evidence()
            row = db.execute("SELECT * FROM incidents WHERE fingerprint='routing' AND status!='resolved'").fetchone()
            if not e['symptoms'] and row is None:
                return None
            if e['symptoms']:
                severity, summary = self.analysis(e)
            else:
                severity = row['severity']
                summary = row['summary'].split('; evidence:')[0] + '; evidence: symptoms cleared; explicit recovery verification required'

            encoded = json.dumps(e, sort_keys=True)
            if row is None:
                ident = db.execute('''INSERT INTO incidents(fingerprint,severity,created,updated,evidence,summary)
                    VALUES ('routing',?,?,?,?,?)''', (severity,self.clock(),self.clock(),encoded,summary)).lastrowid
                self.audit(db, ident, 'detected', e)
            else:
                ident = row['id']
                old = json.loads(row['evidence'])
                # Counts vary on every request; invalidate approval only on a material change.
                changed = (severity != row['severity'] or any(old[k] != e[k] for k in
                           ('symptoms','unavailable','open_circuits','provider_errors')))
                db.execute('''UPDATE incidents SET severity=?,updated=?,evidence=?,summary=?,
                    revision=revision+? WHERE id=?''',
                    (severity,self.clock(),encoded,summary,int(changed),ident))
                if changed:
                    self.audit(db, ident, 'evidence_changed', e)
            return self.decode(db.execute('SELECT * FROM incidents WHERE id=?',(ident,)).fetchone())

    def list(self):
        with self.db() as db:
            return [self.decode(r) for r in db.execute('SELECT * FROM incidents ORDER BY id DESC LIMIT 100')]

    def get(self, ident):
        with self.db() as db:
            row = db.execute('SELECT * FROM incidents WHERE id=?',(ident,)).fetchone()
            if row is None:
                raise IncidentError('Unknown incident')
            result = self.decode(row)
            result['audit'] = [dict(r) for r in db.execute(
                'SELECT * FROM incident_audit WHERE incident_id=? ORDER BY id',(ident,))]
            result['approvals'] = [dict(r) for r in db.execute(
                'SELECT * FROM incident_approvals WHERE incident_id=? ORDER BY id',(ident,))]
            return result

    def recommendations(self, ident):
        row = self.get(ident)
        if row['status'] == 'resolved':
            return []
        result = [{'action':'probe_providers', 'description':'Send fixed mock health probes through normal circuit cooldowns; no forced resets.'}]
        if self.mock_remediation:
            result.append({'action':'restore_mock_providers', 'description':'Override mock outcomes in this process to success, then probe. Scenario files are preserved.'})
        result.append({'action':'reconcile_reservations', 'description':'Release only stale reservations owned by provably exited local processes; living/unknown owners are skipped.'})
        return result

    def approve(self, ident, action):
        if action not in {r['action'] for r in self.recommendations(ident)}:
            raise IncidentError('Action is not recommended or allowlisted')
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM incidents WHERE id=?',(ident,)).fetchone()
            if row is None or row['status'] not in ('open','verifying'):
                raise IncidentError('Incident must be open or verifying')
            approval = db.execute('''INSERT INTO incident_approvals(incident_id,revision,action,expires)
                VALUES (?,?,?,?)''',(ident,row['revision'],action,self.clock()+300)).lastrowid
            self.audit(db,ident,'approved',{'approval_id':approval,'action':action},'administrator')
            return approval

    def execute(self, ident, approval):
        with self.lock:
            self.evaluate()
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM incidents WHERE id=?',(ident,)).fetchone()
                a = db.execute('SELECT * FROM incident_approvals WHERE id=? AND incident_id=?',(approval,ident)).fetchone()
                if (row is None or a is None or a['status'] != 'approved' or a['expires'] <= self.clock()
                        or row['status'] not in ('open','verifying') or a['revision'] != row['revision']):
                    raise IncidentError('Approval missing, expired, consumed or stale')
                action = a['action']
                if action not in {r['action'] for r in self.recommendations(ident)}:
                    raise IncidentError('Action disabled')
                db.execute("UPDATE incident_approvals SET status='executing' WHERE id=?",(approval,))
                db.execute("UPDATE incidents SET status='executing' WHERE id=?",(ident,))
                self.audit(db,ident,'execution_started',{'approval_id':approval,'action':action},'administrator')
            try:
                if action == 'restore_mock_providers':
                    self.router.simulator.restore_mock()
                if action in ('restore_mock_providers','probe_providers'):
                    for provider in self.router.health:
                        try:
                            self.router.route('Stage 7 fixed health probe', provider)
                        except Exception:
                            # No exception strings (which may contain private input) enter audit.
                            pass
                    result = {'providers':self.router.metrics()['providers']}
                else:
                    result = self.usage.reconcile_stale()
            except Exception:
                with self.db() as db:
                    db.execute("UPDATE incident_approvals SET status='failed' WHERE id=?",(approval,))
                    db.execute("UPDATE incidents SET status='open' WHERE id=?",(ident,))
                    self.audit(db,ident,'execution_failed',{'action':action})
                raise IncidentError('Remediation failed; inspect local application state') from None
            with self.db() as db:
                db.execute("UPDATE incident_approvals SET status='executed' WHERE id=?",(approval,))
                db.execute("UPDATE incidents SET status='verifying',recovery_after=? WHERE id=?",(self.clock(),ident))
                self.audit(db,ident,'executed',{'action':action,'result':result})
            return result

    def verify(self, ident):
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM incidents WHERE id=?',(ident,)).fetchone()
            if row is None or row['status'] != 'verifying':
                raise IncidentError('Incident must have an executed remediation')
            e = self.evidence()
            # Fresh post-action chat evidence, not a process liveness endpoint alone.
            fresh = self.router.observability.window(since=row['recovery_after'])
            recovered = (not e['symptoms'] and fresh['requests'] >= 5
                         and fresh['error_rate'] == 0 and fresh['p95_seconds'] <= .5
                         and not e['unavailable'])
            self.audit(db,ident,'recovery_verified' if recovered else 'recovery_pending',{'evidence':e,'fresh':fresh})
            if recovered:
                db.execute("UPDATE incidents SET status='resolved',updated=?,evidence=?,summary=? WHERE id=?",
                           (self.clock(),json.dumps(e,sort_keys=True),
                            row['summary'].split('; evidence:')[0] + '; evidence: recovery verified',ident))
            return {'recovered':recovered,'evidence':e,'fresh':fresh}

    def metrics(self):
        with self.db() as db:
            counts = {s:0 for s in ('P1','P2','P3')}
            for s,n in db.execute("SELECT severity,COUNT(*) FROM incidents WHERE status!='resolved' GROUP BY severity"):
                counts[s] = n
            resolved = db.execute("SELECT COUNT(*) FROM incidents WHERE status='resolved'").fetchone()[0]
            approvals = db.execute('SELECT COUNT(*) FROM incident_approvals').fetchone()[0]
        return counts, resolved, approvals
