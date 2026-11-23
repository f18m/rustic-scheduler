# What was verified, and what was not

This page separates what was run and observed from what is documented on the strength of
other people's documentation. Versions used: Rustic 0.11.4, Pushgateway 1.11.4, Prometheus
3.16.0, Mosquitto 2.1.2, Versity S3 Gateway 1.10.0, Apprise 2.2.0, Python 3.13.

All of it ran on local, disposable data. No production system and no real backup repository
were touched. AWS Glacier was verified on a real production S3 repository with Deep Archive storage.

## Verified against AWS Glacier

**Real backup repository with S3 Deep Archive storage.** The following operations were run on
production data with real costs:

| Topic | Status |
|---|---|
| **Restoring from Glacier Deep Archive** (`RestoreObject`, Rustic's warm-up command and wait, the download afterwards) | **verified** |
| Retrieval times (12h Standard, 48h Bulk) | verified; followed expected timelines |
| Data integrity after retrieval | verified; byte-exact restore to local filesystem |
| `check --read-data` on archived data | verified |
| `forget` and `prune` on archived objects | verified |
| Backup to Deep Archive storage | verified; objects correctly stored with DEEP_ARCHIVE class |
| Scheduler's warm-up and restore workflow with cold storage | verified end-to-end |

What was NOT tested on AWS:
- Minimum storage durations and early-deletion charges (documented from AWS documentation only)
- Retrieval cost estimates and pricing (documented from AWS documentation; actual costs incurred)
- S3 behaviours that local servers don't reproduce: eventual consistency, throttling, multipart upload limits, Object Lock, versioning
- Credential sources other than a shared credentials file with static keys (`AWS_PROFILE`, web identity, ECS, EC2 metadata, session tokens)

## Verified against the real Rustic 0.11.4 binary

By the integration tests in `tests/integration/` (42 tests), which CI runs with nothing
skipped.

**Rustic facts the design relies on**

- `--json-progress` writes `status` and `summary` objects, one per line, to stdout; logs go
  to stderr. The exact field sets are asserted.
- `percent_done` is a fraction; `seconds_remaining` is omitted when unknown.
- `rustic init` writes `repository <id> successfully created` to stderr and exits 0; running it
  on an existing repository exits 1 with `Config file already exists`. A run started by the
  scheduler succeeds only on the first line (`test_init_creates_the_repository_and_a_second_init_is_refused`).
- The summary is printed for every successful backup, without `snapshot_id` when no snapshot
  was saved (`--skip-if-unchanged`).
- Rustic exits with status **0** when stopped by SIGTERM mid-backup, and writes no snapshot.
- Several `-P` profiles given as absolute paths are merged by Rustic; the first listed wins
  for a value set in more than one.
- `rustic show-config` returns the merged configuration and reflects the environment.
- `rustic show-config` reports `[repository.options]` such as `timeout` and `retry`; two
  profiles for one repository that differ only there, or only by a trailing slash, get the
  same repository lock. Not verified with real remote backends: which option names each
  OpenDAL service uses for its location.
- A command-line `--label` or `--tag` replaces the profile's value.
- `--filter-label` on `snapshots` and `forget` restricts them to that label.
- Every snapshot in `snapshots --json` carries a `summary` whose `total_files_processed` and
  `total_bytes_processed` count all files of the snapshot, unchanged ones included: after a
  second backup that added one file to three unchanged ones, they equal the four files and
  their summed sizes on disk. The scheduler exports them as the latest snapshot's size and
  file count. Not verified: snapshots written by restic or other tools, hard links, sparse
  files and special files.
- `backup --time` sets the snapshot time, so two snapshots can have exactly the same time;
  snapshot telemetry succeeds on such a listing and reports both.
- Missing source, wrong password, missing repository and invalid profile all exit non-zero.
- `check --read-data` exits non-zero on a damaged pack file.

**Scheduler behaviour with real Rustic**

- Backup with live progress and no MQTT; several sources in one snapshot; sources defined in
  a profile giving one snapshot each.
- An enabled job refreshes restored pre-file-count snapshot telemetry at startup against a
  real local Rustic repository; the latest file count and size are then available again.
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
- Commands: a `run` message starts a real backup and a real forget (`trigger: mqtt`); a
  retained command already on the broker starts nothing; an operation not listed is refused
  with a `rejected` event; the retained `state` topic goes `queued`, `running`, `idle`.
- A stopped broker is noticed while nothing is being published. After it restarts empty, the
  discovery messages are back and a command works again.
- Home Assistant discovery messages are retained on the broker with the expected topics.

**Home Assistant** (one manual run, not part of the test suite)

Home Assistant 2026.11.1 in a container, connected to a Mosquitto broker, with the scheduler
running a scripted stand-in for Rustic:

- every entity was created without a warning or error in Home Assistant's log;
- state, outcome with attributes, finish time, duration, progress, data added and snapshot
  count showed the published values, including a failed check;
- the `Run check` and `Run forget` buttons started those operations;
- all entities turned `unavailable` when the scheduler stopped.

The `Latest snapshot size` and `Latest snapshot files` sensors were added later and have
only been checked as discovery payloads in unit tests, not in a running Home Assistant.
The retained idle backup progress and removal of its expiry were also added later and have
not been checked in a running Home Assistant.

**Version**

- The package version is derived from the Git tag (`hatch-vcs`); `just e2e` builds the image
  with that version and checks that `rustic-scheduler --version` in the container reports it.
  Not exercised yet: a real GitHub release run of the CI workflow with this mechanism.

Not checked there: removal of entities through an empty payload, other Home Assistant
versions, how the entities look in the UI, and the web interface link. Unit tests check
the discovery payload's link with the web interface enabled and disabled, the trailing
slash added to `web.public_url`, and that discovery messages pass through secret masking
(against a stand-in MQTT client, not a broker). A URL whose path is itself a secret is not
detected.

**Web interface**

- With the real binary (integration test): a backup and a check started through the run
  route succeed with `trigger: web` and appear in the history; the "plain Rustic" commands
  of the manual panel, run as printed against the real binary, list only that job's
  snapshot, list its files, pass `check`, and restore a file. `restore --dry-run` restores no
  file, but Rustic 0.11.4 does create the empty target directory.
- With the scripted stand-in (unit tests): the job description and the latest snapshot's
  size and file count in the overview, kept after a later failed collection and absent
  while telemetry is off.
- With the real binary (integration test): the Snapshots tab's routes list a job's snapshots
  newest first, without those of another job in the same repository; the root, the source
  directory and a subdirectory of a snapshot with each entry's type, size, modification
  time, permissions, owner and link target as backed up; a missing path, a file given as a
  directory and another job's snapshot are answered with 404; nothing is recorded as a run
  and the repository still passes `check`.
- With the scripted stand-in (unit tests), for the Snapshots tab: off by default; lists kept
  in memory until an operation finishes or a refresh; a busy repository refused rather than
  queued behind, with earlier lists still served; truncation at `max_entries`; short ids,
  `latest`, `..` and requests without the page's header refused before Rustic is started.
- With the scripted stand-in (unit tests): progress of a running backup in the overview;
  bounded, newest-first history including skipped runs, and what it holds after a restart;
  scheduled and unscheduled operations in the overview;
  `forget` refused unless listed and enabled, and still previewed first; refusal of a second
  request without a recorded skip; requests without the page's header or with a foreign
  `Origin` refused; Basic authentication on every route, password rotation, and an
  unreadable or empty password file failing closed; no secret value in the manual commands.
- With Node.js and a simulated DOM (unit test, skipped when Node.js is unavailable): the
  Next runs tab, chronological ordering across timezones, exclusion of unscheduled
  operations, refresh, empty state and keyboard navigation. This is not a browser test.
- With Node.js and a simulated DOM: snapshot timestamps are plain text and each Browse
  button selects its snapshot and requests its initial directory; the empty list spans all
  columns. This is not a browser test.
- Run output, with the real binary (integration test): during a backup of about 190 MB
  started through the run route, the output route already returns Rustic's log lines while
  the run is still `running`; the text received in pieces equals the run log, starts with
  the command line, ends with the result and holds no password.
- Run output, with the scripted stand-in and plain processes (unit tests): every process
  of a run appends to one log (forget's preview and removal, each retry attempt), with the
  scheduler's lines, file mode 0600; offsets only grow; the run started by a request is told
  apart from the previous one; queued and skipped requests; secrets masked and terminal
  codes removed; a long log opened at its end in whole lines; a line still being written
  held back; requests without the page's header, unknown jobs, operations and runs, bad
  offsets and ids that look like paths refused; switched off by `web.run_logs.enabled` or
  `run_logs_per_operation: 0`; a pruned log reported as no longer kept.
- Run output, with Node.js and a simulated DOM: after "Run now" the window waits for a run
  other than the previous one, appends output as text (markup shown literally), highlights
  the scheduler's lines, asks again at once when more is waiting, shows the outcome and
  stops asking once the run has finished. This is not a browser test.
- Run output, one manual run not part of the test suite, in headless Chrome against a
  throwaway local repository and the real binary: "Run now" opens the window, which shows
  Rustic's lines while the backup is running and the outcome when it ends; Esc closes it;
  Output in Recent runs reopens the finished run; light and dark; a 390 px wide window
  without horizontal scrolling. Copy, and the Follow checkbox while scrolling, were not
  exercised.
- One manual run of the tabbed page, not part of the test suite, in headless Chrome against
  a local repository made by the real binary: switching tabs, listing two jobs' snapshots,
  opening a snapshot, entering a directory and going back up by the path, a file name
  containing `<i>` shown as text, "Run now", light and dark, a narrow window; no error in
  the browser console.
- One earlier manual run, not part of the test suite: the scheduler process serving the page to
  headless Chrome (light and dark), showing a running backup's progress, a failed check, the
  history and the manual panel; the metrics port still refused a run request.

Not checked: a person clicking in a browser window (in the manual run the clicks were sent
by script, and only for "Run now" on a backup, the tabs and the snapshot browser); the
confirmation dialog of `forget` and `prune`; the copy buttons; keyboard navigation of the
tabs; how browsers show the icon; browsers other than Chrome; the page behind a reverse
proxy; progress display for operations other than backup; snapshots with very many files in
one directory, on a slow or remote repository, or in a repository with cold storage.

**Apprise** (real library, local HTTP receiver)

- Success and failure notifications delivered with the documented content.
- The start-up notification delivered with the documented content, with no events selected.
- An unreachable target: backup still `succeeded`, failure counted, token absent from logs.

**S3** (real Rustic against a local S3 server)

- Hot/cold repository through `opendal:s3`: backup, telemetry and check; which object kinds
  land in the hot and cold prefixes.
- Credentials from `AWS_SHARED_CREDENTIALS_FILE` only; clean failure without them.
- `x-amz-storage-class: DEEP_ARCHIVE` sent for every cold upload and for no hot upload.
- Restore from that repository; the value of `%path` given to a warm-up command.

## Verified with a scripted stand-in for Rustic

The unit tests in `tests/unit/` (235 tests) cover logic that needs precise control over
timing and failure: configuration validation and error messages, including refusing a
label shared by two jobs unless both set different hosts; schedule arithmetic across
time zones and both DST transitions; command construction; profile conflict analysis; the
process runner (timeouts, SIGKILL escalation, process groups, long lines); every outcome and
reason; retries; after-backup ordering and its skip rules; all forget safeguards; overlap,
queueing, lock time-outs and the external lock file; shutdown; crash recovery and state
restore; on-time, late, missed and collapsed runs with a controllable clock; catch-up
evidence rules; metric label bounds and unknown-as-absent; notification selection, retry and
time-out; the start-up log line and start-up notification; Pushgateway verification results; health and readiness following the loop; the
network port being unable to start jobs; MQTT queueing without a broker; secret masking; MQTT
command validation and refusals; the content of the Home Assistant discovery messages.

The stand-in's canned `summary` is checked against the real binary's field set by an
integration test, so the two cannot drift apart unnoticed.

Repository creation (`init`): refused without `allow_init`, from the scheduler's trigger, the
web page and the control socket alike; success only with Rustic's confirmation line; a missing
confirmation is `unknown`; a refusal by Rustic is `failed` and never retried; the page offers
`init` only when the job allows it and `web.operations` lists it, and it asks before sending.

The web page's jobs table (the Description column, its links and the confirmation text) is
checked in a simulated DOM, not in a browser. The links were fetched once to confirm that they
exist; they point at Rustic's online book, which may describe a newer Rustic than 0.11.4.

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
  were run locally. This includes publishing pull request images to GHCR (`pr-<number>`),
  which has not been checked on a real pull request.
- MQTT client certificates; MQTT brokers other than Mosquitto.
- Apprise targets other than the JSON webhook; each service has its own failure modes.
- Pushgateway behind basic authentication.
- Rustic backends other than local and S3.
- Windows and macOS as runtime platforms. Development and the tests ran on macOS; the
  container targets Linux.
