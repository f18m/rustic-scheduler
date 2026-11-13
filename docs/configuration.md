# Configuration reference

The scheduler reads one YAML file (default `/etc/rustic-scheduler/config.yaml`, or
`-c FILE`, or `$RUSTIC_SCHEDULER_CONFIG`). Only `jobs` is required. Unknown settings are
errors, not ignored. The file is read at start-up; restart the container to apply changes.

Durations accept `30s`, `15m`, `12h`, `7d`, combinations such as `1h30m`, or a plain number
of seconds.

Rustic's own options do not belong here. They stay in your Rustic TOML profiles; see
[rustic-integration.md](rustic-integration.md).

## `scheduler`

| Setting | Default | Meaning |
|---|---|---|
| `timezone` | none | IANA timezone used by schedules that do not name one. There is no implicit fallback to the container's timezone: a schedule without any timezone is an error. |
| `state_dir` | `/var/lib/rustic-scheduler` | Last results, run logs, lock files, control socket. `null` disables all of them. |
| `missed_run_policy` | `catch_up` | Default for jobs: `catch_up` or `skip`. See [behaviour.md](behaviour.md#missed-runs). |
| `misfire_grace` | `24h` | How late a run may still start under `catch_up`. |
| `stop_grace` | `30s` | Time Rustic gets between SIGTERM and SIGKILL. |
| `run_logs_per_operation` | `20` | Run logs kept per job and operation. `0` disables run logs. |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `log_format` | `text` | `text` or `json`. |

## `rustic`

| Setting | Default | Meaning |
|---|---|---|
| `binary` | automatic | Path to Rustic. Automatic: the capability-carrying copy when the container has `CAP_DAC_READ_SEARCH`, otherwise `rustic` from `PATH`. |
| `profile_dir` | `/etc/rustic-scheduler/profiles` | Where profile names in jobs are looked up. |
| `progress_interval` | `5s` | How often Rustic reports progress. Not passed if a profile sets `progress-interval`. |
| `env` | `{}` | Extra environment for every Rustic process. Not for secrets: point to secret files instead. |

## `http`

| Setting | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Listen address for `/metrics`, `/healthz`, `/readyz`, `/api/v1/status`. |
| `port` | `9300` | Listen port. |
| `max_heartbeat_age` | `90s` | Health and readiness fail when the scheduling loop has not iterated for this long. |

## `web`

The optional browser interface. See [web.md](web.md).

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Serve the web interface. |
| `host`, `port` | `0.0.0.0`, `9301` | Listen address. The port must differ from `http.port`, which stays read-only. |
| `operations` | `[backup, check]` | Operations the page may start: `backup`, `snapshots`, `check`, `forget`, `prune`. `forget` and `prune` delete data and must be listed explicitly; they must also be enabled on the job. `[]` makes the page read-only. |
| `username` | `admin` | User name for HTTP Basic authentication. Used only with `password_file`. |
| `password_file` | none | File holding the password. Unset: no authentication. Read at each request. |
| `history_size` | `50` | Finished and skipped runs kept in memory for the history table (1 to 1000). |

## `pushgateway`

| Setting | Default | Meaning |
|---|---|---|
| `url` | none | Pushgateway that Rustic pushes its own metrics to. Unset: Rustic pushes nothing. |
| `verify` | `true` | After each backup, ask the Pushgateway whether the push arrived. |
| `verify_timeout` | `10s` | Timeout of that check. |
| `cleanup_stale_groups` | `false` | Delete older metric groups of the same job whose grouping labels changed. |

## `mqtt`

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `host`, `port` | `localhost`, 1883 (8883 with TLS) | Broker address. |
| `username`, `password_file` | none | Credentials; the password is read from a file on every connection. |
| `client_id` | `rustic-scheduler` | |
| `topic_prefix` | `rustic-scheduler` | |
| `qos` | `1` | QoS of lifecycle and result messages. Progress always uses QoS 0. |
| `retain_results` | `true` | Retain the last result of each operation. |
| `progress_interval` | `10s` | Minimum spacing of progress messages. |
| `keepalive`, `connect_timeout` | `60s`, `10s` | |
| `max_reconnect_delay` | `60s` | Upper bound of the reconnect backoff. |
| `queue_size` | `1000` | In-memory messages kept while the broker is unreachable. |
| `tls.enabled` | `false` | |
| `tls.ca_file`, `tls.cert_file`, `tls.key_file` | none | CA bundle; client certificate and key. |
| `tls.insecure` | `false` | Skip certificate verification. Test brokers only. |
| `commands.enabled` | `false` | Start operations by MQTT message. See [mqtt.md](mqtt.md#commands). |
| `commands.operations` | `[backup, check]` | Operations a message may start: `backup`, `snapshots`, `check`, `forget`, `prune`. `forget` and `prune` delete data and must be listed explicitly; they must also be enabled on the job. |
| `homeassistant.enabled` | `false` | Publish Home Assistant MQTT discovery messages. See [mqtt.md](mqtt.md#home-assistant). |
| `homeassistant.discovery_prefix` | `homeassistant` | Home Assistant's discovery topic prefix. |
| `homeassistant.node_id` | `rustic_scheduler` | Namespace of the entities; letters, digits, `_`, `-`. Use a different one per scheduler. |
| `homeassistant.device_name` | `Rustic Scheduler` | Devices are named `<device_name> <job>`. |

## `notifications`

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | |
| `config_files` | `[]` | Apprise configuration files (TEXT or YAML), kept in mounted secrets. |
| `events` | `[backup_success, backup_failure, maintenance_failure]` | Also available: `backup_skipped`, `maintenance_success`. |
| `timeout` | `30s` | Per attempt. |
| `retries`, `retry_delay` | `2`, `15s` | |

## `jobs.<name>`

Job names may contain letters, digits, `_`, `.` and `-`. They appear in metric labels, MQTT
topics and file names.

| Setting | Default | Meaning |
|---|---|---|
| `profiles` | required | Rustic profiles, in order. A name is looked up as `<profile_dir>/<name>.toml`; absolute paths are allowed. |
| `description` | none | One line of free text for people, at most 300 characters. Shown on the web page, in `rustic-scheduler status` and in `/api/v1/status`. Not sent to Rustic, and not a metric label or MQTT topic. |
| `sources` | `[]` | Absolute paths passed to `rustic backup`. All of them go into **one** snapshot. Empty: Rustic uses the `[[backup.snapshots]]` sources of the profiles, one snapshot each. |
| `label` | job name | Snapshot label. It identifies the job's snapshots and scopes telemetry and forget. |
| `host` | none | Snapshot host name. When set it is passed to backups and also narrows the scope. Unset: Rustic uses the machine's host name. |
| `schedule` | none | When to run the backup. Without it the job only runs on request. |
| `missed_run_policy` | from `scheduler` | `catch_up` or `skip`. |
| `misfire_grace` | from `scheduler` | |
| `repository_wait_timeout` | `6h` | How long a run may wait for the repository. |
| `repository_lock` | automatic | Name of the lock to use instead of the repository identity Rustic reports. Give two jobs the same name to force them to run one at a time. |
| `allow_init` | `false` | Accept `init = true` in the profiles (Rustic may create the repository). |
| `env` | `{}` | Extra environment for this job's Rustic processes. Not for secrets. |

Label and host together must be unique among jobs.

### `schedule` (also under `snapshots`, `forget`, `prune`, `check`)

| Setting | Meaning |
|---|---|
| `days` | `daily`, or a list of `mon` `tue` `wed` `thu` `fri` `sat` `sun` (full names accepted). |
| `times` | One or more 24-hour `HH:MM` local times. |
| `timezone` | IANA name. Optional if `scheduler.timezone` is set. |

### `backup`

| Setting | Default | Meaning |
|---|---|---|
| `timeout` | `24h` | |
| `retries`, `retry_delay` | `0`, `5m` | Retries after a plain failure. Not after a timeout or an interruption. |
| `extra_args` | `[]` | Extra arguments for `rustic backup`, passed verbatim. |

### `snapshots`

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Collect telemetry with `rustic snapshots --json`. |
| `after_backup` | `true` | Collect after every backup of the job, whatever its outcome. |
| `schedule` | none | Additional collection times. |
| `timeout`, `retries`, `retry_delay` | `15m`, `0`, `1m` | |

### `forget`

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Remove snapshots of this job according to retention rules. |
| `keep` | `{}` | Rustic `keep-*` options, passed verbatim: `keep-daily: 7` becomes `--keep-daily 7`. |
| `rules_from_profile` | `false` | Use the `[forget]` rules of the profiles instead of `keep`. |
| `dry_run` | `false` | Preview only; reports what would be removed. |
| `max_remove` | none | Refuse to run if more snapshots than this would be removed. |
| `prune` | `false` | Use `forget --prune`: also delete unused repository data. |
| `after_backup` | `true` | Run after each succeeded backup. |
| `schedule` | none | Run at these times instead of, or in addition to, after backups. |
| `timeout`, `retries`, `retry_delay`, `extra_args` | `1h`, `0`, `5m`, `[]` | |

### `prune`

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Run `rustic prune` on a schedule. Acts on the whole repository. |
| `schedule` | required when enabled | Not required if `prune` is listed in `mqtt.commands.operations` or `web.operations`: it then runs on request only. |
| `dry_run` | `false` | |
| `timeout`, `retries`, `retry_delay`, `extra_args` | `12h`, `0`, `5m`, `[]` | |

### `check`

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Run `rustic check`. |
| `read_data` | `false` | Add `--read-data`: download and verify every pack file. |
| `read_data_subset` | none | Add `--read-data-subset`, for example `5%` or `1/52`. |
| `after_backup` | `false` | Run after each succeeded backup. |
| `schedule` | none | |
| `timeout`, `retries`, `retry_delay`, `extra_args` | `6h`, `0`, `5m`, `[]` | |

## Arguments that `extra_args` refuses

To keep scheduler-owned options and secrets out of command lines, these are rejected with
an explanation: `-P`/`--use-profile`, `--json-progress`, `--no-progress`, `--password`,
`--key`, `--prometheus-pass`, `--label`, `--filter-label`, `--host`, `--filter-host`,
`-n`/`--dry-run`; and per operation `--init` (backup), `--prune`, `--json`, `--keep-none`
(forget), `--read-data`, `--read-data-subset` (check). Each has a YAML setting instead.
