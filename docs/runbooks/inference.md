# Operating asynchronous inference

## Start the local gateway

Run these commands from the project root in Bash. They use local demo
credentials and a disposable database inside the checkout.

```bash
cd ~/projects/ai-gateway-advanced-mock
mkdir -p "$PWD/tmp"
run_dir=$(mktemp -d "$PWD/tmp/inference-demo.XXXXXX")
printf '{}\n' > "$run_dir/providers.json"
export GATEWAY_BUSINESS_API_KEY=inference-demo-business
export GATEWAY_STARTER_API_KEY=inference-demo-starter
export GATEWAY_METRICS_API_KEY=inference-demo-metrics
python3 -m gateway.server --host 127.0.0.1 --port 8010 \
  --usage-db "$run_dir/usage.sqlite3" --workers 2 \
  --job-lease-seconds 2 --job-retry-base 1 --job-retry-cap 4 \
  --simulation-file "$run_dir/providers.json" --worker-crash-file "$run_dir/crash-next" &
gateway_pid=$!
trap 'kill -TERM "$gateway_pid" 2>/dev/null || true; wait "$gateway_pid" 2>/dev/null || true' EXIT
base=http://127.0.0.1:8010
for attempt in {1..50}; do
  if curl -fs "$base/health"; then break; fi
  sleep 0.1
done

submit() {
  curl -fsS -H "Authorization: Bearer $GATEWAY_BUSINESS_API_KEY" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $1" \
    -d '{"provider":"OpenAI","prompt":"hello","priority":7}' "$base/jobs" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'
}
job_status() {
  curl -fsS -H "Authorization: Bearer $GATEWAY_BUSINESS_API_KEY" "$base/jobs/$1"
}
scrape() {
  curl -fsS -H "Authorization: Bearer $GATEWAY_METRICS_API_KEY" "$base/metrics/prometheus"
}
job=$(submit first)
test "$(submit first)" = "$job"
job_status "$job"
sleep 1
curl -fsS -H "Authorization: Bearer $GATEWAY_BUSINESS_API_KEY" "$base/jobs/$job/result"
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $GATEWAY_STARTER_API_KEY" "$base/jobs/$job"
curl -fsS -H "Authorization: Bearer $GATEWAY_BUSINESS_API_KEY" "$base/usage"
```

Expect one settled submission and a 404 from the Starter key.

## Exercise crash recovery, provider outage, and cancellation

Continue in the same shell. Creating the marker opts the next claim
into a simulated worker thread exit before its first heartbeat. The
supervisor replaces the thread. Run fault injection only on a test instance.

```bash
touch "$run_dir/crash-next"
crash_job=$(submit crash)
sleep 5
job_status "$crash_job"
scrape | grep -E 'gateway_job_events_total|gateway_workers_alive'

printf '%s\n' '{"OpenAI":["503"],"Anthropic":["503"],"Gemini":["503"]}' > "$run_dir/providers.next"
mv "$run_dir/providers.next" "$run_dir/providers.json"
outage_job=$(submit outage)
sleep 5
job_status "$outage_job"
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $GATEWAY_BUSINESS_API_KEY" "$base/jobs/$outage_job/result"

cancel_job=$(submit cancel)
curl -fsS -X DELETE -H "Authorization: Bearer $GATEWAY_BUSINESS_API_KEY" "$base/jobs/$cancel_job"
printf '{}\n' > "$run_dir/providers.next"
mv "$run_dir/providers.next" "$run_dir/providers.json"
sleep 5
recovered_job=$(submit recovered)
sleep 1
job_status "$recovered_job"
kill -TERM "$gateway_pid"
wait "$gateway_pid"
trap - EXIT
```

Expect the crash job to succeed on attempt 2 and increment
`gateway_job_events_total{event="lease_expired"}`. The outage job should
reach `dead_letter` on attempt 3 and its result endpoint should return 409.
The restored provider scenario should allow new work to succeed once
the router's circuit recovery interval has elapsed. Each job ID
remains readable after restarting the gateway against the same database.

## Diagnose queue pressure and stalled work

- Watch ready age, active job count, and submission 429s. Respect
  `Retry-After` and retry uncertain submissions with the same key.
  Compare queue-full codes with request-rate and quota errors.
- Compare worker liveness with the configured worker count. A worker
  gauge measures thread liveness. Check ready age and transition counters
  to distinguish live workers from a queue making progress. API-only mode
  (`--workers 0`) leaves processing and lease recovery to a process
  using the same database.
- Check `retried`, `lease_expired`, and `dead_letter` events alongside
  provider circuit state. Investigate SQLite contention or process
  restarts if lease expiration rises. After correcting the cause, submit
  a new job with a fresh idempotency key for any dead-lettered work
  that still needs execution.
- SIGTERM and Ctrl-C stop new worker claims and drain the current simulated
  attempts with heartbeats maintained. Pending jobs stay durable. Set
  deployment shutdown time above your configured provider timeouts and
  retry/failover budget within a single attempt, plus storage latency.
  Forced termination leaves leases for recovery. Graceful shutdown
  waits for simulator calls to return.
- Back up SQLite with its online backup API or after stopping all
  writers. Protect database files and backups because they hold prompts and
  results. Put external traffic behind TLS and restrict local write access
  to the database and fault-injection paths.

Defaults are 2 workers, 1000 active jobs globally, 100 per tenant, 3
attempts, a 15-second lease, a 60-second aging window, and retry delays
starting at 1 second capped at 60 seconds. See `python3 -m gateway.server
--help` for overrides. Use matching queue configuration on processes that
share a database. Use one scrape target per SQLite file for durable queue
gauges and event counters. Worker gauges are local to each process.

The existing Compose monitoring stack provisions the updated Grafana
dashboard. Follow [the monitoring setup](../monitoring.md) and submit jobs
to its gateway to populate the inference panels.

## Repeat the engineering validation on Ubuntu

Use Python 3.10 or later. The harness uses the standard library and
launches its own gateway processes with generated credentials, ephemeral
loopback ports, and independent SQLite databases. It prints each scenario's
wall-clock time and retains logs, databases, and two Prometheus scrapes in
an ignored directory under `tmp/`. It exits nonzero on the first failed
scenario.

```sh
cd ~/projects/ai-gateway-advanced-mock
python3 -m unittest discover -v
python3 scripts/validate_inference.py
```

If Prometheus `promtool` is installed, add
`--promtool "$(command -v promtool)"` to validate both captured scrapes
with Prometheus's own parser and linter. Otherwise run the following with
Docker, replacing `validation_dir` with the artifact path printed by the
harness.

```sh
validation_dir="$PWD/tmp/inference-validation-REPLACE_WITH_PRINTED_SUFFIX"
for scrape in "$validation_dir/gateway.prom" "$validation_dir/restarted-gateway.prom"; do
  docker run --rm -i --network none --entrypoint promtool \
    prom/prometheus:v3.7.3 check metrics < "$scrape" || exit 1
done
```

For the interactive HTTP demo, run the two Bash blocks above in the same
shell, or execute them together from the project root:

```sh
awk '/^```bash/{capture=1; next} /^```/{capture=0} capture' \
  docs/runbooks/inference.md | bash -eo pipefail
```

Run the [monitoring Compose commands](../deployment.md#prometheus-and-grafana)
on an isolated local stack to inspect the six inference panels in Grafana.
The asynchronous checks and measured results are recorded in the
[validation log](../validation.md#asynchronous-inference-engineering-validation).
