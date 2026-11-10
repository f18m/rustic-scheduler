# What was verified, and what was not

This page separates what was run and observed from what is documented on the strength of
other people's documentation. Versions used: Rustic 0.11.4, Pushgateway 1.11.4, Prometheus
3.16.0, Mosquitto 2.1.2, Versity S3 Gateway 1.10.0, Apprise 2.2.0, Python 3.13.

All of it ran on local, disposable data. No production system, no real backup repository and
no AWS account was touched.

## Not verified: AWS and Glacier

**Nothing was run against AWS.** The following is documented from AWS's, OpenDAL's and
Rustic's documentation only:

| Topic | Status |
|---|---|
| That S3 stores cold objects in `DEEP_ARCHIVE` and bills them as such | not tested |
| That archived objects cannot be read until restored | not tested |
| **Restoring from Glacier Flexible Retrieval or Deep Archive** (`RestoreObject`, Rustic's warm-up command and wait, the download afterwards) | **not tested; do not assume it works until you have done it** |
| Retrieval times (12h Standard, 48h Bulk) | from AWS documentation |
| Minimum storage durations and early-deletion charges | from AWS documentation |
| All prices and cost estimates | from AWS documentation; indicative only |
| The IAM policy in [aws-s3-glacier.md](aws-s3-glacier.md#permissions) | written from the API calls involved; never applied |
| `forget` and `prune` on archived objects, including `--keep-pack` and the cacheable-only repack default | not tested on archived data |
| `check --read-data` on archived data | not tested |
| Credential sources other than a shared credentials file with static keys (`AWS_PROFILE`, web identity, ECS, EC2 metadata, session tokens) | not tested |
| S3 behaviours a local server does not reproduce: eventual consistency, throttling, multipart upload limits, Object Lock, versioning | not tested |

What stands in for AWS in the tests is a local S3-compatible server. It proves what Rustic
*sends*; it proves nothing about what AWS does with it.

## Verified against the real Rustic 0.11.4 binary

By the integration tests in `tests/integration/` (39 tests), which CI runs with nothing
skipped.

**Rustic facts the design relies on**

- `--json-progress` writes `status` and `summary` objects, one per line, to stdout; logs go
  to stderr. The exact field sets are asserted.
- `percent_done` is a fraction; `seconds_remaining` is omitted when unknown.
- The summary is printed for every successful backup, without `snapshot_id` when no snapshot
  was saved (`--skip-if-unchanged`).
- Rustic exits with status **0** when stopped by SIGTERM mid-backup, and writes no snapshot.
- Several `-P` profiles given as absolute paths are merged by Rustic; the first listed wins
  for a value set in more than one.
- `rustic show-config` returns the merged configuration and reflects the environment.
- A command-line `--label` or `--tag` replaces the profile's value.
- `--filter-label` on `snapshots` and `forget` restricts them to that label.
- Missing source, wrong password, missing repository and invalid profile all exit non-zero.
- `check --read-data` exits non-zero on a damaged pack file.

**Scheduler behaviour with real Rustic**

- Backup with live progress and no MQTT; several sources in one snapshot; sources defined in
  a profile giving one snapshot each.
- Three real TOML profiles combined: repository from one, tags and globs from another,
  `exclude-if-present` from a third, all effective in the same snapshot; conflict resolution
  by order.
- Conflicting or invalid profiles refused before anything runs, with no secret in the error.
- Snapshot telemetry counted separately for two jobs sharing a repository.
- Forget removing only its own job's snapshots while another job's stay; forget off by
  default; dry run removing nothing; rules taken from a profile; a profile's `prune = true`
  refused unless enabled.
- `forget --prune` and a separate `prune` on throwaway data, followed by a passing
  `check --read-data` and a byte-exact restore.
- Check passing, then failing on a deliberately damaged pack, and forget being withheld.
- A real backup interrupted by shutdown reported as `interrupted`, with no snapshot left.
- Two jobs on one repository running strictly one after the other.
- The daemon as a process: endpoints, manual trigger over the control socket, refusal of a
  disabled forget, `status`, the `rustic` helper, SIGTERM exit status 0, last results
  restored after restart, refusal to start on a bad configuration.

**Pushgateway and Prometheus** (real containers)

- Rustic pushes its metrics; they are scraped from the Pushgateway and validated by name,
  value and grouping labels (`job`, `snapshot_label`, `hostname`, `paths`; no `tags` when
  empty).
- Delivery verified by the scheduler; a stopped Pushgateway gives a succeeded backup, a
  logged push error and `push_verified` 0; a restarted, empty Pushgateway is refilled by the
  next backup.
- Rustic pushes even when a backup saves no snapshot.
- A changed source path leaves a stale group, removed only with `cleanup_stale_groups`.
- A profile's own Pushgateway setting and `metrics-job` are left untouched.
- A real Prometheus with the example scrape configuration and alert rules collects both
  endpoints; `honor_labels` keeps Rustic's `job`; the scheduler's `backup_job` label does not
  clash with Prometheus's `job`.

**MQTT** (real Mosquitto with password authentication)

- Lifecycle events, progress, results and telemetry on the documented topics; retained
  results and status; `offline` on a clean stop.
- Wrong password: no connection, backup unaffected.
- Reconnection after the broker is stopped and started.
- TLS: connects with the right CA; refuses the same broker without it.

**Apprise** (real library, local HTTP receiver)

- Success and failure notifications delivered with the documented content.
- An unreachable target: backup still `succeeded`, failure counted, token absent from logs.

**S3** (real Rustic against a local S3 server)

- Hot/cold repository through `opendal:s3`: backup, telemetry and check; which object kinds
  land in the hot and cold prefixes.
- Credentials from `AWS_SHARED_CREDENTIALS_FILE` only; clean failure without them.
- `x-amz-storage-class: DEEP_ARCHIVE` sent for every cold upload and for no hot upload.
- Restore from that repository; the value of `%path` given to a warm-up command.

## Verified with a scripted stand-in for Rustic

The unit tests in `tests/unit/` (188 tests) cover logic that needs precise control over
timing and failure: configuration validation and error messages; schedule arithmetic across
time zones and both DST transitions; command construction; profile conflict analysis; the
process runner (timeouts, SIGKILL escalation, process groups, long lines); every outcome and
reason; retries; after-backup ordering and its skip rules; all forget safeguards; overlap,
queueing, lock time-outs and the external lock file; shutdown; crash recovery and state
restore; on-time, late, missed and collapsed runs with a controllable clock; catch-up
evidence rules; metric label bounds and unknown-as-absent; notification selection, retry and
time-out; Pushgateway verification results; health and readiness following the loop; the
network port being unable to start jobs; MQTT queueing without a broker; secret masking.

The stand-in's canned `summary` is checked against the real binary's field set by an
integration test, so the two cannot drift apart unnoticed.

## Verified on the built image

`tests/e2e/run.sh` builds the image and runs it with the Pushgateway sidecar and Prometheus:

- runs as UID 1000 on a read-only root filesystem; the scheduler process has no effective
  capability and cannot read a root-only file;
- a backup includes a file and a directory only root can read, through the capability on the
  Rustic binary;
- the container health check, `validate`, `trigger --wait`, the `rustic` helper;
- backup, check and telemetry outcomes and the push verification in `/metrics`;
- Prometheus, using the example configuration, has both metric families and both targets up;
- pushed metrics survive a Pushgateway restart through its persistence file;
- `docker stop` ends the scheduler with status 0, and results are restored after restart;
- the repository password does not appear in the logs.

The image was built for `linux/arm64` (and tested as above) and for `linux/amd64` (built
under emulation; `rustic --version` and `rustic-scheduler --version` run). The end-to-end
script was not run on amd64 hardware.

## Other things not verified

- A long-running deployment: real schedules firing over days, real DST transitions, real
  host suspend and clock steps. These are covered by unit tests with a controllable clock.
- Large repositories: memory and time for snapshot listings with thousands of snapshots.
- The GitHub Actions workflow itself has not been run; its steps are the same commands that
  were run locally.
- MQTT client certificates; MQTT brokers other than Mosquitto.
- Apprise targets other than the JSON webhook; each service has its own failure modes.
- Pushgateway behind basic authentication.
- Rustic backends other than local and S3.
- Windows and macOS as runtime platforms. Development and the tests ran on macOS; the
  container targets Linux.
