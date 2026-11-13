# Web interface

An optional page, served by the scheduler itself, to see what is going on and to start an
operation by hand. It is **off by default**.

```yaml
web:
  enabled: true
  port: 9301
  operations: [backup, check]          # what the page may start
  password_file: /run/secrets/web_password
  browse:
    enabled: true                      # list snapshots and their files; off by default
```

Open `http://<host>:9301/`. With the example Compose file, publish the port first (it is
commented out in `compose.yaml`).

The page has four tabs: **Jobs**, **Snapshots**, **Recent runs** and **Using Rustic
directly**. The address keeps the open tab (`#jobs`, `#snapshots`, `#history`, `#manual`), so
a tab can be bookmarked. The header with the scheduler's state, and messages about requests,
stay visible on every tab.

## Jobs

One card per job, one row per operation that is enabled for it:

- **State**: `idle`, `queued` (waiting for the repository) or `running`.
- **Progress**, while running: percentage, bytes and Rustic's estimate of the time left, as
  far as Rustic reports them, and how long the run has been going. Backups report progress;
  an operation that reports none shows a moving bar with no percentage, not 0 %. Progress covers Rustic's current byte-counting phase only (see
  [metrics.md](metrics.md)).
- **Next run**, when idle and scheduled.
- **Last result**: outcome, time and duration; the reason when it did not succeed; and the
  last skipped run, if any.
- **Run now**, for the operations listed in `web.operations`.

The page refreshes itself every three seconds (jobs and recent runs; not the snapshots).

### Starting an operation

"Run now" does what `rustic-scheduler trigger` and an MQTT command do: the run is queued, waits
for the repository, and a backup is followed by its after-backup sequence. The result carries
`"trigger": "web"`.

- Only operations in `web.operations` can be started. The default is `[backup, check]`.
  `forget` and `prune` delete data: they must be listed explicitly **and** be enabled on the
  job, and the page asks for confirmation before sending the request. A forget started here
  goes through the same preview and safeguards as any other
  ([maintenance.md](maintenance.md)), including `forget.dry_run`.
- `operations: []` makes the page read-only.
- A request for an operation that is already queued or running is refused, and is not
  recorded as a skipped run.
- "Queued" is not a result. Watch the row, or the history.
- An enabled `forget`, `check` or `prune` with no `schedule` and no `after_backup` is
  accepted when it is listed in `web.operations`: it then runs on request only.

There is no button to stop a run.

## Snapshots

Off unless `web.browse.enabled` is set; the tab then says how to turn it on.

Choose a job to see its snapshots, newest first: when each was taken, its short id, host,
paths, number of files, size, how much it added to the repository, and tags. Choose a
snapshot to see its files. The listing opens at the snapshot's path (or at `/` when it has
several) and shows one directory at a time: name, size, time of last modification,
permissions and owner. Directories open on a click; the path above the table leads back up.

- **Read-only.** It lists; it does not download, restore or delete. For those, see
  "Using Rustic directly".
