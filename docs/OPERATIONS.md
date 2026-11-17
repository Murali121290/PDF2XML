# Deployment and acceptance checks

The service is a single-host, single-worker deployment. Use one process per output volume.
Set `PDF2XML_API_KEYS` through environment secrets before startup; Docker Compose rejects an unset map.
Bind external ingress through TLS and apply request-rate and connection limits there.

## Container validation

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

Generate the synthetic regression corpus, set `PDF2XML_SMOKE_TOKEN` to one configured tenant token,
then run:

```powershell
python tools/deployment_smoke.py http://127.0.0.1:8087 out/regression-inputs/simple.pdf
```

The smoke check verifies readiness, denied anonymous access, asynchronous conversion and artifact
downloads using the real Docling worker. Restart the container and repeat the check. Verify that
completed job IDs remain accessible and interrupted work reports failed after restart. The CI
workflow automates build/start/conversion/restart health checks. Docker is not installed in the
development environment used for this change; do not treat its image as deployment-validated yet.

Linux bind mounts must be writable by UID 10001. The container has an 8 GiB memory limit, four-CPU
limit, dropped Linux capabilities and `no-new-privileges`. Adjust limits using measured capacity.
The health check verifies files and listener state, not inference. Model verification runs at build.

## Monitoring

Collect JSON logs from stdout/stderr and poll authenticated `/metrics` once per minute. Counts are
gauges over retained jobs, scoped to the calling tenant; they are not lifetime counters. Aggregate
across tenants only in a trusted monitoring service. Configure alerts in your monitoring platform:

`/metrics/prometheus` exposes the same tenant gauges in Prometheus text format. Starter scrape and
alert files are under `monitoring/`. Store a tenant token at the configured credentials file and
adjust the scrape target for your network. These files define queue/listener/availability and
15-minute failure-rate alerts; connect Prometheus to your Alertmanager separately. For a shared
queue, an operator must aggregate tenant backlogs against one queue capacity to detect combined
pressure. Metrics do not expose other tenants' jobs. Validate rules with `promtool check rules
monitoring/alerts.yml` when Prometheus tooling is installed.

| Signal | Initial action threshold |
|---|---|
| Readiness | Failed checks for 2 minutes |
| Queue saturation | Outstanding jobs above 80% of configured capacity for 5 minutes |
| Worker failures | Any deadline/crash/OOM; correlate `job_finished` state and container logs |
| Failure rate | Above 5% of completed jobs over 15 minutes, with at least 20 jobs |
| Review rate | Investigate increases from the accepted corpus baseline |
| Disk space | Less than 20% free, or retention cleanup errors |
| Latency and RSS | Compare uncached document-size buckets against the reviewed release baseline |

These are initial thresholds; no external monitoring account or notification receiver is connected.
Recent failure gauges use a rolling 15-minute window; compute other historical rates from logs.
Health endpoints intentionally omit application data and require no token.

## Release gates and rollback

Run `pip check`, all tests, model verification and the benchmark on supported platforms. Review
the generated benchmark JSON; distinguish cold inference, warm inference and cache reuse. Retain
reviewed references covering multi-column pages, tables with spans, long books, unusual fonts,
figures, repeated headers and malformed inputs. Do not replace the reference corpus with model
output. Synthetic acceptance checks are a starting point, not human editorial approval.

Version application artifacts, dependency constraints and model manifests together. Snapshot the
output volume before an upgrade. Roll back to the previous image and volume snapshot together
if job-record or canonical formats change. Model/library signature changes produce new cache keys.
Do not share one writable output volume across versions or hosts. A durable external scheduler
and database are required before introducing multiple hosts or transparent retries.
