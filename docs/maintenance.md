# Snapshot telemetry, retention, prune and check

All four are optional and off by default. They are configured per job.

| Operation | Rustic command | Changes the repository | Scope |
|---|---|---|---|
| `snapshots` | `snapshots --json` | no | the job's snapshots |
| `forget` | `forget` | **deletes snapshots** | the job's snapshots |
| `forget` with `prune: true` | `forget --prune` | **deletes snapshots and data** | snapshots: the job; data: whole repository |
| `prune` | `prune` | **deletes data** | whole repository |
| `check` | `check` | no | whole repository |

## When they run

| Operation | After a backup of the job | On its own schedule | On request |
|---|---|---|---|
| `snapshots` | yes if `after_backup` (default), whatever the backup's outcome | optional | always allowed |
| `check` | if `after_backup: true`, only after a succeeded backup | optional | always allowed |
| `forget` | if `after_backup` (default), only after a succeeded backup and a check that did not fail | optional | only if enabled |
| `prune` | never | required | only if enabled |

Order after a backup: **check, forget, snapshots**. Integrity is checked before anything is
removed, and telemetry is collected last so that it reflects the final state. The repository
lock is held throughout.

A scheduled maintenance operation that finds the repository busy waits for it like any other
run.

**Failure policy.** A failed maintenance operation never changes the backup's outcome. It has
its own outcome, metrics, MQTT result and `maintenance_failure` notification. If a backup did
not succeed, `check` and `forget` are recorded as `skipped` (`backup_not_successful`): old
snapshots are not removed on the strength of a backup that did not complete. Timeouts and
retries are per operation (`timeout`, `retries`, `retry_delay`). Rustic's output of each run
is in its run log.

## Snapshot telemetry

```yaml
snapshots:
  enabled: true
```

Runs `rustic snapshots --json --filter-label <label>` (plus `--filter-host` if the job sets
`host`) and exports:

- `rustic_scheduler_snapshots`: number of snapshots
- `rustic_scheduler_snapshot_latest_timestamp_seconds`, `..._oldest_timestamp_seconds`
- `rustic_scheduler_snapshot_latest_bytes`
- `rustic_scheduler_snapshot_collection_timestamp_seconds`: when this last worked
- `rustic_scheduler_snapshot_collection_errors_total`

