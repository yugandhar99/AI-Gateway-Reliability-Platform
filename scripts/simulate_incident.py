"""Isolated Stage 7 HTTP demo; no Docker, existing service, or real API access.

Default: detection-only. --interactive asks a human before approval/execution.
All artifacts stay under the selected new directory inside this repository.
"""
import argparse
import json
from pathlib import Path
import secrets
import sys
from threading import Thread
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gateway.reliability import Router, Simulator
from gateway.security import Customer, KeyRegistry
from gateway.server import create_server
from gateway.usage import UsageStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='monitoring/runtime/stage7-demo',
                        help='New artifact directory inside this repository (must not exist)')
    parser.add_argument('--interactive', action='store_true', help='Ask before approving mock recovery')
    args = parser.parse_args()
    directory = (ROOT / args.directory).resolve()
    if not directory.is_relative_to(ROOT) or directory == ROOT:
        parser.error('Artifacts must stay inside this repository')
    try:
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        parser.error('Choose a new --directory to preserve previous audit history')
    scenario = directory / 'scenario.json'
    scenario.write_text(json.dumps({p:['503'] for p in ('OpenAI','Anthropic','Gemini')}))
    router = Router(simulator=Simulator(scenario))
    customer, admin, scraper = (secrets.token_urlsafe(32) for _ in range(3))
    usage = UsageStore(directory / 'incidents.sqlite3')
    with create_server('127.0.0.1', 0, router, KeyRegistry(
            {customer:Customer('incident-demo','Enterprise')},admin), usage,
            metrics_key=scraper, mock_remediation=True) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def call(path, token=admin, payload=None):
            headers = {'Authorization':'Bearer '+token}
            data = None
            if payload is not None:
                headers['Content-Type']='application/json'
                data = json.dumps(payload).encode()
            try:
                with urlopen(Request(base+path,data,headers),timeout=10) as r:
                    return r.status,json.load(r)
            except HTTPError as error:
                with error:
                    return error.code,json.load(error)
        try:
            print('Isolated loopback server started; credentials stay in memory.')
            for _ in range(5):
                assert call('/chat',customer,{'prompt':'Stage 7 fixed demo','provider':'OpenAI'})[0] == 503
            status, data = call('/incidents/evaluate',payload={})
            assert status == 200
            ident = data['result']['id']
            assert data['result']['severity'] == 'P1'
            print('DETECTED:', data['result']['summary'])
            status, data = call(f'/incidents/recommendations?id={ident}')
            assert status == 200
            print('RECOMMENDATIONS:', ', '.join(r['action'] for r in data['result']))
            assert call('/incidents/execute',payload={'incident_id':ident,'approval_id':1})[0] == 409
            print('PASS: execution without approval rejected.')
            if not args.interactive:
                print('Detection-only complete. Use --interactive and a new directory for human-approved recovery.')
                return
            print('restore_mock_providers affects only this demo process; scenario files remain unchanged.')
            if input('Type APPROVE to authorize this incident remediation: ').strip() != 'APPROVE':
                print('No remediation approved or executed.')
                return
            status, data = call('/incidents/approve',payload={'incident_id':ident,'action':'restore_mock_providers'})
            assert status == 200
            approval = data['result']['approval_id']
            # Let the ordinary circuit cooldown expire; do not force circuits closed.
            time.sleep(6)
            assert call('/incidents/execute',payload={'incident_id':ident,'approval_id':approval})[0] == 200
            assert not call('/incidents/verify',payload={'incident_id':ident})[1]['result']['recovered']
            print('EXECUTED: mock override and normal probes. Waiting 61s for old failures to leave the metric window.')
            time.sleep(31)
            print('Recovery observation continues; existing services are untouched.')
            time.sleep(30)
            assert call('/health',token=customer)[1]['status'] == 'healthy'
            for _ in range(5):
                assert call('/chat',customer,{'prompt':'Stage 7 recovery','provider':'OpenAI'})[0] == 200
            status, data = call('/incidents/verify',payload={'incident_id':ident})
            assert status == 200 and data['result']['recovered']
            assert call('/incidents/execute',payload={'incident_id':ident,'approval_id':approval})[0] == 409
            print('RECOVERED: health check, healthy provider circuits, five fresh successful chats, no errors/latency spike.')
            print('PASS: approval replay rejected; incident and audit history persisted.')
        finally:
            server.shutdown()
            worker.join(timeout=5)
            print('Audit database:', (directory / 'incidents.sqlite3').relative_to(ROOT))


if __name__ == '__main__':
    main()
