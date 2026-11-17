"""Render, schema-validate, and enforce SQLite/security wiring without a cluster.

Requires Helm 3.19.0, kubeconform 0.8.0, and PyYAML 6.0.3.
"""
import os
from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'charts/ai-gateway'
HELM = os.environ.get('HELM', str(ROOT / '.tools/helm'))
KUBECONFORM = os.environ.get('KUBECONFORM', str(ROOT / '.tools/kubeconform'))


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def policies(documents, port=8000, service_port=8000, secret='ai-gateway-keys'):
    objects = {obj['kind']: obj for obj in documents if obj}
    assert set(objects) == {'Deployment', 'Service', 'ConfigMap', 'PersistentVolumeClaim'}
    d, s, config, pvc = (objects[k] for k in
                       ['Deployment', 'Service', 'ConfigMap', 'PersistentVolumeClaim'])
    assert d['spec']['replicas'] == 1 and d['spec']['strategy'] == {'type': 'Recreate'}
    pod = d['spec']['template']
    assert d['spec']['selector']['matchLabels'] == s['spec']['selector'] == pod['metadata']['labels']
    spec = pod['spec']
    assert spec['automountServiceAccountToken'] is False
    context = spec['securityContext']
    assert context['runAsNonRoot'] and context['runAsUser'] == context['runAsGroup'] == context['fsGroup'] == 10001
    assert context['seccompProfile']['type'] == 'RuntimeDefault'
    assert len(spec['containers']) == 1
    c = spec['containers'][0]
    assert c['command'] == ['python', '-m', 'gateway.server']
    assert c['args'] == ['--host', '0.0.0.0', '--port', str(port), '--usage-db', '/data/usage.sqlite3']
    assert c['ports'] == [{'name': 'http', 'containerPort': port}]
    assert s['spec']['type'] == 'ClusterIP'
    assert s['spec']['ports'] == [{'name': 'http', 'port': service_port, 'targetPort': 'http'}]
    assert c['envFrom'] == [{'configMapRef': {'name': config['metadata']['name']}},
                            {'secretRef': {'name': secret}}]
    assert not any('KEY' in key.upper() for key in config['data'])
    security = c['securityContext']
    assert security['allowPrivilegeEscalation'] is False and security['readOnlyRootFilesystem'] is True
    assert security['capabilities']['drop'] == ['ALL']
    for kind in ['startup', 'readiness', 'liveness']:
        assert c[kind+'Probe']['httpGet'] == {'path': '/health', 'port': 'http'}
        assert c[kind+'Probe']['timeoutSeconds'] > 0
    assert c['volumeMounts'] == [{'name': 'usage', 'mountPath': '/data'}]
    assert spec['volumes'] == [{'name': 'usage', 'persistentVolumeClaim': {'claimName': pvc['metadata']['name']}}]
    assert pvc['spec']['accessModes'] == ['ReadWriteOncePod']
    for kind in ['requests', 'limits']:
        assert set(c['resources'][kind]) == {'cpu', 'memory'}


def validate(text):
    run(KUBECONFORM, '-strict', '-summary', '-kubernetes-version', '1.34.0', input=text)


def main():
    run(HELM, 'lint', str(CHART), '--strict')
    raw = '\n---\n'.join(p.read_text() for p in sorted((ROOT/'k8s').glob('*.yaml')))
    validate(raw)
    policies(list(yaml.safe_load_all(raw)))
    # Include the deliberately unpopulated example as an explicit stdin document.
    validate((ROOT/'k8s/secret.yaml.example').read_text())
    scenarios = [
        ('ai-gateway', [], 8000, 8000, 'ai-gateway-keys'),
        ('interview', ['--set', 'containerPort=8080,service.port=80,existingSecret=demo-keys,storage.storageClassName=fast,storage.size=2Gi,image.tag=review,probes.startup.failureThreshold=45', '--set-string', 'env.DEMO_MODE=enabled'], 8080, 80, 'demo-keys'),
        ('explicit-class', ['--set-string', 'storage.storageClassName='], 8000, 8000, 'ai-gateway-keys'),
    ]
    for release, overrides, port, service_port, secret in scenarios:
        rendered = subprocess.check_output([HELM, 'template', release, str(CHART), *overrides], text=True)
        validate(rendered)
        docs = list(yaml.safe_load_all(rendered))
        policies(docs, port, service_port, secret)
        objects = {o['kind']: o for o in docs}
        assert objects['PersistentVolumeClaim']['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
        claim = objects['PersistentVolumeClaim']['spec']
        if release == 'ai-gateway':
            assert 'storageClassName' not in claim
        elif release == 'explicit-class':
            assert claim['storageClassName'] == ''
        else:
            assert claim['storageClassName'] == 'fast' and claim['resources']['requests']['storage'] == '2Gi'
            assert objects['Deployment']['spec']['template']['spec']['containers'][0]['image'] == 'ai-gateway:review'
            assert objects['ConfigMap']['data']['DEMO_MODE'] == 'enabled'
        print('PASS: schema and wiring for ' + release)
    invalid = ['replicaCount=2', 'replicaCount=0', 'containerPort=80', 'service.port=65536',
               'existingSecret=', 'probes.startup.failureThreshold=0',
               'env.GATEWAY_STARTER_API_KEY=must-not-render', 'storage.size=0Gi']
    for override in invalid:
        result = subprocess.run([HELM, 'template', 'invalid', str(CHART), '--set', override],
                                capture_output=True, text=True)
        assert result.returncode != 0, 'Unsafe override accepted: ' + override.split('=')[0]
    print('PASS: 4 deployment policy scenarios, 8 rejected invalid overrides; 17 schema-valid resources')


if __name__ == '__main__':
    main()
