"""Finite mock traffic against only the Stage 6 loopback port; restores scenario."""
import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]


def scenario(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value))
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests', type=int, default=6, help='Requests per phase (1-20)')
    parser.add_argument('--interval', type=float, default=1, help='Seconds between requests (at least 1)')
    args = parser.parse_args()
    if not 1 <= args.requests <= 20 or not 1 <= args.interval <= 60:
        parser.error('Use 1-20 requests and interval 1-60 seconds')
    token = (ROOT / 'secrets/monitoring/customer').read_text().strip()
    path = ROOT / 'monitoring/runtime/simulation/outcomes.json'
    original = path.read_text()
    phases = [('normal', {}), ('preferred provider outage', {'OpenAI': ['503']}),
              ('all providers slow/unavailable', {p: ['timeout'] for p in ('OpenAI', 'Anthropic', 'Gemini')}),
              ('recovery', {})]
    try:
        for label, outcomes in phases:
            scenario(path, outcomes)
            if label == 'recovery':
                time.sleep(6)
            print(label)
            for _ in range(args.requests):
                req = Request('http://127.0.0.1:8003/chat', json.dumps({'provider':'OpenAI', 'prompt':'Stage 6 demo'}).encode(),
                              {'Content-Type':'application/json', 'Authorization':'Bearer ' + token})
                try:
                    with urlopen(req, timeout=5) as response:
                        print('  HTTP', response.status)
                except HTTPError as error:
                    print('  HTTP', error.code)
                    error.close()
                time.sleep(args.interval)
    finally:
        scenario(path, json.loads(original))


if __name__ == '__main__':
    main()
