# Scheduling, concurrency and outcomes

## Schedules and time

A schedule is a set of weekdays and local times in a named IANA timezone. The scheduler
converts every (date, time) pair to exactly one instant, so behaviour around daylight-saving
changes is fixed:

| Case | Behaviour | Example, Europe/Rome |
|---|---|---|
| The local time does not exist | Runs once, shifted forward by the length of the gap | 02:30 on the last Sunday of March runs at 03:30 CEST |
| The local time occurs twice | Runs once, at the first occurrence | 02:30 on the last Sunday of October runs at the CEST 02:30 only |
| Two configured times land on the same instant | Runs once | `02:30` and `03:30` on the spring-forward day |

The weekday is the weekday of the local date. Wall-clock distance between daily runs is 23
or 25 hours across a change, as expected for a wall-clock schedule.

The timezone must be named, in the schedule or in `scheduler.timezone`. The container's `TZ`
is not used for scheduling.

The loop re-reads the wall clock at least every five seconds, so a clock change is noticed
quickly and schedules never drift.

## Concurrency

**One operation at a time per repository.** The repository is identified from what Rustic
itself reports for the job's merged profiles (`repository`, `repo-hot` and the non-secret
backend options), so two jobs that reach the same repository through different profile files
still share one lock. Override it with `repository_lock` if you need to.

- Operations on the same repository queue in arrival order.
- A run that has waited `repository_wait_timeout` is skipped (`repository_busy`).
- Operations on different repositories run in parallel.
- The lock is held for a whole backup sequence (backup, check, forget, telemetry).

The lock exists in two layers: a queue inside the scheduler, and an advisory `flock` on
`<state_dir>/locks/<id>.lock`. The second layer also covers `rustic-scheduler rustic` and a
second scheduler sharing the same state volume. The lock file descriptor is inherited by the
Rustic process, so if the scheduler dies while Rustic keeps running, the repository stays
locked until that Rustic exits.

Rustic itself is designed to tolerate concurrent access, and it does not take repository
locks. This lock is therefore the only protection against, for example, a prune overlapping
a backup. It cannot see Rustic processes started outside this container.

## A run that is still going at the next scheduled time

A (job, operation) that is already queued or running is never queued a second time. The
newer request is **skipped** with reason `already_running`, counted in
`rustic_scheduler_operation_runs_total{outcome="skipped"}` and logged. Runs do not pile up,
and a slow backup is never killed to make room for the next one. Use `backup.timeout` to
bound a run.

## Missed runs

Per job, `missed_run_policy` chooses what happens when a scheduled time passes while the run
cannot start (scheduler stopped, host suspended, clock jumped forward):

- `catch_up` (default): the run starts late if it is at most `misfire_grace` (default 24h)
  late. Later than that, it is skipped as `missed`.
- `skip`: a run more than 60 seconds late is skipped as `missed`. The job waits for its
  next time.

Several occurrences that passed together collapse into **one** run; the others are counted
in `rustic_scheduler_missed_runs_total`.

### After downtime

At start-up, for each `catch_up` schedule whose previous occurrence is within the grace
window, the scheduler runs the operation once **only if there is evidence it was missed**:

1. The state file records an older run of that operation, or a run at that time that ended
   `interrupted` or `unknown`.
2. Without state, for backups only: Rustic lists a snapshot of the job that is older than
   the missed time. This is the "no database" path: the evidence is Rustic's own metadata.

No evidence means no catch-up. In particular a brand-new job, or a repository that cannot be
read, waits for its next scheduled time. A run that failed on time is not repeated at
restart; use `retries` for that.

### Clock changes

- Forward jump: handled as missed runs, above.
- Backward jump: the next planned instant is kept, so nothing runs twice.

## Outcomes

| Outcome | When |
|---|---|
| `succeeded` | Rustic exited 0, was not interrupted, and (backup) printed its summary; (forget, snapshots) its JSON was readable and in scope. |
| `failed` | Non-zero exit, timeout, killed, could not be started, unreadable output, or a safeguard refused the operation. |
| `skipped` | Never started: `already_running`, `repository_busy`, `missed`, `backup_not_successful`, `shutdown`. |
| `interrupted` | Started, then stopped by scheduler shutdown or by a signal sent to Rustic. |
| `unknown` | Started, but the result cannot be established: exit 0 with no summary, or the scheduler died during the run. |

Each result also carries a `reason` from a small fixed list, the exit status, the number of
attempts and free-text detail. Detail is available from `/api/v1/status`, `status`, MQTT and
the run log; it is never a metric label.

**Why the exit status is not enough.** Rustic 0.11.4 handles SIGINT and SIGTERM by logging
`Ctrl-C received, shutting down...` and exiting with status 0, having written no snapshot.
The scheduler therefore requires the JSON `summary` message for a backup to succeed, and
treats that log line, or a stop it requested itself, as an interruption for every operation.

A backup that exits 0 with a summary but saves no snapshot (unchanged source with
`skip-if-unchanged`) is `succeeded`, with `backup_last_snapshots_saved` at 0.

## Timeouts, shutdown and interruption

- **Timeout**: SIGTERM to Rustic's process group, SIGKILL after `stop_grace`. Outcome
  `failed`, reason `timeout`. Not retried.
- **Shutdown** (SIGTERM or SIGINT to the scheduler, `docker stop`): scheduling stops,
  queued work is dropped as `skipped`/`shutdown`, the running Rustic is stopped as above and
  recorded as `interrupted`. Set the container's `stop_grace_period` longer than
  `scheduler.stop_grace`.
- **Rustic signalled from outside**: `interrupted`, reason `signal`.
- **Rustic killed** (for example out of memory): `failed`, reason `killed`.

An interrupted backup leaves no snapshot. Data already uploaded stays in the repository and
is reused by the next backup; anything never referenced is removed by a later prune.

Rustic runs in its own process group, so its hook commands are stopped together with it.

## Restart recovery

The state directory holds one JSON file with the last result of each operation and a marker
for runs in flight. After a restart:

- last results reappear in metrics and status;
- a run that was in flight when the scheduler stopped without recording a result is reported
  as `unknown` (`scheduler_restart`), and is a candidate for catch-up;
- nothing is assumed to have succeeded.

Without a writable state directory the scheduler still runs. It starts with no memory, run
logs and manual triggers are unavailable, and repository locking is in-process only.

## Manual runs

`rustic-scheduler trigger JOB [OPERATION] [--wait]` talks to the running scheduler through a
Unix socket in the state directory (mode 0600). A manual run:

- queues behind whatever holds the repository, exactly like a scheduled run;
- is refused if the same job and operation is already queued or running;
- for a backup, is followed by the same after-backup sequence;
- is refused for `forget` and `prune` unless they are enabled for the job.

Without `--wait` the command reports only that the run was queued, which is not a result.

## Retries

`retries` repeats an operation after a plain failure (non-zero exit), waiting `retry_delay`
between attempts. A timeout, an interruption, an unknown outcome or a refused safeguard is
not retried. However many attempts it takes, one result is recorded.