**Target selection.** A job's snapshots are those carrying its label, which the scheduler
writes on every backup; see
[rustic-integration.md](rustic-integration.md#snapshot-scope). Jobs sharing a repository are
counted separately. If Rustic ever returns a snapshot with another label, the result is
discarded and the collection fails with `scope_violation` rather than exporting mixed
numbers.

**Staleness.** When a collection fails, the previous values stay and
`snapshot_collection_timestamp_seconds` stops advancing. Alert on its age. An empty scope
exports a count of 0 and no timestamps.

In a hot/cold repository snapshots are read from the hot part: no retrieval from cold storage
is involved.

## Retention with `forget`

```yaml
forget:
  enabled: true
  dry_run: true          # start with a preview
  keep:
    keep-daily: 14
    keep-weekly: 8
    keep-monthly: 24
  max_remove: 10
```

`keep` entries are Rustic's own `keep-*` options, passed verbatim; see
`rustic forget --help`. Rustic applies them per group of host, label and paths. With
`rules_from_profile: true` the `[forget]` section of the profiles is used instead.

### Safeguards

Forget is the only operation that removes snapshots, so it is fenced in:

1. **Off by default**, and it needs explicit retention rules. `enabled: true` without `keep`
   or `rules_from_profile` is a configuration error.
2. **Always scoped** to the job's label (and host). There is no way to run an unscoped forget
   through the scheduler; `--filter-label` and friends are refused in `extra_args`.
3. **Two phases.** Every run first executes `forget --dry-run --json`. The real command runs
   only if, in that preview:
   - every selected snapshot carries the job's label (and host), else `scope_violation`;
   - no more than `max_remove` snapshots would be removed, else `removal_limit`;
   - at least one snapshot would be kept, unless `keep-none: true` is set, else
     `removal_limit`.

   When a check fails nothing is removed and the operation is `failed`.
4. **Nothing to remove, nothing run.** If the preview removes nothing and `prune` is off, the
   real command is not executed.
5. **Not after a bad backup**, and not after a failed post-backup check.
6. **Not on request unless enabled.** `trigger JOB forget` is refused when forget is disabled.
7. **Profiles cannot widen it.** `[forget] prune = true` or a foreign `filter-labels` in a
   profile is a start-up error unless the YAML says the same.

### Preview

Three ways to see what forget would do, none of which changes anything:

```sh
# one-off, from the command line
docker compose exec rustic-scheduler rustic-scheduler forget-preview documents
```

```yaml
# continuously: every forget run is a preview, reported in metrics and results
forget:
  enabled: true
  dry_run: true
  keep: {keep-daily: 14}
```

With `dry_run: true`, `rustic_scheduler_forget_last_removed` is the number of snapshots that
*would* be removed and `rustic_scheduler_forget_last_dry_run` is 1. Remove `dry_run` once the
numbers look right. `--prune` is never added to a preview.

### Forgetting is not pruning

- **Forget** removes snapshot files. The data they referenced stays in the repository and
  keeps costing storage. This is cheap, fast, and in a hot/cold repository touches small
  objects only.
- **Prune** finds data no snapshot references any more and deletes or repacks pack files.

The scheduler only prunes when asked, in one of two ways:

```yaml
forget:
  enabled: true
  keep: {keep-daily: 14}
  prune: true            # forget --prune
```

```yaml
prune:
  enabled: true          # a separate `rustic prune`
  schedule: {days: [sun], times: ["04:00"]}
  extra_args: ["--keep-pack", "180d"]
```

Prune acts on the **whole repository**, including data of other jobs and of snapshots made
outside the scheduler. Unused data of one job is removed whichever job's prune runs.

Rustic prunes in two steps: packs are first marked, and deleted by a later prune once they
have been marked for `--keep-delete` (default 23 hours). One prune therefore frees nothing
immediately. `--instant-delete` skips this and is only safe when nothing else uses the
repository.

### Consequences and costs on Glacier storage classes

Read [aws-s3-glacier.md](aws-s3-glacier.md#deleting-data-forget-and-prune) first. In short:

- **Forget** deletes small snapshot objects. In a hot/cold repository each snapshot exists in
  both parts, and deleting the cold copy before its minimum storage duration is billed as
  early deletion, for a few kilobytes.
- **Prune** deletes pack files. Deleting a Deep Archive object younger than 180 days is
  billed for the remaining days. Use `--keep-pack 180d` so that young packs are left alone.
- **Repacking** data packs needs their content, which for archived objects means a retrieval.
  Rustic limits repacking to cacheable (tree) packs by default on hot/cold repositories
  (`--repack-cacheable-only`), so partly used data packs stay as they are and only fully
  unused ones are deleted. Space is reclaimed more slowly, without retrievals.

None of this was tested against AWS.

## Integrity with `check`

```yaml
check:
  enabled: true
  schedule: {days: [sun], times: ["05:00"]}
```

| Setting | Rustic command | What it verifies | Reads |
|---|---|---|---|
| default | `check` | Snapshots, index and pack listing are consistent; every tree is readable and every blob it references is in the index; packs listed in the index exist with the expected size | Metadata only: snapshots, index, tree packs, a listing of pack files. In a hot/cold repository these come from the hot part. **No archived data is retrieved.** |
| `read_data: true` | `check --read-data` | Additionally downloads every pack file and verifies its hash and every blob in it | **All data.** On cold storage every pack must be retrieved first. |
| `read_data_subset: "5%"` | `check --read-data-subset 5%` | The same for a subset: a percentage, a size, or `n/m` to cover the repository over `m` runs | That subset |

The default check catches a damaged index, missing packs and unreadable metadata. It does
**not** detect corruption inside a data pack. Only `read_data` does.

**Duration and cost.**

- Default check: time grows with the number of snapshots, files and packs. Typically minutes.
  On S3 it costs `LIST` and `GET` requests on the hot part.
- `read_data`: time and transfer proportional to the repository size. On Glacier Deep Archive
  every data pack must be restored first (hours, up to 48 with Bulk), which is billed per
  gigabyte and per request, plus temporary storage of the restored copies and transfer out of
  AWS if the scheduler runs elsewhere. For a large repository this is expensive and slow, and
  needs Rustic's warm-up settings; see [restore.md](restore.md). Prefer `read_data_subset`,
  on a long schedule.
- `timeout` defaults to 6 hours; a `read_data` check of archived data needs far more.

`check` is read-only. A failed check is reported as `failed` with Rustic's exit status and
error lines; the scheduler never runs `repair`.

Like prune, check covers the whole repository. When several jobs share a repository, enable
it on one of them.
