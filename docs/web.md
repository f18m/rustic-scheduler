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
  run_logs:
    enabled: true                      # show Rustic's output of each run; on by default
```

Open `http://<host>:9301/`. With the example Compose file, publish the port first (it is
commented out in `compose.yaml`).

Set `web.public_url` to the externally reachable HTTP(S) URL to include a link in Home
Assistant MQTT discovery when both features are enabled. This can differ from the listen
address and port when using Docker or a reverse proxy. The URL must not contain credentials,
a query or a fragment.

The URL is the directory the page is served from, so it ends with `/`; the scheduler adds
the slash when it is missing (`https://example.com/backup` becomes
`https://example.com/backup/`). The page loads its stylesheet, script and API by relative
paths, so a reverse proxy serving it under a sub-path must forward everything below that
path.

The scheduler publishes the URL as written, in retained messages that every client of the
broker can read. It cannot tell a secret path segment from an ordinary one: do not use a
URL whose path carries an access token (as some tunnel and share-link services issue), and
put authentication in `web.auth.password_file` or the reverse proxy instead.

The page has five tabs: **Jobs**, **Snapshots**, **Recent runs**, **Next runs** and **Using
Rustic directly**. The address keeps the open tab (`#jobs`, `#snapshots`, `#history`, `#next`,
`#manual`), so a tab can be bookmarked. The header with the scheduler's state, and messages
about requests, stay visible on every tab.

## Jobs

One card per job. Under the job's name:

