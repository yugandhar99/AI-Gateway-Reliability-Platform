"""Stage 7 uses actual SQLite and HTTP parsing, with deterministic metric clocks."""
import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from gateway.incidents import IncidentEngine, IncidentError
from gateway.observability import Metrics
from gateway.reliability import Router, Simulator
from gateway.security import Customer, KeyRegistry
from gateway.usage import UsageStore
from tests import test_http


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.
        self.clock = lambda:self.now
        self.path = Path(self.temp.name)
        self.scenario = self.path / 'scenario.json'
        self.scenario.write_text('{}')
        self.router = Router(simulator=Simulator(self.scenario, sleeper=lambda _:None),clock=self.clock)
        self.router.observability = Metrics(clock=self.clock)
        self.usage = UsageStore(self.path/'usage.db',clock=self.clock)
        self.engine = IncidentEngine(self.usage.path,self.router,self.usage,clock=self.clock,mock_remediation=True)
        self.router.observability.incidents = self.engine

    def observe(self, status=200, duration=.01, count=5):
        for _ in range(count):
            self.router.observability.observe('/chat','POST',status,duration)

    def outage(self):
        self.scenario.write_text(json.dumps({p:['503'] for p in self.router.health}))
        for _ in range(2):
            try:
                self.router.route('fixed mock request','OpenAI')
            except Exception:
                pass
        self.observe(503,.7)
        return self.engine.evaluate()['id']

    def test_detection_severity_and_deterministic_analysis(self):
        self.assertIsNone(self.engine.evaluate())
        self.observe(duration=.8)
        row = self.engine.evaluate()
        self.assertEqual(row['severity'],'P3')
        self.assertIn('latency',row['summary'])
        self.now += 61
        self.observe(503)
        self.assertEqual(self.engine.evaluate()['severity'],'P1')
        self.now += 61
        self.observe(200,count=8)
        self.observe(503,count=2)
        self.assertEqual(self.engine.evaluate()['severity'],'P2')

    def test_provider_and_circuit_correlation_and_deduplication(self):
        ident = self.outage()
        results = list(ThreadPoolExecutor(4).map(lambda _:self.engine.evaluate()['id'],range(12)))
        self.assertEqual(set(results),{ident})
        row = self.engine.get(ident)
        self.assertEqual(row['severity'],'P1')
        self.assertEqual(len(row['evidence']['symptoms']),4)
        self.assertEqual(len(row['audit']),1)
        self.assertEqual(len(self.engine.list()),1)

    def test_partial_provider_outage_is_p2(self):
        self.scenario.write_text('{"OpenAI":["503"]}')
        self.router.route('mock','OpenAI')
        row=self.engine.evaluate()
        self.assertEqual(row['severity'],'P2')
        self.assertIn('OpenAI',row['evidence']['unavailable'])

    def test_root_cause_uses_fixed_provider_error_evidence(self):
        self.scenario.write_text('invalid JSON')
        try:self.router.route('fixed','OpenAI')
        except Exception:pass
        row = self.engine.evaluate()
        self.assertIn('configuration',row['root_cause'])
        self.assertEqual(row['severity'],'P1')

    def test_approval_required_allowlist_and_disabled_override(self):
        ident=self.outage()
        with self.assertRaises(IncidentError):self.engine.execute(ident,1)
        for action in ('restart','rm -rf /','reset_circuit',''):
            with self.assertRaises(IncidentError):self.engine.approve(ident,action)
        self.engine.mock_remediation=False
        with self.assertRaises(IncidentError):self.engine.approve(ident,'restore_mock_providers')
        self.assertFalse(self.router.simulator.restored)

    def test_expired_and_changed_evidence_reject_approval(self):
        ident=self.outage()
        approval=self.engine.approve(ident,'probe_providers')
        self.now+=301
        with self.assertRaises(IncidentError):self.engine.execute(ident,approval)
        approval=self.engine.approve(ident,'probe_providers')
        self.router.health['OpenAI'].state='CLOSED'
        self.router.health['OpenAI'].consecutive_failures=0
        with self.assertRaises(IncidentError):self.engine.execute(ident,approval)

    def test_recovery_needs_fresh_traffic_and_consumes_approval(self):
        ident=self.outage()
        before=self.scenario.read_text()
        approval=self.engine.approve(ident,'restore_mock_providers')
        self.now+=6
        self.engine.execute(ident,approval)
        self.assertEqual(self.scenario.read_text(),before)
        self.assertFalse(self.engine.verify(ident)['recovered'])
        with self.assertRaises(IncidentError):self.engine.execute(ident,approval)
        self.now+=61
        self.observe()
        self.engine.evaluate()
        self.assertTrue(self.engine.verify(ident)['recovered'])
        row=self.engine.get(ident)
        self.assertEqual(row['status'],'resolved')
        self.assertIn('recovery_verified',[e['event'] for e in row['audit']])
        reloaded=IncidentEngine(self.usage.path,self.router,self.usage,clock=self.clock)
        self.assertEqual(reloaded.get(ident)['status'],'resolved')
        self.outage()  # override remains success, inject an HTTP error incident instead
        self.observe(503)
        self.assertNotEqual(self.engine.evaluate()['id'],ident)

    def test_failure_audited_and_not_replayed(self):
        ident=self.outage()
        approval=self.engine.approve(ident,'restore_mock_providers')
        with patch.object(self.router.simulator,'restore_mock',side_effect=RuntimeError('private')):
            with self.assertRaises(IncidentError):self.engine.execute(ident,approval)
        row=self.engine.get(ident)
        self.assertEqual(row['approvals'][0]['status'],'failed')
        self.assertNotIn('private',json.dumps(row))
        with self.assertRaises(IncidentError):self.engine.execute(ident,approval)
        self.assertEqual(row['status'],'open')

    def test_single_execution_across_engine_instances(self):
        ident=self.outage()
        approval=self.engine.approve(ident,'reconcile_reservations')
        other=IncidentEngine(self.usage.path,self.router,self.usage,clock=self.clock,mock_remediation=True)
        def run(engine):
            try:engine.execute(ident,approval);return True
            except IncidentError:return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(run,[self.engine,other])),1)

    def test_metrics_and_no_customer_evidence(self):
        ident=self.outage()
        self.engine.approve(ident,'probe_providers')
        text=self.router.observability.render(self.router).decode()
        self.assertIn('gateway_incidents_active{severity="P1"} 1',text)
        self.assertIn('gateway_incident_approvals_total 1',text)
        self.assertNotIn('prompt',json.dumps(self.engine.get(ident)))

    def test_stale_reconciliation_protects_active_legacy_and_other_namespace(self):
        customer=Customer('fixture','Enterprise')
        live=self.usage.admit(customer,10)
        legacy=self.usage.admit(customer,10)
        other=self.usage.admit(customer,10)
        with self.usage.connection() as db:
            db.execute('UPDATE requests SET owner_pid=NULL WHERE id=?',(legacy,))
            db.execute("UPDATE requests SET owner_scope='unknown' WHERE id=?",(other,))
        self.now+=600
        result=self.usage.reconcile_stale()
        self.assertEqual(result,{'released':0,'skipped':3})
        self.usage.finish(live,5)
        with self.assertRaises(ValueError):self.usage.reconcile_stale(1)

    def test_stale_exited_process_reservations_released_once(self):
        # Real child admits then exits; no fabricated PID or liveness assumption.
        code="from gateway.usage import UsageStore; from gateway.security import Customer; UsageStore(__import__('sys').argv[1], clock=lambda:1000).admit(Customer('child','Enterprise'),10)"
        subprocess.run([sys.executable,'-c',code,self.usage.path],check=True,capture_output=True)
        self.now+=600
        self.assertEqual(self.usage.reconcile_stale()['released'],1)
        self.assertEqual(self.usage.reconcile_stale()['released'],0)
        with self.usage.connection() as db:
            self.assertEqual(db.execute('SELECT status,reserved,tokens FROM requests').fetchone(),('failed',0,0))

    def test_approval_reconciliation_and_audit(self):
        ident=self.outage()
        a=self.engine.approve(ident,'reconcile_reservations')
        self.assertEqual(self.engine.execute(ident,a),{'released':0,'skipped':0})
        self.assertIn('executed',[r['event'] for r in self.engine.get(ident)['audit']])

    def test_active_request_survives_reconciliation(self):
        from threading import Event
        from gateway.service import customer_chat
        entered, release = Event(), Event()
        original = self.router.simulator
        def blocked(provider,prompt,timeout):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            return original(provider,prompt,timeout)
        self.router.simulator = blocked
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(customer_chat,{'prompt':'fixed','provider':'OpenAI'},
                                 self.router,self.usage,Customer('active','Enterprise'))
            try:
                self.assertTrue(entered.wait(3))
                self.now += 600
                self.assertEqual(self.usage.reconcile_stale(),{'released':0,'skipped':1})
            finally:
                release.set()
            self.assertEqual(future.result()['status'],'success')
        self.assertEqual(self.usage.usage(Customer('active','Enterprise'))['successes'],1)

    def test_cleared_symptoms_invalidate_previous_approval(self):
        self.observe(503)
        ident = self.engine.evaluate()['id']
        approval = self.engine.approve(ident,'probe_providers')
        self.now += 61
        self.observe()
        with self.assertRaises(IncidentError):self.engine.execute(ident,approval)

    def test_live_owner_with_unreadable_metadata_is_not_reclaimed(self):
        self.usage.admit(Customer('live','Enterprise'),10)
        self.now += 600
        with patch.object(self.usage,'process_identity',return_value=(os.getpid(),None,None)):
            with patch('gateway.usage.os.kill',side_effect=PermissionError):
                self.assertEqual(self.usage.reconcile_stale(),{'released':0,'skipped':1})

    def test_interrupted_execution_remains_non_replayable(self):
        ident = self.outage()
        approval = self.engine.approve(ident,'probe_providers')
        with self.engine.db() as db:
            db.execute("UPDATE incident_approvals SET status='executing' WHERE id=?",(approval,))
            db.execute("UPDATE incidents SET status='executing' WHERE id=?",(ident,))
        restarted = IncidentEngine(self.usage.path,self.router,self.usage,clock=self.clock)
        with self.assertRaises(IncidentError):restarted.execute(ident,approval)
        with self.assertRaises(IncidentError):restarted.approve(ident,'probe_providers')



