"""Check Stage 7 dashboard datasource, IDs and incident metric coverage offline."""
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    dashboard = json.loads((root / 'monitoring/grafana/dashboards/gateway.json').read_text())
    panels = dashboard['panels']
    assert len({p['id'] for p in panels}) == len(panels)
    expressions = {t['expr'] for p in panels for t in p['targets']}
    for metric in ('gateway_incidents_active','gateway_incidents_resolved_total','gateway_incident_approvals_total'):
        assert metric in expressions, metric
    assert all(p['datasource']['uid'] == 'gateway-prometheus' for p in panels)
    print(f'PASS: {len(panels)} unique dashboard panels; all three incident metrics configured')


if __name__ == '__main__':
    main()
