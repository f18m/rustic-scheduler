# Metrics

There are two sources of metrics and Prometheus must scrape **both**:

| Endpoint | Produced by | Prefix |
|---|---|---|
| `http://rustic-scheduler:9300/metrics` | the scheduler | `rustic_scheduler_` |
| `http://pushgateway:9091/metrics` | Rustic, through the Pushgateway | `rustic_backup_` |

The scheduler does not proxy or merge the Pushgateway's metrics. A ready-to-use scrape
configuration is in [examples/prometheus/prometheus.yml](../examples/prometheus/prometheus.yml):

```yaml
scrape_configs:
  - job_name: rustic-scheduler
    static_configs:
      - targets: ["rustic-scheduler:9300"]

  - job_name: pushgateway
    honor_labels: true
    static_configs:
      - targets: ["pushgateway:9091"]
```

## Scheduler metrics

Labels: `backup_job` (the job name), `operation` (`backup`, `snapshots`, `forget`, `prune`,
`check`) and small fixed vocabularies (`outcome`, `trigger`, `reason`, `result`, `event`).
The label is `backup_job`, not `job`, because Prometheus sets `job` itself and would rename a
scraped `job` label to `exported_job`.

There are no labels for run ids, snapshot ids, paths or error messages. **A value that is
unknown is an absent series, never zero.**

| Metric | Labels | Meaning |
|---|---|---|
| `rustic_scheduler_info` | `version`, `rustic_version` | Always 1. |
| `rustic_scheduler_start_time_seconds` | | Start of the scheduler. |
| `rustic_scheduler_ready` | | 1 when scheduled work can be launched. |
| `rustic_scheduler_loop_heartbeat_timestamp_seconds` | | Last iteration of the scheduling loop. |
| `rustic_scheduler_jobs` | | Configured jobs. |
| `rustic_scheduler_next_run_timestamp_seconds` | job, operation | Next scheduled run. |
| `rustic_scheduler_operation_running` | job, operation | 1 while running. |
| `rustic_scheduler_operation_queued` | job, operation | 1 while waiting for the repository. |
| `rustic_scheduler_operation_runs_total` | job, operation, `trigger`, `outcome`, `reason` | Finished and skipped runs. |
| `rustic_scheduler_missed_runs_total` | job, operation | Scheduled occurrences that passed unstarted. |
| `rustic_scheduler_operation_last_outcome` | job, operation, `outcome` | 1 for the latest outcome, 0 for the other four. Absent until something happened. |
| `rustic_scheduler_operation_last_start_timestamp_seconds` | job, operation | |
| `rustic_scheduler_operation_last_end_timestamp_seconds` | job, operation | |
| `rustic_scheduler_operation_last_success_timestamp_seconds` | job, operation | Unchanged by later failures. |
| `rustic_scheduler_operation_last_duration_seconds` | job, operation | |
| `rustic_scheduler_operation_last_exit_code` | job, operation | Rustic's exit status; absent if it never exited. |
| `rustic_scheduler_progress_bytes_done` | job, operation | Only while running. |
| `rustic_scheduler_progress_bytes_total` | job, operation | Absent when Rustic does not know it. |
| `rustic_scheduler_progress_ratio` | job, operation | 0 to 1; absent when unknown. |
| `rustic_scheduler_progress_eta_seconds` | job, operation | Absent when unknown. |
| `rustic_scheduler_progress_last_update_timestamp_seconds` | job, operation | |
| `rustic_scheduler_backup_last_files_new`, `_files_changed`, `_files_unmodified`, `_total_files_processed`, `_total_bytes_processed`, `_data_added`, `_data_added_packed`, `_snapshots_saved` | job | From the last **succeeded** backup. |
| `rustic_scheduler_snapshots` | job | Snapshots in the job's scope. |
| `rustic_scheduler_snapshot_latest_timestamp_seconds`, `_oldest_timestamp_seconds` | job | Absent when there is no snapshot. |
| `rustic_scheduler_snapshot_latest_bytes` | job | Bytes processed by the newest snapshot. |
| `rustic_scheduler_snapshot_collection_timestamp_seconds` | job | Last successful telemetry collection. Alert on its age. |
| `rustic_scheduler_snapshot_collection_errors_total` | job | |
| `rustic_scheduler_forget_last_removed`, `_last_kept`, `_last_dry_run` | job | Last forget; in a dry run, what would be removed. |
| `rustic_scheduler_pushgateway_push_verified` | job | 1 if Rustic's push of the last backup was found, else 0. |
| `rustic_scheduler_pushgateway_verifications_total` | job, `result` | `delivered`, `missing`, `stale`, `unreachable`. |
| `rustic_scheduler_pushgateway_last_push_timestamp_seconds` | job | As reported by the Pushgateway. |
| `rustic_scheduler_mqtt_connected` | | |
| `rustic_scheduler_mqtt_messages_total` | `result` | `published`, `dropped`, `connection_error`. |
| `rustic_scheduler_mqtt_commands_total` | `result` | Command messages: `accepted`, `rejected`, `invalid`. |
| `rustic_scheduler_notifications_total` | `event`, `result` | `sent` or `failed`. |
| `rustic_scheduler_notification_last_failure_timestamp_seconds` | | |
| `rustic_scheduler_state_write_errors_total` | | |