class IncidentHTTPTests(unittest.TestCase):
    request = test_http.HTTPTests.request
    def setUp(self):
        self.temp=TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        router=Router()
        usage=UsageStore(Path(self.temp.name)/'usage.db')
        self.gateway=SimpleNamespace(router=router,usage_store=usage,
            keys=KeyRegistry({'test-starter':Customer('starter-demo','Starter')},'test-admin'))
        self.gateway.incidents=IncidentEngine(usage.path,router,usage)

    def admin(self,path,payload,token='test-admin'):
        return self.request('POST',path,json.dumps(payload),{'Content-Type':'application/json','Authorization':'Bearer '+token})

    def test_incident_endpoint_authorization_and_invalid_payloads(self):
        for path in ('/incidents','/incidents/recommendations?id=1'):
            for token in ('test-starter','test-scraper','bad'):
                self.assertEqual(self.request('GET',path,headers={'Authorization':'Bearer '+token})[0],401)
        for path in ('/incidents/evaluate','/incidents/approve','/incidents/execute','/incidents/verify'):
            self.assertEqual(self.admin(path,{},'test-starter')[0],401)
        self.assertEqual(self.admin('/incidents/evaluate',{})[0],200)
        self.assertEqual(self.admin('/incidents/approve',{'incident_id':True,'action':'probe_providers'})[0],400)
        self.assertEqual(self.admin('/incidents/evaluate',[])[0],400)
        self.assertEqual(self.admin('/incidents/approve',{'incident_id':2**64,'action':'probe_providers'})[0],400)
        self.assertEqual(self.request('GET','/incidents?id='+('9'*100),headers={'Authorization':'Bearer test-admin'})[0],400)
        self.assertEqual(self.admin('/incidents/evaluate',{'evidence':'injected'})[0],400)
        self.assertEqual(self.request('GET','/incidents/recommendations?id=x',headers={'Authorization':'Bearer test-admin'})[0],400)
        self.assertEqual(self.request('GET','/incidents?id=999',headers={'Authorization':'Bearer test-admin'})[0],409)

    def test_http_detection_approval_execution_and_storage_failure(self):
        for _ in range(5):self.gateway.router.observability.observe('/chat','POST',503,.01)
        status,_,body=self.admin('/incidents/evaluate',{})
        self.assertEqual(status,200)
        ident=body['result']['id']
        self.assertEqual(self.request('GET',f'/incidents/recommendations?id={ident}',headers={'Authorization':'Bearer test-admin'})[0],200)
        status,_,body=self.admin('/incidents/approve',{'incident_id':ident,'action':'probe_providers'})
        self.assertEqual(status,200)
        self.assertEqual(self.admin('/incidents/execute',{'incident_id':ident,'approval_id':body['result']['approval_id']})[0],200)
        self.assertFalse(self.admin('/incidents/verify',{'incident_id':ident})[2]['result']['recovered'])
        import sqlite3
        with patch.object(self.gateway.incidents,'list',side_effect=sqlite3.OperationalError('private')):
            status,_,body=self.request('GET','/incidents',headers={'Authorization':'Bearer test-admin'})
            self.assertEqual(status,503)
            self.assertNotIn('private',json.dumps(body))

    def test_full_http_incident_lifecycle(self):
        now = [1000.]
        clock = lambda:now[0]
        scenario = Path(self.temp.name)/'scenario.json'
        scenario.write_text(json.dumps({p:['503'] for p in self.gateway.router.health}))
        self.gateway.router.simulator = Simulator(scenario)
        self.gateway.router.clock = clock
        self.gateway.router.observability = Metrics(clock=clock)
        self.gateway.incidents.clock = clock
        self.gateway.incidents.mock_remediation = True
        self.gateway.keys = KeyRegistry({'test-starter':Customer('demo','Enterprise')},'test-admin')
        for _ in range(5):
            self.assertEqual(self.request('POST','/chat',json.dumps({'prompt':'fixed','provider':'OpenAI'}),
                                         {'Content-Type':'application/json'})[0],503)
        incident = self.request('GET','/incidents',headers={'Authorization':'Bearer test-admin'})[2]['incidents'][0]
        ident=incident['id']
        self.assertEqual(incident['severity'],'P1')
        approval=self.admin('/incidents/approve',{'incident_id':ident,'action':'restore_mock_providers'})[2]['result']['approval_id']
        now[0]+=6
        self.assertEqual(self.admin('/incidents/execute',{'incident_id':ident,'approval_id':approval})[0],200)
        self.assertFalse(self.admin('/incidents/verify',{'incident_id':ident})[2]['result']['recovered'])
        now[0]+=61
        self.assertEqual(self.request('GET','/health')[0],200)
        for _ in range(5):
            self.assertEqual(self.request('POST','/chat',json.dumps({'prompt':'fixed','provider':'OpenAI'}),
                                         {'Content-Type':'application/json'})[0],200)
        self.assertTrue(self.admin('/incidents/verify',{'incident_id':ident})[2]['result']['recovered'])
        self.assertEqual(self.admin('/incidents/execute',{'incident_id':ident,'approval_id':approval})[0],409)
