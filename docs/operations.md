# Deployment and operations

## Image

```sh
docker build -f docker/Dockerfile -t rustic-scheduler:0.1.0 .
docker buildx build --platform linux/amd64,linux/arm64 -f docker/Dockerfile -t rustic-scheduler:0.1.0 .
```

- Platforms: `linux/amd64` and `linux/arm64`. Rustic also publishes `armv7` and `i686`
  builds, but they are not wired into the Dockerfile.
- CI publishes `ghcr.io/<owner>/rustic-scheduler` on pushes to `main`, tagged `main`,
  `latest`, and `sha-<commit>`, and on published GitHub releases using the release's Git tag.
  Pull requests build and test the image without publishing it.
- Reproducibility: base and uv images pinned by digest; Rustic 0.11.4 pinned by SHA-256;
  Python dependencies installed from `uv.lock` with `--frozen`. The same sources give the
  same dependency set. Image bytes are not bit-for-bit identical between builds (Debian
  packages are installed at build time from the pinned base's package lists).
- Runs as UID 1000. Entry point `rustic-scheduler`, default command `run`.

## Compose

[compose.yaml](../compose.yaml) starts the scheduler and the Pushgateway. Optional profiles
add an example Prometheus (`--profile monitoring`) and Mosquitto (`--profile mqtt`).

| Mount | Purpose | Mode |
|---|---|---|
| `/etc/rustic-scheduler/config.yaml` | scheduler configuration | read-only |
| `/etc/rustic-scheduler/profiles/` | Rustic TOML profiles | read-only |
| `/data/...` | data to back up | **read-only** |
| `/run/secrets/...` | secrets | read-only |
| `/var/lib/rustic-scheduler` | state: last results, run logs, locks, control socket | volume |
| `/var/cache/rustic` | Rustic's metadata cache | volume |
| `/pushgateway` (Pushgateway) | persisted pushed metrics | volume |

Things to set for your deployment:

- `hostname:` Rustic stores the host name in each snapshot and groups snapshots by it for
  retention and for finding the parent snapshot. A container's default host name changes when
  it is recreated; fix it, or set `host:` on each job.
- `stop_grace_period:` longer than `scheduler.stop_grace`.
- the source mounts, read-only.
- `user:` and `cap_add:` as explained in [security.md](security.md#container-hardening).

### What needs to persist

| Data | If lost |
|---|---|
| The Rustic repository and its password | The backups are gone. This is the only thing that truly matters. |
| State volume | Last results and run logs disappear; operations report no outcome until they next run; catch-up after downtime falls back to Rustic's snapshot list. Nothing else. |
| Cache volume | Rustic downloads metadata again from the hot part. Slower first run. |
| Pushgateway volume | Rustic's pushed metrics are missing until each job's next backup. |

No database is involved. The scheduler can be redeployed from its configuration alone.

## Health and readiness

| Check | Passes when |
|---|---|
| `GET /healthz` | The scheduling loop iterated within `http.max_heartbeat_age` (90s), or the scheduler is shutting down cleanly |
| `GET /readyz` | The configuration was validated with Rustic, the loop is running and recent, and the scheduler is not shutting down |
| `rustic-scheduler healthcheck` | `/readyz` returns 200. Used by the image's `HEALTHCHECK`. |
| `rustic_scheduler_ready` | Same condition, as a metric |

An HTTP server that answers is not treated as a working scheduler: all of these follow the
loop's heartbeat. A scheduler whose loop has stopped answers `/metrics` but reports
`rustic_scheduler_ready 0` and fails both probes. The scheduler does not start its HTTP
server at all if the configuration is invalid; it logs every problem and exits with status 2.

Readiness says the scheduler can launch work. It does not say backups succeed; alert on
outcomes for that ([examples/prometheus/alerts.yml](../examples/prometheus/alerts.yml)).

## Day to day

```sh
docker compose exec rustic-scheduler rustic-scheduler status            # what ran, what is next
docker compose exec rustic-scheduler rustic-scheduler trigger documents --wait
docker compose exec rustic-scheduler rustic-scheduler show-command documents forget
docker compose exec rustic-scheduler rustic-scheduler forget-preview documents
docker compose logs -f rustic-scheduler
```

Changing the configuration or a profile: edit, `docker compose run --rm rustic-scheduler
validate`, then `docker compose restart rustic-scheduler`. A restart while a backup runs
interrupts it; check `status` first.

### Diagnosing a failure

1. `rustic-scheduler status`: outcome, reason, Rustic's exit status and last error lines.
2. The run log named there (`/var/lib/rustic-scheduler/logs/<job>/...`): Rustic's complete
   output of that run.
3. `show-command JOB OPERATION`, then run it by hand with
   `rustic-scheduler rustic JOB -- ...`.

| Symptom | Likely cause |
|---|---|
| `failed` / `exit_code`, "signing http request" | AWS credentials missing or unreadable by UID 1000 |
| `failed` / `exit_code`, permission denied on a source file | No `DAC_READ_SEARCH` capability and the file is not readable by the container user |
| `failed` / `spawn_error`, "Operation not permitted" | `rustic.binary` points at the capability copy but the container lacks `cap_add: [DAC_READ_SEARCH]`, or `no-new-privileges` is set |
| `unknown` / `no_summary` | Rustic exited 0 without a summary: it was interrupted from outside, or a profile changes its output |
| `skipped` / `repository_busy` | Another operation held the repository longer than `repository_wait_timeout` |
| `skipped` / `already_running` | The previous run of that job had not finished |
| `failed` / `scope_violation` | A profile filter changes which snapshots Rustic returns for the job |
| `pushgateway_push_verified` is 0 | Pushgateway unreachable from the container, or the profiles change the grouping labels |
| Start-up error about a label | A profile sets `label`; remove it or set the same `label` on the job |

## Upgrading

**rustic-scheduler.** Read the release notes, build the new image, run `validate` with it,
then recreate the container. State files are versioned and tolerant: an unreadable state file
is ignored with a warning.

**Rustic.** The scheduler depends on details of one Rustic release: JSON progress fields,
profile merge order, exit statuses, the interruption behaviour, the Pushgateway grouping key.
To move to another release:

1. Put the new version and checksums in `docker/Dockerfile` and `scripts/install-rustic.sh`,
   and the version in `src/rustic_scheduler/__init__.py`.
2. Run `just test-strict` and `just e2e`. The integration tests check those details against
   the real binary and fail if one changed.
3. Read Rustic's changelog for repository format changes. Rustic may upgrade repository
   metadata on write; an older Rustic may then be unable to read it. Never run two Rustic
   versions against one repository without checking this.

Running with a different Rustic than the pinned one only produces a warning at start-up.

**Pushgateway, Prometheus, Mosquitto.** Independent of the scheduler; follow their own notes.
The scheduler uses only the Pushgateway's `/api/v1/metrics` and delete endpoints.

## Development and CI

```sh
just setup         # uv sync + pinned Rustic into .tools/
just lint          # ruff format --check, ruff check, mypy --strict
just test          # unit + integration (skips what needs a missing Rustic or Docker)
just test-strict   # same, but missing prerequisites are errors
just e2e           # build the image; test it with Pushgateway and Prometheus
```

`.github/workflows/ci.yml` runs lint, the full test suite on Python 3.12 and 3.13 with the
real Rustic binary and the service containers, the image end-to-end test, and a build for
both architectures. Pushes to `main` and published GitHub releases publish that
multi-architecture image to GHCR.