Progress covers the current byte-counting phase only; see
[rustic-integration.md](rustic-integration.md#what-the-scheduler-reads-from-rustic).

Example alerts are in [examples/prometheus/alerts.yml](../examples/prometheus/alerts.yml).

### Where the details are

Metrics say what happened. For why:

- `GET /api/v1/status` or `rustic-scheduler status`: last result of every operation with
  reason, exit status, detail text, snapshot ids and the path of the run log.
- Run logs in `<state_dir>/logs/<job>/<time>-<operation>.log`: Rustic's full output for that
  run, with secrets masked. The newest 20 per operation are kept.
- The container log.

## Rustic's own metrics and the Pushgateway

Rustic can push metrics about a completed backup to a Pushgateway. With `pushgateway.url`
set, the scheduler passes it to Rustic as `RUSTIC_PROMETHEUS`, and Rustic 0.11.4 does this
once per snapshot of a backup run:

- one HTTP `POST` to `<url>/metrics/job@base64/<job>/<label>@base64/<value>/...`
- body: the protobuf exposition format (`application/vnd.google.protobuf`, delimited)
- 23 gauges named `rustic_backup_*`, such as `rustic_backup_time`, `rustic_backup_files_new`,
  `rustic_backup_data_added`, `rustic_backup_total_bytes_processed`,
  `rustic_backup_backup_duration`
- grouping key: `job` (default `rustic_backup`; change it with `metrics-job` in a profile),
  `paths`, `hostname`, `snapshot_label`, `tags`. Empty values are left out. Extra labels come
  from `[global.metrics-labels]` and `[backup.metrics-labels]` in a profile.

Rustic pushes for `backup` only, and also when nothing changed and `skip-if-unchanged` kept
it from saving a snapshot.

### Delivery is verified

If the push fails, Rustic logs `error pushing metrics` and still exits 0. The scheduler does
not infer delivery from that. After each succeeded backup it asks the Pushgateway
(`GET /api/v1/metrics`) for a group with `snapshot_label` equal to the job's label (and the
job's `host`, if set) that contains Rustic's metrics, was pushed successfully, and was pushed
no earlier than the start of this backup. The result is recorded separately from the backup:

| Result | Meaning |
|---|---|
| `delivered` | Found. `rustic_scheduler_pushgateway_push_verified` is 1. |
| `missing` | The Pushgateway has no group for this job. |
| `stale` | It has one, but from an earlier backup. |
| `unreachable` | The Pushgateway did not answer. |

A failed push is never retried by Rustic or by the scheduler; the next backup pushes again.
The backup's outcome is unaffected.

Verification is skipped when the profiles set their own `[global] prometheus`, and when the
profiles override `snapshot_label` through `metrics-labels` it will report `missing`.

### What the Pushgateway keeps, and for how long

The Pushgateway is a cache, not a store with expiry:

- It keeps the **last pushed value of every group forever**, until the group is deleted or
  the Pushgateway restarts without a persistence file. It never expires anything.
- Rustic uses `POST`, which replaces metrics of the same name in the group and leaves other
  metrics of the group alone.
- Prometheus scrapes the same old values on every scrape. The sample timestamp is the scrape
  time, so **a pushed value looks fresh however old it is.** To tell how old it is, use
  `rustic_backup_time` (a Unix timestamp set by Rustic) or the Pushgateway's own
  `push_time_seconds`, and alert on their age.
- `push_failure_time_seconds` is non-zero after a rejected push.

**Persistence.** The Compose file starts the Pushgateway with
`--persistence.file=/pushgateway/metrics.db` on a named volume, so pushed metrics survive a
restart. Without it, a restart leaves the Pushgateway empty until each job's next backup, and
the series disappear from Prometheus meanwhile.

**Stale groups.** Because `paths` and `tags` are part of the grouping key, changing a job's
sources or tags makes Rustic push to a *new* group while the old one stays, frozen at its
last values. The same happens when a job is renamed (new `snapshot_label`) or deleted.

- Changed sources or tags: set `pushgateway.cleanup_stale_groups: true`. After a verified
  push, the scheduler deletes the other groups that carry this job's label (and host) and
  were not refreshed by this backup. It never touches groups of other labels.
- Renamed or deleted jobs: the scheduler no longer knows the old label, so delete those
  groups yourself, from the Pushgateway's web page or with its API:

  ```sh
  # list
  curl -s http://pushgateway:9091/api/v1/metrics | jq '.data[].labels'
  # delete one group: same path the push used
  curl -X DELETE http://pushgateway:9091/metrics/job/rustic_backup/snapshot_label/old-name/hostname/backup-host/paths@base64/L2RhdGEvZG9jdW1lbnRz
  ```

  Label values containing `/` must be given as `<name>@base64/<url-safe base64>`.

### Labels and `honor_labels`

Pushed metrics carry their own `job` label (`rustic_backup`) and an empty `instance`. Without
`honor_labels: true`, Prometheus would rename them to `exported_job` and attach the
Pushgateway's own `job` and `instance` to every series. With it, the pushed labels are kept,
which is the documented way to scrape a Pushgateway. Keep `honor_labels` to that one scrape
job.

Possible conflicts:

- Two jobs with the same label, host, paths and tags would share a group and overwrite each
  other. The scheduler refuses two jobs with the same label and host.
- A profile `metrics-labels` entry named like a grouping label replaces Rustic's value.
- The scheduler's metrics never clash: different prefix, and `backup_job` instead of `job`.

### Securing it

The Pushgateway has no authentication by default: whoever can reach it can overwrite or
delete metrics. The Compose file does not publish its port. If it must be reachable more
widely, put it behind a reverse proxy with basic authentication and give Rustic the
credentials with `prometheus-user` in a profile and `RUSTIC_PROMETHEUS_PASS` in the
environment. Do not put the password in `extra_args`.

See also <https://prometheus.io/docs/practices/pushing/>.
