"""Bounded, process-local Prometheus text metrics; no user-controlled labels."""
from collections import Counter
from threading import Lock


class Metrics:
    buckets = (.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10)

    def __init__(self):
        self.lock = Lock()
        self.requests = Counter()
        self.latency = Counter()
        self.events = Counter()
        self.tokens = 0

    def observe(self, route, method, status, elapsed):
        with self.lock:
            self.requests[route, method, str(status)] += 1
            self.latency[route, 'count'] += 1
            self.latency[route, 'sum'] += elapsed
            for bound in self.buckets:
                self.latency[route, str(bound)] += elapsed <= bound

    def event(self, event, provider):
        if event not in ('provider_failure', 'retry', 'failover'):
            return
        with self.lock:
            self.events[event, provider] += 1

    def add_tokens(self, count):
        with self.lock:
            self.tokens += count

    def render(self, router):
        lines = []
        def family(name, kind, help_text):
            lines.extend([f'# HELP gateway_{name} {help_text}', f'# TYPE gateway_{name} {kind}'])
        def sample(name, value, **labels):
            suffix = '{' + ','.join(f'{k}="{v}"' for k, v in labels.items()) + '}' if labels else ''
            lines.append(f'gateway_{name}{suffix} {value}')
        with self.lock:
            family('http_requests_total', 'counter', 'Completed HTTP requests including errors.')
            for (route, method, status), count in sorted(self.requests.items()):
                sample('http_requests_total', count, route=route, method=method, status=status)
            for name, status in [('authentication_failures_total', '401'), ('rate_limits_total', '429')]:
                family(name, 'counter', 'HTTP rejections since process start.')
                sample(name, sum(v for (_, _, s), v in self.requests.items() if s == status))
            family('http_request_duration_seconds', 'histogram', 'HTTP duration through response write.')
            for route in sorted({r for r, _ in self.latency}):
                for bound in self.buckets:
                    sample('http_request_duration_seconds_bucket', self.latency[route, str(bound)], route=route, le=str(bound))
                sample('http_request_duration_seconds_bucket', self.latency[route, 'count'], route=route, le='+Inf')
                for suffix in ('count', 'sum'):
                    sample('http_request_duration_seconds_' + suffix, self.latency[route, suffix], route=route)
            for event in ('provider_failure', 'retry', 'failover'):
                name = {'retry': 'retries_total', 'provider_failure': 'provider_failures_total', 'failover': 'failovers_total'}[event]
                family(name, 'counter', 'Routing events since process start.')
                for provider in router.health:
                    sample(name, self.events[event, provider], provider=provider)
            family('tokens_total', 'counter', 'Estimated tokens settled successfully since process start.')
            sample('tokens_total', self.tokens)
        snapshot = router.metrics()['providers']
        family('provider_available', 'gauge', 'One when circuit is closed and no consecutive failures.')
        for provider, health in snapshot.items():
            sample('provider_available', int(health['healthy']), provider=provider)
        family('circuit_breaker_state', 'gauge', 'One hot observed circuit state; recovery probes require traffic.')
        for provider, health in snapshot.items():
            for state in ('CLOSED', 'OPEN', 'HALF_OPEN'):
                sample('circuit_breaker_state', int(health['state'] == state), provider=provider, state=state)
        return ('\n'.join(lines) + '\n').encode('utf-8')
