"""Create local credentials once. Never print or overwrite existing credentials."""
import os
from pathlib import Path
import secrets


def main(root=None):
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    directory = root / 'secrets/monitoring'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for name in ('customer', 'admin', 'metrics', 'grafana'):
        path = directory / name
        if not path.exists():
            with open(path, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as output:
                output.write(secrets.token_urlsafe(32))
        # Gateway UID 10001 needs read access; parent directory remains private.
        if name in ('customer', 'admin', 'metrics'):
            path.chmod(0o644)
    runtime = root / 'monitoring/runtime'
    for name in ('prometheus', 'grafana', 'simulation'):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    scenario = runtime / 'simulation/outcomes.json'
    if not scenario.exists():
        scenario.write_text('{}\n')
    (root / '.env.monitoring').write_text(f'MONITORING_UID={os.getuid()}\nMONITORING_GID={os.getgid()}\n')
    print('Monitoring initialized. Credentials are stored locally and were not printed.')


if __name__ == '__main__':
    main()