- its `description`, when the job has one;
- **Latest snapshot**: time, total size and number of files of the job's newest snapshot,
  the number of snapshots, and when this was last read from the repository. It comes from
  snapshot telemetry ([maintenance.md](maintenance.md#snapshot-telemetry)), so the job needs
  `snapshots: {enabled: true}`; without it the page says so instead of showing a number. A
  value Rustic did not record is shown as unknown, never as 0. After an upgrade, older
  saved telemetry may show an unknown file count until its startup refresh succeeds; the
  Snapshots tab reads the repository independently.

Then one row per operation that is enabled for it:

- **Operation** and **Description**: what the operation does, with a link to the Rustic
  documentation for the command it runs (`init` links to the `rustic init` chapter). The
  links open Rustic's online book; the page loads nothing from it.
- **State**: `idle`, `queued` (waiting for the repository) or `running`.
- **Progress**, while running: percentage, bytes and Rustic's estimate of the time left, as
  far as Rustic reports them, and how long the run has been going. Backups report progress;
  an operation that reports none shows a moving bar with no percentage, not 0 %. Progress covers Rustic's current byte-counting phase only (see
  [metrics.md](metrics.md)).
- **Next run**, when idle and scheduled.
- **Last result**: outcome, time and duration; the reason when it did not succeed; and the
  last skipped run, if any.
- **Run now**, for the operations listed in `web.operations`.
- **Output** while the operation runs, **Last output** otherwise: opens its run's output
  (see [Run output](#run-output)).

The page refreshes itself every three seconds (jobs, recent and next runs; not the snapshots).

### Starting an operation

"Run now" does what `rustic-scheduler trigger` and an MQTT command do: the run is queued, waits
for the repository, and a backup is followed by its after-backup sequence. The result carries
`"trigger": "web"`.

- Only operations in `web.operations` can be started. The default is `[backup, check]`.
  `forget` and `prune` delete data: they must be listed explicitly **and** be enabled on the
  job, and the page asks for confirmation before sending the request.
- `init` creates the job's repository. It must be listed in `web.operations` **and** the job
  must set `allow_init: true`; otherwise the row is not offered, or the request is refused.
  The page asks for confirmation first. Rustic refuses `init` when a repository already
  exists at the location, and the run then fails with Rustic's message. `init` is never
  scheduled and is never retried. A forget started here
  goes through the same preview and safeguards as any other
  ([maintenance.md](maintenance.md)), including `forget.dry_run`.
- `operations: []` makes the page read-only.
- A request for an operation that is already queued or running is refused, and is not
  recorded as a skipped run.
- "Queued" is not a result. Watch the row, or the history.
- An enabled `forget`, `check` or `prune` with no `schedule` and no `after_backup` is
  accepted when it is listed in `web.operations`: it then runs on request only.

There is no button to stop a run.

## Run output

After "Run now", a window opens with Rustic's output for that run, like a terminal. It
waits while the run is queued, then follows the output as Rustic writes it, about once a
second, and stops when the run has finished. The header shows the state (running, then the
outcome) and, at the end, the duration, the number of attempts and the reason when the run
did not succeed. The same window opens from **Output** / **Last output** on the Jobs tab and
from **Output** in Recent runs, for any run whose log is still kept.

The window shows the run log the scheduler already writes
(`<state_dir>/logs/<job>/<time>-<operation>.log`), so it needs `scheduler.state_dir` and
`scheduler.run_logs_per_operation` above 0, and it shows the same text:

- Rustic's log lines (stderr) and its plain stdout lines, **with secrets masked**. Backup
  progress messages are left out; the Jobs tab shows progress. The JSON output of `forget`
  and `snapshots`, which the scheduler reads itself, is not shown either.
- Lines the scheduler adds, highlighted: `$ rustic ...` before each Rustic process (the
  command line, masked), how that process ended, forget's preview and removal steps, retry
  attempts, and the run's result at the end.
- Terminal colour codes and other control characters are removed.

Closing the window does not stop the run, and following it does not delay or change the run.
The page keeps up to about 2 million characters; for a longer log it shows the end. A log
opened after it has grown beyond 256 KiB starts near its end, with a note. "Follow the
output" scrolls along with the output, and scrolling up turns it off. **Copy** copies what
the window shows.

`web.run_logs.enabled: false` hides the buttons and the output (the API answers with the
run's state and no output).

## Snapshots

Off unless `web.browse.enabled` is set; the tab then says how to turn it on.

Choose a job to see its snapshots, newest first: when each was taken, its short id, host,
paths, number of files, size, how much it added to the repository, and tags. Use the Browse
icon on a snapshot's row to see its files. The listing opens at the snapshot's path (or at
`/` when it has several) and shows one directory at a time: name, size, time of last
modification, permissions and owner. Directories open on a click; the path above the table
leads back up.

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
trigger, duration and detail, with an **Output** button for runs whose run log is still kept
(the newest `scheduler.run_logs_per_operation` of each operation). `web.history_size`
(default 50) bounds the list.

This is **not a stored history**. The list lives in memory. After a restart it starts with
the last known result of each operation, which is all the state file holds, and grows from
there. For anything longer use Prometheus (`rustic_scheduler_operation_runs_total`,
`..._last_run_*`), the run logs in the state directory, and the repository's own snapshots.

## Next runs

The next scheduled occurrence of each job's scheduled operation, earliest first, with its
job, operation and schedule. Times are displayed in the browser's local timezone; the
schedule includes its configured timezone. Operations without a schedule, including
operations run only on request or after a backup, are not listed. A scheduled operation
is listed even while it is queued or running; the actual run can still be skipped or delayed.
This is the scheduler's next occurrence per operation, not a calendar of all future runs.

## Using Rustic directly

Ready-to-copy commands, for one job at a time, to look at the repository without the scheduler.
The job is chosen in the Job list at the top of the tab, as on the Snapshots tab. The commands
list the job's snapshots, list the files of a snapshot, check the repository, see what a
restore would do, restore. Two variants are shown:

- through `docker exec` with the scheduler's container, using all environment variables from
  your `rustic.env` and `jobs.<name>.env` configuration, which ensures the same profiles,
  environment and binary are available;
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
| `web.operations` | Limits what can be started. Keep `forget` and `prune` out unless everyone who can open the page may delete backup data within the job's retention rules. `init` also needs `allow_init` on the job. |
| `web.auth.enabled` | Enable HTTP Basic authentication on every route, the page included. When enabled, `web.auth.password_file` must be set. When disabled, no authentication is required. |
| `web.auth.password_file` | The file holds the password for HTTP Basic authentication (with `web.auth.username`, default `admin`). It is read at each request, so replacing the file rotates it. If the file cannot be read, or holds no password (empty or only whitespace), every request fails with 503: an unreadable or empty password never means "no password". Only used when `web.auth.enabled` is true. |
| `web.browse.enabled` | Off by default. When on, everyone who can open the page can read the names, sizes, times and owners of the files in every job's snapshots; not their contents. |
| `web.run_logs.enabled` | **On by default.** Everyone who can open the page can read Rustic's output of each run: the command lines, file paths that Rustic names in warnings and errors, and repository locations, with secrets masked. Turn it off if that is more than the page's users should see. |
| Same-site check | A request to start an operation, to list snapshots or files, or to read a run's output, must carry a header that only the scheduler's own page sends, and a matching `Origin`. Another web site open in your browser cannot start a run or make the scheduler run Rustic. |
| Content policy | The page loads nothing from other sites and has no inline script; job names and error text are inserted as text. |

What it does **not** do:

- **No TLS.** The password travels in clear text. On anything but a trusted network, publish
  the port on loopback and put a reverse proxy with HTTPS in front of it.
- **No password by default.** Without `password_file`, anyone who can reach the port can
  start the listed operations and read job names, paths in error messages, the commands
  of the manual panel and Rustic's output of each run (unless `web.run_logs.enabled` is
  false), and with `web.browse.enabled` the file names in every snapshot. The
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
GET  /api/v1/jobs/{job}/{operation}/output[?run=ID&offset=N]  same header
```

`POST` answers 202 when queued, 409 with a `message` when refused, 403 without the header,
404 for an unknown job or operation, 401 without valid credentials when a password is set.

The two listing routes answer 200 with JSON; 404 when browsing is off, for an unknown job,
a snapshot that is not the job's (the full 64-character id is required) or a path that is
not a directory of it; 400 for a path containing `.` or `..`; 403 without the header; 409
when the repository is in use; 502 with Rustic's message when Rustic failed; 503 during
shutdown. Sizes and times that Rustic did not report are `null`.

`POST .../run` also returns `previous_run_id`, the id of the operation's latest run before
the request, and `requested_at`; the page waits for a run with another id.

The output route answers with the run's `state` (`running`, `finished`, `queued` or `none`),
the `run` (as in the history, with its `run_id`), the operation's `last_skip`, and `log`:
`{available, message, offset, next_offset, text, more, skipped}`. Without `run` it describes
the running run, else the latest finished one. `text` holds whole lines from byte `offset`,
at most 256 KiB; ask again from `next_offset`, at once when `more` is true. When the log
cannot be shown, `available` is false and `message` says why. It answers 404 for an unknown
job or operation and for a run no longer in the scheduler's memory, 400 for a bad offset,
403 without the header. The file is found from the scheduler's own record of the run, never
from a name in the request.

```sh
curl -u admin:"$(cat web_password)" -X POST -H 'X-Requested-With: rustic-scheduler' \
  http://localhost:9301/api/v1/jobs/documents/check/run
```
