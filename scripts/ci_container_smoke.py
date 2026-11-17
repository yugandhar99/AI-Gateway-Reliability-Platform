"""Verify the gateway image with disposable, network-isolated Docker resources."""
import json
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print('PASS: ' + label)


def main():
    image = os.environ.get('SMOKE_IMAGE', 'ai-gateway:stage5')
    prefix = 'ai-gateway-ci-' + uuid.uuid4().hex
    volume = prefix + '-usage'
    containers = []
    os.environ['GATEWAY_STARTER_API_KEY'] = secrets.token_urlsafe(32)
    os.environ['GATEWAY_ADMIN_API_KEY'] = secrets.token_urlsafe(32)
    os.environ['GATEWAY_METRICS_API_KEY'] = secrets.token_urlsafe(32)
    fixtures = TemporaryDirectory(prefix='gateway-smoke-')
    fixture_dir = Path(fixtures.name)
    fixture_dir.chmod(0o755)
    (fixture_dir / 'policy.json').write_text(json.dumps({
        'OpenAI': {'timeout_seconds': 6, 'max_retries': 0},
    }), encoding='utf-8')
    (fixture_dir / 'outcomes.json').write_text('{"OpenAI":["timeout"]}', encoding='utf-8')
    for fixture in fixture_dir.iterdir():
        fixture.chmod(0o644)

    def start(suffix, workers=None):
        name = prefix + suffix
        options = []
        command = []
        if workers is not None:
            command = ['python', '-m', 'gateway.server', '--host', '0.0.0.0',
                       '--port', '8000', '--usage-db', '/data/usage.sqlite3',
                       '--workers', str(workers)]
        if workers == 1:
            options = ['--mount', f'type=bind,source={fixture_dir},target=/fixtures,readonly']
            command += ['--config', '/fixtures/policy.json',
                        '--simulation-file', '/fixtures/outcomes.json']
        docker('run', '-d', '--name', name, '--network', 'none',
               '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true',
               '--mount', f'type=volume,source={volume},target=/data',
               '-e', 'GATEWAY_STARTER_API_KEY', '-e', 'GATEWAY_ADMIN_API_KEY',
               '-e', 'GATEWAY_METRICS_API_KEY', *options, image, *command)
        containers.append(name)
        wait(name)
        return name

    def wait(name):
        for _ in range(60):
            if docker('inspect', '--format', '{{.State.Health.Status}}', name) == 'healthy':
                return
            time.sleep(1)
        raise AssertionError('container did not become healthy')

    def request(name, path, credential='none', payload=None, idempotency_key=''):
        # Credential values stay inside the container; only variable names are passed.
        code = '''import json, os, sys, urllib.request, urllib.error
path, credential, payload, idempotency_key = sys.argv[1:]
headers = {'Content-Type': 'application/json'}
if credential != 'none':
    key = os.environ.get(credential, 'invalid-smoke-key')
    headers['Authorization'] = 'Bearer ' + key
if idempotency_key:
    headers['Idempotency-Key'] = idempotency_key
req = urllib.request.Request('http://127.0.0.1:8000'+path,
    None if payload == 'null' else payload.encode(), headers)
try:
    response = urllib.request.urlopen(req, timeout=5)
except urllib.error.HTTPError as error:
    response = error
with response:
    body = response.read().decode()
    if response.headers.get_content_type() == 'application/json':
        body = json.loads(body)
    print(json.dumps([response.code, body, response.headers.get('Location')]))
'''
        return json.loads(docker('exec', name, 'python', '-c', code, path,
                                 credential, json.dumps(payload), idempotency_key))

    key = 'GATEWAY_STARTER_API_KEY'
    metrics_key = 'GATEWAY_METRICS_API_KEY'

    def submit(name, prompt, idempotency_key=''):
        return request(name, '/jobs', key, {'prompt': prompt, 'provider': 'OpenAI'},
                       idempotency_key)

    def await_job(name, path, expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, job, _ = request(name, path, key)
            if status != 200:
                raise AssertionError(f'Job status endpoint returned HTTP {status}')
            if job['status'] == expected:
                return job
            if job['status'] in {'succeeded', 'dead_letter', 'cancelled'}:
                break
            time.sleep(0.1)
        raise AssertionError(f'Job did not reach {expected}')

    def stop(name):
        docker('stop', '--time', '15', name)
        check(docker('inspect', '--format', '{{.State.ExitCode}}', name) == '0',
              'Docker SIGTERM exits cleanly')

    try:
        docker('volume', 'create', volume)
        name = start('-first')
        details = json.loads(docker('inspect', name))[0]
        check(details['Config']['User'] == '10001:10001', 'non-root image user')
        files = json.loads(docker('exec', name, 'python', '-c',
                                  'import json, pathlib; '
                                  'print(json.dumps([str(p) for p in pathlib.Path("/app").rglob("*")]))'))
        sources = {Path(path) for path in files} - {Path('/app/gateway')}
        check({Path('/app/gateway/jobs.py'), Path('/app/gateway/workers.py')} <= sources
              and all(path.parent == Path('/app/gateway') and path.suffix == '.py'
                      for path in sources), 'application image contains only gateway Python source')
        check(details['State']['Health']['Status'] == 'healthy', 'Docker health check')
        check(details['HostConfig']['ReadonlyRootfs'] and
              details['HostConfig']['CapDrop'] == ['ALL'] and
              'no-new-privileges:true' in details['HostConfig']['SecurityOpt'],
              'read-only root, dropped capabilities, no privilege escalation')
        check(details['HostConfig']['NetworkMode'] == 'none'
              and not details['HostConfig']['PortBindings'],
              'network isolated with no host ports published')
        check(request(name, '/health')[0] == 200, 'public health')
        check(request(name, '/usage')[0] == 401, 'missing credentials rejected')
        check(request(name, '/usage', 'INVALID_SMOKE_KEY')[0] == 401,
              'invalid credentials rejected')
        status, before, _ = request(name, '/usage', key)
        check(status == 200 and before['requests'] == 0, 'authenticated fresh database')
        check(request(name, '/chat', key, {'prompt': 'CI smoke', 'provider': 'OpenAI'})[0] == 200,
              'authenticated simulated chat')
        status, after, _ = request(name, '/usage', key)
        check(status == 200 and after['requests'] == 1 and after['successes'] == 1,
              'SQLite writes on read-only root with writable volume')
        check(request(name, '/metrics', 'GATEWAY_ADMIN_API_KEY')[0] == 200,
              'administrator metrics')
        status, queued, location = submit(name, 'Docker async smoke', 'docker-smoke')
        check(status == 202 and queued['status'] == 'queued'
              and location == '/jobs/' + queued['id'], 'asynchronous submission and Location')
        job = await_job(name, location, 'succeeded')
        result = request(name, location + '/result', key)
        check(job['attempts'] == 1 and result[0] == 200
              and result[1]['result']['response'].endswith('Docker async smoke'),
              'worker completes the job on its first claim and returns its result')
        usage = request(name, '/usage', key)
        check(usage[0] == 200 and usage[1]['requests'] == 2 and usage[1]['successes'] == 2
              and usage[1]['reserved_tokens'] == 0, 'job settles its SQLite usage reservation')
        check(request(name, '/metrics/prometheus', key)[0] == 401,
              'customer credential cannot scrape metrics')
        status, metrics, _ = request(name, '/metrics/prometheus', metrics_key)
        check(status == 200 and {'gateway_workers_configured 2', 'gateway_workers_alive 2',
              'gateway_jobs{state="succeeded"} 1', 'gateway_job_events_total{event="succeeded"} 1'}
              <= set(metrics.splitlines()), 'authenticated queue and default-worker metrics')
        docker('restart', '--time', '15', name)
        wait(name)
        check(request(name, '/usage', key) == usage
              and request(name, location + '/result', key) == result,
              'usage and result survive container restart')
        stop(name)

        draining = start('-drain', workers=1)
        status, _, active = submit(draining, 'Docker drain')
        check(status == 202, 'single-worker container accepts a job')
        await_job(draining, active, 'running')
        status, _, pending = submit(draining, 'Docker queued across stop')
        check(status == 202 and request(draining, pending + '/result', key)[0] == 202,
              'second job remains pending while the single worker is occupied')
        stop(draining)

        api_only = start('-api-only', workers=0)
        status, metrics, _ = request(api_only, '/metrics/prometheus', metrics_key)
        check(status == 200 and {'gateway_workers_configured 0', 'gateway_workers_alive 0'}
              <= set(metrics.splitlines()), 'API-only startup disables worker claims')
        drained = await_job(api_only, active, 'succeeded')
        check(drained['attempts'] == 1 and request(api_only, active + '/result', key)[0] == 200
              and request(api_only, pending, key)[1]['status'] == 'queued',
              'SIGTERM lets the active claim finish and preserves queued work')
        check(request(api_only, location + '/result', key) == result,
              'completed result survives replacement using the same volume')
        status, replay, replay_location = submit(api_only, 'Docker async smoke', 'docker-smoke')
        check(status == 200 and replay['id'] == queued['id'] and replay_location == location,
              'idempotency survives container replacement')
        stop(api_only)

        replacement = start('-replacement')
        check(await_job(replacement, pending, 'succeeded')['attempts'] == 1,
              'replacement processes the preserved queue')
        status, final_usage, _ = request(replacement, '/usage', key)
        check(status == 200 and final_usage['requests'] == 4 and final_usage['successes'] == 4
              and final_usage['reserved_tokens'] == 0, 'persistent accounting includes all jobs once')
        stop(replacement)
        print('All isolated container smoke checks passed')
    finally:
        for name in containers:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, check=False)
        subprocess.run(['docker', 'volume', 'rm', volume], stdout=subprocess.DEVNULL, check=False)
        fixtures.cleanup()


if __name__ == '__main__':
    main()