- **Only the job's own snapshots**, the ones with the job's label and host
  ([rustic-integration.md](rustic-integration.md#snapshot-scope)), also when several jobs
  share a repository.
- **No creation date.** Rustic records when a file was last modified and when its metadata
  last changed, not when it was created. The page shows the modification time. A value
  Rustic did not record is shown as "–": the parent directories of a backed-up path, for
  instance, have no time and no owner.
- **It asks Rustic, so it needs the repository.** Each list is one short Rustic process
  (`snapshots --json`, or `cat tree SNAPSHOT:PATH` for a directory), limited by
  `web.browse.timeout`. Like everything else the scheduler runs, it takes the repository
  lock, but it does not queue: while a backup or another operation is using the repository,
  a new list is refused with a message, and what was listed before is still shown (a
  snapshot list that could not be renewed is marked as possibly out of date). Conversely, a
  run that becomes due during a listing starts when the listing ends.
- **Kept in memory only.** The snapshot list of a job is kept until an operation finishes or
  "Refresh" is pressed; press "Refresh" after changing the repository outside the scheduler.
  The 32 most recently opened directories are kept too. Nothing is written to disk.
- **Large directories** are cut at `web.browse.max_entries` (default 5000), with a note.
- On a repository with cold storage this reads metadata only (snapshots and trees), which
  Rustic keeps in the hot part of a hot/cold repository. This project has not tested it
  against S3 Glacier storage classes.

## Recent runs

The most recent finished and skipped runs, newest first: time, job, operation, outcome,
trigger, duration and detail. `web.history_size` (default 50) bounds the list.

This is **not a stored history**. The list lives in memory. After a restart it starts with
the last known result of each operation, which is all the state file holds, and grows from
there. For anything longer use Prometheus (`rustic_scheduler_operation_runs_total`,
`..._last_run_*`), the run logs in the state directory, and the repository's own snapshots.

## Using Rustic directly

Ready-to-copy commands, per job, to look at the repository without the scheduler: list the
job's snapshots, list the files of a snapshot, check the repository, see what a restore would
do, restore. Two variants are shown:

- through the helper in the container (`rustic-scheduler rustic JOB -- ...`), which uses the
  same profiles, environment and binary as the scheduler;
- with plain `rustic -P <profile> ...`, for the container or any other machine that has the
  profile files and the secrets they point to.

The commands are built from the job's `profiles`, `label`, `host` and the `env` settings of
the YAML. No password or credential is shown: profiles and environment refer to secret
*files*. Values of `rustic.env` and `jobs.<name>.env` are shown (masked where they look like
secrets), which is one more reason to keep secrets out of them.

The panel also says what is safe: listing, `check` and `restore --dry-run` at any time;
`forget` and `prune` by hand skip the scheduler's safeguards. For restores, and for what
cold storage (Glacier) adds to them, see [restore.md](restore.md). Restoring from Glacier
storage classes has not been tested by this project.

## Security

This is the one HTTP listener that can start work, which is why it is separate from the
metrics port (9300 stays read-only) and off by default.

| Protection | What it does |
|---|---|
| `web.operations` | Limits what can be started. Keep `forget` and `prune` out unless everyone who can open the page may delete backup data within the job's retention rules. |
| `web.password_file` | HTTP Basic authentication (`web.username`, default `admin`) on every route, the page included. The file holds the password alone; it is read at each request, so replacing the file rotates it. If the file cannot be read, every request fails with 503: an unreadable password never means "no password". |
| `web.browse.enabled` | Off by default. When on, everyone who can open the page can read the names, sizes, times and owners of the files in every job's snapshots; not their contents. |
| Same-site check | A request to start an operation, or to list snapshots or files, must carry a header that only the scheduler's own page sends, and a matching `Origin`. Another web site open in your browser cannot start a run or make the scheduler run Rustic. |
| Content policy | The page loads nothing from other sites and has no inline script; job names and error text are inserted as text. |

What it does **not** do:

- **No TLS.** The password travels in clear text. On anything but a trusted network, publish
  the port on loopback and put a reverse proxy with HTTPS in front of it.
- **No password by default.** Without `password_file`, anyone who can reach the port can
  start the listed operations and read job names, paths in error messages and the commands
  of the manual panel, and with `web.browse.enabled` the file names in every snapshot. The
  scheduler logs a warning at start-up in that case.
- No user accounts, no roles, no rate limiting, no audit trail beyond the scheduler's log
  (each accepted and refused request is logged) and
  `rustic_scheduler_web_commands_total{result}`.

Without a password, the same-site check stops ordinary cross-site requests but not an attack
that makes your browser treat a hostile site as the scheduler's address (DNS rebinding). A
password stops that too.

## API

The page uses these routes, on the web port only:

```
GET  /api/v1/overview                                 jobs, operations, progress, history, manual commands
POST /api/v1/jobs/{job}/{operation}/run               header: X-Requested-With: rustic-scheduler
GET  /api/v1/jobs/{job}/snapshots[?refresh=1]         same header; needs web.browse.enabled
GET  /api/v1/jobs/{job}/snapshots/{id}/files?path=/a  same header; needs web.browse.enabled
```

`POST` answers 202 when queued, 409 with a `message` when refused, 403 without the header,
404 for an unknown job or operation, 401 without valid credentials when a password is set.

The two listing routes answer 200 with JSON; 404 when browsing is off, for an unknown job,
a snapshot that is not the job's (the full 64-character id is required) or a path that is
not a directory of it; 400 for a path containing `.` or `..`; 403 without the header; 409
when the repository is in use; 502 with Rustic's message when Rustic failed; 503 during
shutdown. Sizes and times that Rustic did not report are `null`.

```sh
curl -u admin:"$(cat web_password)" -X POST -H 'X-Requested-With: rustic-scheduler' \
  http://localhost:9301/api/v1/jobs/documents/check/run
```
