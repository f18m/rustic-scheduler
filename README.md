# rustic-scheduler

A small, Docker-friendly service that runs [Rustic](https://github.com/rustic-rs/rustic)
backups on a schedule and tells you, truthfully, what happened.

- Several named backup jobs in one YAML file: source folders, weekdays, times, timezone.
- Your own Rustic TOML profiles, used as they are. Rustic merges them; the scheduler never
  parses, rewrites or restricts them.
- S3 hot/cold repositories, including cold objects in S3 Glacier Deep Archive.
- Live progress and results on a Prometheus `/metrics` endpoint.
- Rustic's own completed-backup metrics through a Prometheus Pushgateway sidecar, with
  delivery verified rather than assumed.
- Optional MQTT events, optional [Apprise](https://github.com/caronc/apprise) notifications.
- Optional MQTT commands to start a backup, check or forget, and optional Home Assistant
  MQTT discovery (sensors and "run" buttons per job).
- Optional web interface: running operations with progress, recent runs, "Run now" buttons
  and ready-to-copy commands for using Rustic directly.
- Optional snapshot telemetry, retention (`forget`), `prune` and `check`.
- No database. History lives in Rustic's snapshots and in Prometheus.

Rustic runs as an external process: the pinned release is **Rustic 0.11.4**, unmodified.

> **Status.** Everything described here was exercised against the real Rustic 0.11.4 binary,
> Pushgateway 1.11.4, Prometheus 3.16.0 and Mosquitto 2.1.2 on local, disposable data.
> **Nothing was run against AWS.** Behaviour of real S3 Glacier storage classes, retrievals,
> restores and costs is documented from AWS's and Rustic's documentation and is unverified.
> See [docs/verification.md](docs/verification.md).

## What is on by default, and what can delete data

| Operation | Default | Effect on the repository |
|---|---|---|
| `backup` | on for every job | Adds data and snapshots. Never deletes. |
| `snapshots` telemetry | **off** | Read-only (`rustic snapshots --json`). |
| `check` | **off** | Read-only. With `read_data: true` it downloads pack files (cost and delay on cold storage). |
| `forget` | **off** | **Deletes snapshots** of this job. Needs explicit `keep` rules. |
| `forget.prune` | **off** | **Deletes repository data** no snapshot uses any more (`forget --prune`). |
| `prune` | **off** | **Deletes repository data**; acts on the whole repository, not one job. |
| Repository creation (`init`) | **never** unless `allow_init: true` | Creates a repository. |
| Pushgateway group cleanup | **off** | Deletes stale metric groups in the Pushgateway only. |
| MQTT, notifications | **off** | None. |
| MQTT commands | **off** | Lets MQTT messages start `backup` and `check`; `forget` and `prune` only if listed, and only if enabled on the job. |
| Home Assistant discovery | **off** | None. |
| Web interface | **off** | Lets its "Run now" buttons start `backup` and `check`; `forget` and `prune` only if listed, and only if enabled on the job. |

Nothing that deletes anything runs unless you turn it on in the YAML. Destructive options
found only in a Rustic profile (for example `[forget] prune = true`) are refused at start-up
until the YAML agrees. A manual request for `forget` or `prune` on a job where it is not
enabled is refused too.

## Quick start

Requirements: Docker with Compose. Supported image platforms: `linux/amd64`, `linux/arm64`.

```sh
git clone https://github.com/f18m/rustic-scheduler && cd rustic-scheduler

# 1. Secrets: copy the placeholders and fill them in (see examples/secrets/README.md)
cd examples/secrets
for f in *.example; do cp -n "$f" "${f%.example}"; done
chmod 600 rustic_password aws_credentials mqtt_password apprise.conf
sudo chown 1000:1000 rustic_password aws_credentials mqtt_password apprise.conf
cd ../..

# 2. Edit examples/config.yaml and examples/profiles/*.toml (bucket, region, paths),
#    and point the source mounts in compose.yaml at your data.

# 3. Check the configuration with the real Rustic binary, without starting anything
docker compose run --rm rustic-scheduler validate

# 4. Start the scheduler and the Pushgateway
docker compose up -d
docker compose exec rustic-scheduler rustic-scheduler status
```

The scheduler never creates a repository on its own. Create it once, by hand:

```sh
docker compose run --rm --entrypoint rustic rustic-scheduler \
  -P /etc/rustic-scheduler/profiles/s3-deep-archive.toml init
```

Run a job now instead of waiting for its schedule, and wait for the real result:

```sh
docker compose exec rustic-scheduler rustic-scheduler trigger documents --wait
```

To try it without AWS, use `examples/profiles/local-test.toml` as the job's profile.

## Configuration in one screen

```yaml
scheduler:
  timezone: Europe/Rome          # default for schedules

pushgateway:
  url: http://pushgateway:9091   # where Rustic pushes its own metrics

jobs:
  documents:
    profiles: [s3-deep-archive, documents]   # Rustic TOML profiles, in order
    sources: [/data/documents]               # one or more folders, one snapshot
    schedule:
      days: [mon, wed, fri]                  # or `daily`
      times: ["02:30", "14:30"]
      timezone: Europe/Rome                  # optional per schedule
    snapshots: {enabled: true}               # telemetry after each backup
    forget:
      enabled: true
      dry_run: true                          # preview until you remove this
      keep: {keep-daily: 14, keep-weekly: 8}
```

- Full reference: [docs/configuration.md](docs/configuration.md)
- Complete example: [examples/config.yaml](examples/config.yaml), profiles in
  [examples/profiles/](examples/profiles/)

Configuration is validated before any work is scheduled, using the real Rustic binary to
load your profiles. Every problem is reported at once, with its location and what to do:

```
error: jobs.documents.schedule.times: cannot read '2:30' as a time; use 24-hour HH:MM such as 02:30
error: jobs.media: the profiles set [backup] label = 'pics' but the scheduler would pass 'media'
       and override it. Remove it from the profile or set `label: pics` on the job.
```

## How it behaves

The short version; details and rationale are in [docs/behaviour.md](docs/behaviour.md).

| Situation | Behaviour |
|---|---|
| Two jobs use the same repository | They run one after the other, in arrival order. Never concurrently. |
| Jobs on different repositories | Run in parallel. |
| A job is still running at its next scheduled time | The new run is **skipped** and counted. Runs never pile up. |
| A run waits too long for a busy repository | Skipped after `repository_wait_timeout` (default 6h). |
| Scheduler was down at a scheduled time | Per job: `catch_up` (default) runs once at start-up if the run is provably missing and within `misfire_grace`; `skip` waits for the next time. |
| Host suspended, clock jumped forward | All passed occurrences collapse into at most one run. |
| Clock moved backwards | Nothing runs twice. |
| Local time that does not exist (spring forward) | Runs once, shifted by the gap (02:30 becomes 03:30). |
| Local time that occurs twice (fall back) | Runs once, at the first occurrence. |
| Timeout reached | Rustic gets SIGTERM, then SIGKILL after `stop_grace`. Outcome `failed`. |
| Container stops | Running Rustic is stopped the same way. Outcome `interrupted`. Queued work is dropped. |
| Scheduler crashes mid-run | After restart the run is reported as `unknown`, never as success. |
| Manual run (CLI, or MQTT message or web interface if enabled) | Queues like a scheduled run; refused if that job is already queued or running. |

### Outcomes

Every operation ends in exactly one of five states:

| Outcome | Meaning |
|---|---|
| `succeeded` | Rustic exited 0, was not interrupted and, for a backup, reported its summary. |
| `failed` | Rustic reported an error, timed out, was killed, or could not be started. |
| `skipped` | The run never started. |
| `interrupted` | The run started and was stopped by shutdown or a signal. |
| `unknown` | The run started but its result cannot be established. |

Queued or running is never reported as a result. Rustic's exit status alone is never trusted:
Rustic 0.11.4 exits with status **0** when it is interrupted by SIGTERM or SIGINT. Telemetry,
MQTT, notification and Pushgateway delivery results are tracked separately and never change a
Rustic outcome.

### After a backup

If enabled with `after_backup`, in this order: `check`, then `forget`, then `snapshots`.
`check` and `forget` only run after a succeeded backup, and `forget` does not run if that
check failed. Snapshot telemetry always runs. The repository stays locked for the whole
sequence.

## Observability

Prometheus scrapes **two endpoints**. A single combined endpoint is not provided.

| Endpoint | Content |
|---|---|
| `rustic-scheduler:9300/metrics` | Scheduler state, live progress, results, snapshot telemetry, delivery health (`rustic_scheduler_*`) |
| `pushgateway:9091/metrics` | Rustic's own completed-backup metrics (`rustic_backup_*`), with `honor_labels: true` |

A working scrape configuration and alert rules are in
[examples/prometheus/](examples/prometheus/). Metric reference, Pushgateway retention and
clean-up: [docs/metrics.md](docs/metrics.md).

Other endpoints on port 9300: `/healthz` and `/readyz` (both follow the scheduling loop's
heartbeat, not merely "the port answers") and `/api/v1/status` (JSON with full diagnostics).

- MQTT topics, payloads, commands and Home Assistant: [docs/mqtt.md](docs/mqtt.md)
- Web interface (`web.enabled`, port 9301): [docs/web.md](docs/web.md)
- Notifications: [docs/notifications.md](docs/notifications.md)

## Working on the repository yourself

The repository is an ordinary Rustic repository; nothing about it is scheduler-specific.
Use Rustic directly, from anywhere, with your own profiles. From inside the container, this
helper adds a job's profiles and environment for you:

```sh
docker compose exec rustic-scheduler rustic-scheduler rustic documents -- snapshots
docker compose exec rustic-scheduler rustic-scheduler rustic documents -- ls latest
docker compose exec rustic-scheduler rustic-scheduler forget-preview documents   # changes nothing
```

For commands that change the repository (`forget`, `prune`, `repair`, ...) the helper first
waits for the scheduler's repository lock, so it never overlaps a scheduled operation.

The optional [web interface](docs/web.md) shows these commands filled in for each job.

## Commands

```
rustic-scheduler [-c CONFIG] COMMAND
  run                      run the scheduler (container default)
  validate [--offline]     check the configuration and exit
  status [--json]          state of the running scheduler
  trigger JOB [OPERATION] [--wait]   run backup (default), snapshots, check, forget or prune now
  show-command JOB [OPERATION]       print the exact Rustic command line
  forget-preview JOB       list what forget would remove; changes nothing
  rustic JOB -- ARGS...    run Rustic by hand with the job's profiles
  healthcheck              exit 0 if the scheduler is ready
```

## Documentation

| Topic | File |
|---|---|
| Every YAML setting | [docs/configuration.md](docs/configuration.md) |
| Scheduling, concurrency, outcomes, recovery | [docs/behaviour.md](docs/behaviour.md) |
| Rustic profiles, precedence, options the scheduler adds | [docs/rustic-integration.md](docs/rustic-integration.md) |
| Metrics, Pushgateway, Prometheus | [docs/metrics.md](docs/metrics.md) |
| MQTT, MQTT commands, Home Assistant discovery | [docs/mqtt.md](docs/mqtt.md) |
| Web interface | [docs/web.md](docs/web.md) |
| Notifications | [docs/notifications.md](docs/notifications.md) |
| AWS S3, Glacier Deep Archive, permissions, costs | [docs/aws-s3-glacier.md](docs/aws-s3-glacier.md) |
| Retention, prune and check | [docs/maintenance.md](docs/maintenance.md) |
| Restore walkthrough | [docs/restore.md](docs/restore.md) |
| Secrets, container hardening | [docs/security.md](docs/security.md) |
| Deployment, upgrades, troubleshooting | [docs/operations.md](docs/operations.md) |
| What was verified, and what was not | [docs/verification.md](docs/verification.md) |

## Development

```sh
just setup        # Python dependencies (uv) and the pinned Rustic binary in .tools/
just lint         # ruff format, ruff check, mypy --strict
just test         # unit + integration tests (real Rustic; Docker for the service tests)
just e2e          # build the image, test it with Pushgateway and Prometheus
```

Tests need no AWS account, no production credentials and no real backup data.

## License

MIT. See [LICENSE](LICENSE).
