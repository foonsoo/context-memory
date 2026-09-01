# Hosted operations and recovery runbook

This runbook applies to a future hosted deployment. It does not turn the
dependency-injected listener into a supported public service. P7-8's
development-host load and failure gate has passed, but deployment-specific
measurements remain mandatory before service-level objectives are published.

## Signals and exposure

`GET /healthz` is a process liveness check. It returns only `{"status":"ok"}`
and must not query tenant data. `GET /readyz` checks the configured migration
identifier, read-only SQLite integrity and required tables, and age of the most
recent confirmed backup. Its public response contains only `ready` or
`not_ready`; detailed check results stay in the operator process. Neither route
requires a user session, so neither may expose paths, versions, regions,
tenants, actors, database names, or backup object keys.

Every API request has a validated or generated request ID and trace ID. Both
are returned in response headers. Structured request logs contain only the
timestamp, request/trace IDs, operation class, status, elapsed milliseconds,
active-request depth, and coarse error class. Headers, peer addresses,
credentials, tenant/actor/project identifiers, query text, event content,
database paths, SQL, and exception strings are excluded. A telemetry-sink
failure never changes the API response.

The internal metrics snapshot provides:

- requests by operation and status, plus coarse client, rate-limit, listener,
  and server error counts;
- latency count, sum, and maximum by operation;
- current active-request depth;
- SQLite file bytes by operator-defined non-sensitive database label;
- age in seconds of the latest confirmed backup.

The deployment exporter may translate this snapshot to its metrics system but
must retain the bounded labels above. Tenant, actor, project, request ID, trace
ID, path, and arbitrary error text are forbidden metric labels.

## Initial alert thresholds

These are conservative operator alerts, not service-level objectives.
Production-like measurements may tighten them.

| Signal | Warning | Page / remove readiness |
|---|---:|---:|
| Readiness | one failed probe for 2 minutes | failed for 5 minutes |
| Backup age | 18 hours | 24 hours |
| Server errors | at least 1% over 5 minutes and 100 requests | at least 5% over 5 minutes and 100 requests |
| Average request latency | over 1 second for 10 minutes | over 3 seconds for 5 minutes |
| Active requests | over 80% of measured safe capacity for 5 minutes | at measured capacity for 2 minutes |
| Database bytes | 70% of provisioned storage | 85% of provisioned storage |
| Listener errors | any occurrence | 3 occurrences in 5 minutes |

The development-host gate establishes eight concurrent workers (four readers
and four writers) only as the initial pre-production test bound. It is not a
safe public-service capacity claim. Until a deployment-specific measurement is
recorded, configure no more than eight application workers, warn above six
active requests for five minutes, and remove readiness at eight active requests
for two minutes. Lower this bound when storage or runtime measurements require
it; never raise it from the development result alone. The edge must stop routing
new traffic whenever readiness is removed; liveness failure is reserved for a
stuck/dead process that should be restarted.

## Load and failure evidence

Run the dependency-free measurement from a clean checkout with:

```sh
PYTHONPATH=src python3 tests/hosted_load_measurement.py \
  --writers 4 --readers 4 --operations 100
```

The 2026-09-01 development-host run completed 800 operations at about 2,807
operations/second, committed all 400 events, and measured write p50/p95 at
0.79/5.58 ms and read p50/p95 at 2.73/3.71 ms. Record the runtime, storage
class, topology, configuration, raw output, and artifact revision whenever this
is repeated. Do not compare hosts from rounded numbers in this runbook.

The automated failure gate verifies contiguous event sequences under
concurrent reads and writes, abrupt exit before and after commit, WAL growth and
truncation, failed-migration rollback and forward recovery, and exact tenant
backup restoration. Socket regressions separately retain slow-body,
malformed/oversized request, disconnect-cancellation, and shutdown coverage.
An environment may publish SLOs only after these same cases pass on its selected
runtime, storage, edge, and regional topology with representative data volume.

## Migration and database incidents

Deployments set an expected migration identifier independently of the running
artifact. A mismatch removes readiness. Before a migration, take and verify a
backup, stop or drain writers, run the migration, verify required tables and
`PRAGMA quick_check`, then restore readiness. Never change the expected value
merely to silence an alert. Forward recovery or rollback behavior is proven in
P7-8.

SQLite `ENOSPC`, quota exhaustion, and `database or disk is full` failures map
to a stable `507 storage_exhausted` API response with no path or SQL. On alert:

1. remove readiness and drain writes;
2. preserve the database, WAL, and SHM files as one unit and inspect filesystem
   capacity outside application logs;
3. expand storage or remove only verified-expired external backups—never delete
   live SQLite/WAL files as cleanup;
4. run integrity and restore checks, then resume writes;
5. record request/trace IDs, coarse metrics, cause, and remediation without
   copying user content or secrets into the incident record.

## Backup and restore drill

`run_sqlite_restore_drill` opens the source read-only, uses SQLite's online
backup API to restore into an isolated temporary database, runs full integrity
checking, and verifies required tables. It never replaces the live database.
Run it at least daily against the latest application backup in each configured
region and after every migration or storage change. A drill is successful only
when the expected backup object is downloaded, cryptographic/provider integrity
is confirmed, the isolated restore returns `passed`, and an operator records
the backup timestamp, artifact version, migration identifier, duration, and
result. Delete the temporary restore after evidence is recorded.

If a drill fails, remove readiness when no other verified backup is younger
than 24 hours, retain the failed artifact for restricted investigation, create
a fresh backup if the source is healthy, and repeat the drill. Do not mark a
backup healthy based only on object existence or upload success.
