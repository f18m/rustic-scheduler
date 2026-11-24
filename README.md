[![GitHub Tag](https://img.shields.io/github/v/tag/f18m/rustic-scheduler)](https://github.com/f18m/rustic-scheduler/releases) ![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/f18m/rustic-scheduler/ci.yml)

# <img src="./src/rustic_scheduler/webui/favicon.svg" width="32" height="32" alt="favicon" /> rustic-scheduler

<div align="center">

⭐ If you find this App useful, please star this repo - it helps others discover the project! ⭐

</div>

A small, Docker-friendly service that runs [Rustic](https://github.com/rustic-rs/rustic)
backups on a schedule and tells you, truthfully, what happened.

- Several named backup jobs in one YAML file: source folders, weekdays, times, timezone.
- Your own Rustic TOML profiles, used as they are. Rustic merges them; the scheduler never
  parses, rewrites or restricts them.
- S3 hot/cold repositories, including cold objects in S3 Glacier Deep Archive.
- Live progress and results on a Prometheus `/metrics` endpoint.
- Rustic's own completed-backup metrics through a Prometheus Pushgateway sidecar, with
  delivery verified rather than assumed.
- Optional MQTT notifications for backup start/progress/completion, Snapshot check and forget operations, etc (more than 20 entities provided).
- Optional MQTT commands to start a backup, check or forget, and optional Home Assistant MQTT discovery (sensors and "run" buttons per job).
- Optional [Apprise](https://github.com/caronc/apprise) notifications.
- Optional web interface: running operations with progress, recent and next runs, "Run now" buttons,
  a terminal-like window following Rustic's output of each run, ready-to-copy commands for using Rustic directly and, if enabled, a read-only browser for
  each job's snapshots and the files in them.
- Optional snapshot telemetry, retention (`forget`), `prune` and `check`.
- No database. History lives in Rustic's snapshots (and in Prometheus if you use it).

Rustic runs as an external process: the pinned release is **Rustic 0.11.4**, unmodified.


## 💡 Why?

I use Rustic to backup my NAS to AWS S3 Cold Storage. 
Initially I used ad-hoc Bash scripts running on my NAS to do that; they did the job but these 
"quick-and-dirty" scripts were lacking:

* proper telemetry/observability: backup has completed? when? successfully? 
* notification support: via Telegram, email, etc
* flexibility: scheduling during the day/week, extra operations ("check", "forget", etc)

This is why this project was born: make it easy to run backups with Rustic, with no compromise on flexibility and observability.


## 🚀 Quick start

Assumes you've read the [Rustic Getting Started](https://rustic.cli.rs/docs/getting_started.html). We'll schedule the same `backup` and `forget` operations from that guide.

**Requirements:** Docker with Compose. Supported platforms: `linux/amd64`, `linux/arm64`.

```sh
git clone https://github.com/f18m/rustic-scheduler && cd rustic-scheduler/examples

# 1. Create a Rustic profile matching your repository (like Getting Started)
cat > profiles/myrepo.toml << 'EOF'
[repository]
repository = "/tmp/repo"              # or S3 path like "s3:bucket/path"
password-file = "/run/secrets/rustic_password"
EOF

# Note: The password-file is loaded from Docker Compose secrets (secrets/rustic_password.example)

# 2. Create a minimal configuration for rustic-scheduler
cat > config.yaml << 'EOF'
scheduler:
  timezone: Europe/Rome

web:
  enabled: true
  port: 9301
  operations: [backup, check]

jobs:
  mydata:
    profiles: [myrepo]
    sources: [/data/src]              # your data folder (see compose.yaml)
    schedule:
      days: daily
      times: ["02:30"]
    forget:
      enabled: true
      dry_run: false
      keep:
        keep-daily: 14
        keep-weekly: 8
EOF

# 3. Create the repository (one time only)
docker compose run --rm --entrypoint rustic sched -P /etc/rustic-scheduler/profiles/myrepo.toml init

# 4. Start the scheduler
docker compose up -d
docker compose exec sched rustic-scheduler status

# 5. Run a backup now (optional)
docker compose exec sched rustic-scheduler trigger mydata --wait
```

**Open the web interface:** Once the scheduler is running, open `http://localhost:9301` in your browser to see backups, run operations, and browse snapshots.

**To use your own data:** Edit `docker-compose.yaml` and change the `DATA_DIR` environment variable (or the left side of the mount path) to point to your actual folder. The right side must stay `/data/src` to match the configuration.

## 📋 Minimal configuration example

```yaml
scheduler:
  timezone: Europe/Rome              # timezone for all schedules

jobs:
  mydata:
    profiles: [myrepo]               # your Rustic TOML profile
    sources: [/data/src]             # folder to backup
    schedule:
      days: daily                    # or [mon, wed, fri]
      times: ["02:30"]
    forget:
      enabled: true                  # optional: clean up old snapshots
      keep:
        keep-daily: 14               # keep daily snapshots for 14 days
        keep-weekly: 8               # keep weekly snapshots for 8 weeks
```

That's it. See [docs/configuration.md](docs/configuration.md) for all available options (additional backup timeout, snapshot telemetry, check operations, notifications, etc).

## 🔭 What to do next

Your backups are running on schedule. Here's what you can do:

**Check status:**
```sh
cd examples
docker compose exec sched rustic-scheduler status
docker compose logs -f sched
```

**View Rustic snapshots and operations:**
```sh
cd examples
docker compose exec sched rustic-scheduler rustic mydata -- snapshots
docker compose exec sched rustic-scheduler rustic mydata -- ls latest
docker compose exec sched rustic-scheduler forget-preview mydata
```

**Advanced setup** (optional features):
- **Prometheus metrics:** Uncomment `pushgateway` in `docker-compose.yaml` and configure Prometheus scraping (see [examples/prometheus/](examples/prometheus/))
- **Web interface:** Set `web.enabled: true` in config.yaml and open `http://localhost:9301`
- **MQTT notifications:** Set `mqtt.enabled: true` and connect Home Assistant or other clients
- **Email/Telegram/etc:** Set `notifications.enabled: true` and configure Apprise (see [docs/notifications.md](docs/notifications.md))

**Learn more** about all options in [docs/configuration.md](docs/configuration.md).

## 📋 Safety: what can delete data

By default, **only `backup` runs**. It never deletes anything from your repository.

`forget` is **disabled by default**. When you enable it, it will only delete snapshots according to your `keep` rules. It **does not** delete the data itself (`prune`) unless you explicitly turn that on.

Nothing destructive runs unless you explicitly enable it in the configuration YAML.


## 💻 Rustic CLI: direct use

You can use Rustic directly from anywhere with your own profiles and credentials. For convenience, the scheduler provides helpers that load your job's profiles and environment:

```sh
cd examples

# List snapshots
docker compose exec sched rustic-scheduler rustic mydata -- snapshots

# Browse latest snapshot
docker compose exec sched rustic-scheduler rustic mydata -- ls latest

# See what forget would remove (no changes)
docker compose exec sched rustic-scheduler forget-preview mydata
```

Your repository is a standard Rustic repository; nothing about it is scheduler-specific.

## 💻 Commands

```
rustic-scheduler [-c CONFIG] COMMAND
  run                      run the scheduler (container default)
  status [--json]          show scheduler state and next scheduled run
  trigger JOB [--wait]     run a backup now (use rustic-scheduler trigger -h for all operations)
  validate                 check the configuration and exit
```

See all available commands and operations:

```sh
cd examples
docker compose exec rustic-scheduler rustic-scheduler --help
docker compose exec rustic-scheduler rustic-scheduler trigger --help
```

## 📝 Documentation

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

## 🛠️ Development

```sh
just setup        # Python dependencies (uv) and the pinned Rustic binary in .tools/
just lint         # ruff format, ruff check, mypy --strict
just test         # unit + integration tests (real Rustic; Docker for the service tests)
just e2e          # build the image, test it with Pushgateway and Prometheus
```

Tests need no AWS account, no production credentials and no real backup data.


## Alternatives

* The [Backrest](https://github.com/garethgeorge/backrest) project seems a great option for the Restic backup tool, but does not support Rustic as of today. There is an [open discussion](https://github.com/rustic-rs/rustic/discussions/1160) on that.

* The [rustic_scheduler](https://rustic.cli.rs/ecosystem/rustic-scheduler/) offered by Rustic developer team is a different thing compared to this project: it's meant to simplify the backup of many clients connected to a centralized server.

## License

MIT. See [LICENSE](LICENSE).
