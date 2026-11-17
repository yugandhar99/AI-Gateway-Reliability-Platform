# Monitoring dashboards and alerts

The [provisioned Grafana dashboard](../monitoring/grafana/dashboards/gateway.json) has eighteen panels. Start the separate monitoring stack using the [deployment guide](deployment.md#prometheus-and-grafana), then open `http://127.0.0.1:3000`. Use the generated local credentials; never include them in captures.

| Panel | Operational purpose |
|---|---|
| Chat throughput | Observe chat request traffic |
| Chat HTTP error rate (4xx + 5xx) | Distinguish overall request errors from the server-only alert |
| Chat P95 latency | Inspect tail latency |
| Provider availability | Inspect simulator availability |
| Failover events | Observe fallback activity |
| Circuit breaker states | Inspect closed, open and half-open states |
| Provider failures and retries | Correlate provider errors with repeated attempts |
| Aggregate estimated tokens | Observe aggregate estimated usage; not billing data |
| Authentication and rate limit rejections | Identify rejected credentials and admission limits |
| Active incidents by severity | Track active P1/P2/P3 incidents |
| Resolved incidents | Inspect recorded resolutions |
| Human remediation approvals | Observe administrator approval events |
| Inference jobs by state | Persisted counts by queued, running, succeeded, cancelled, and dead-letter status. |
| Inference queue pressure | Queued plus running jobs as a fraction of global capacity. |
| Oldest ready inference job | Seconds since the oldest eligible queued attempt became ready. |
| Inference workers | Live worker threads and configured slots per scrape target. |
| Inference retries and lease recovery | Five-minute counts of rescheduling, lease expiry, and dead-letter transitions. |
| Inference submission backpressure | Rate of job submission 429 responses, including tenant limits. |

Prometheus scrapes the authenticated `/metrics/prometheus` endpoint with a dedicated file-mounted key. Metric labels omit prompts, credentials and customer identities. HTTP and routing counters reset on process restart. Queue transition counters, SQLite accounting, and incident audit records persist. The approvals panel counts API approvals and does not prove distinct operator identities.

The [alert rules](../monitoring/alerts.yml) evaluate scrape unavailability for one minute, chat P95 latency above 500ms for two minutes, and chat server error rate above 10% for two minutes. The latency and error expressions use five-minute rate windows. Dashboard errors include 4xx and 5xx; the error alert includes only 5xx. Alertmanager and external notifications are not configured.

Check target status at `http://127.0.0.1:9090/targets`. `python3 scripts/check_monitoring.py` verifies the local stack once running. Rule behavior is covered by [Prometheus fixtures](../monitoring/alerts.test.yml); see the deployment guide for commands and the [validation record](validation.md) for checks actually performed.

Use [screenshot guidance](../screenshots/README.md) when recording genuine dashboard evidence. The README gallery records genuine local runtime captures and their provenance; it does not demonstrate cloud deployment.
