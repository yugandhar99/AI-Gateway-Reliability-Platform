"""Isolated Docker checks. Retains stopped test containers and volume; never prunes."""
import json
import os
import secrets
import subprocess
import time
import uuid


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
    docker('volume', 'create', volume)

    def start(suffix):
        name = prefix + suffix
        docker('run', '-d', '--name', name, '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true',
               '--mount', f'type=volume,source={volume},target=/data',
               '-e', 'GATEWAY_STARTER_API_KEY', '-e', 'GATEWAY_ADMIN_API_KEY', image)
        containers.append(name)
        wait(name)
        return name

    def wait(name):
        for _ in range(60):
            if docker('inspect', '--format', '{{.State.Health.Status}}', name) == 'healthy':
                return
            time.sleep(1)
        raise AssertionError('container did not become healthy')

    def request(name, path, credential='none', payload=None):
        # Credential values stay inside the container; only variable names are passed.
        code = '''import json, os, sys, urllib.request, urllib.error
path, credential, payload = sys.argv[1:]
headers = {'Content-Type': 'application/json'}
if credential != 'none':
    key = os.environ.get(credential, 'invalid-smoke-key')
    headers['Authorization'] = 'Bearer ' + key
req = urllib.request.Request('http://127.0.0.1:8000'+path,
    None if payload == 'null' else payload.encode(), headers)
try:
    response = urllib.request.urlopen(req, timeout=5)
except urllib.error.HTTPError as error:
    response = error
print(json.dumps([response.code, json.load(response)]))
'''
        return json.loads(docker('exec', name, 'python', '-c', code, path,
                                 credential, json.dumps(payload)))

    try:
        name = start('-first')
        details = json.loads(docker('inspect', name))[0]
        check(details['Config']['User'] == '10001:10001', 'non-root image user')
        check(details['State']['Health']['Status'] == 'healthy', 'Docker health check')
        check(details['HostConfig']['ReadonlyRootfs'] and
              details['HostConfig']['CapDrop'] == ['ALL'] and
              'no-new-privileges:true' in details['HostConfig']['SecurityOpt'],
              'read-only root, dropped capabilities, no privilege escalation')
        check(not details['HostConfig']['PortBindings'], 'no host ports published')
        check(request(name, '/health')[0] == 200, 'public health')
        check(request(name, '/usage')[0] == 401, 'missing credentials rejected')
        check(request(name, '/usage', 'INVALID_SMOKE_KEY')[0] == 401,
              'invalid credentials rejected')
        key = 'GATEWAY_STARTER_API_KEY'
        status, before = request(name, '/usage', key)
        check(status == 200 and before['requests'] == 0, 'authenticated fresh database')
        check(request(name, '/chat', key, {'prompt': 'CI smoke', 'provider': 'OpenAI'})[0] == 200,
              'authenticated simulated chat')
        status, after = request(name, '/usage', key)
        check(status == 200 and after['requests'] == 1 and after['successes'] == 1,
              'SQLite writes on read-only root with writable volume')
        check(request(name, '/metrics', 'GATEWAY_ADMIN_API_KEY')[0] == 200,
              'administrator metrics')
        docker('restart', name)
        wait(name)
        check(request(name, '/usage', key) == [200, after], 'usage survives restart')
        docker('stop', name)
        replacement = start('-replacement')
        check(request(replacement, '/usage', key) == [200, after],
              'usage survives replacement using same volume')
        print('13 smoke checks passed')
    finally:
        for name in containers:
            docker('stop', name)
        print('Retained stopped test containers and volume: ' + volume)


if __name__ == '__main__':
    main()
