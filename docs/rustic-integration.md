# How the scheduler uses Rustic

Rustic 0.11.4 runs as an unmodified external program. The scheduler builds a command line,
starts the process, reads its output and reports the result. Everything on this page was
checked against that binary (see [verification.md](verification.md)).

## Profiles

A job lists one or more Rustic TOML profiles. The scheduler passes each as an absolute path:

```
rustic -P /etc/rustic-scheduler/profiles/s3-deep-archive.toml -P /etc/rustic-scheduler/profiles/documents.toml ...
```

It does not read, merge, rewrite or restrict them. Any option Rustic accepts may be used,
including `[global.env]`, hooks, `use-profiles`, `[[backup.snapshots]]`, `[copy]` and
backend options. Reference:
<https://github.com/rustic-rs/rustic/blob/v0.11.4/config/README.md>.

How Rustic 0.11.4 finds and merges profiles:

- A profile name gets `.toml` appended unless it already ends in `.toml`.
- A relative name is searched in `$XDG_CONFIG_HOME/rustic/` (or `~/.config/rustic/`), then
  `/etc/rustic/`, then the working directory. An absolute path is used as it is, which is why
  the scheduler always passes absolute paths.
- With several `-P` options, **the profile listed first wins** for single-valued options.
  Later profiles only fill in what earlier ones left unset. For lists, Rustic's behaviour
  depends on the option; put a list in one profile only.
- A profile's own `use-profiles` are merged below that profile.
- A profile that does not exist is only a warning for Rustic. The scheduler turns it into a
  start-up error.

Precedence across sources, as Rustic documents it:

```
command line  >  environment variables  >  profiles
```

### How validation looks at your profiles

At start-up, and in `rustic-scheduler validate`, the scheduler runs
`rustic -P ... show-config` for each job and reads a few keys of the merged result. This is
Rustic's own answer, not a re-implementation. It is used to:

- report profiles Rustic rejects, with Rustic's message;
- identify the repository for locking;
- detect values the scheduler would override (below);
- detect destructive settings that are not enabled in the YAML.

The output can contain secrets, so it is kept in memory and never logged.

## Options the scheduler adds

Use `rustic-scheduler show-command JOB [OPERATION]` to see the exact command.

| Operation | Added to the command line |
|---|---|
| all | `-P <profile>` for each profile |
| backup | `--json-progress`, `--progress-interval <rustic.progress_interval>`, `backup`, `--label <label>`, `--host <host>` if set, `backup.extra_args`, the job's `sources` |
| snapshots | `snapshots --json --filter-label <label>`, `--filter-host <host>` if set |
| forget | `forget --json --filter-label <label>`, `--filter-host` if set, one `--keep-*` per entry of `forget.keep`, `--prune` if `forget.prune`, `forget.extra_args`; `--dry-run` for the preview |
| prune | `prune`, `prune.extra_args`; `--dry-run` if `prune.dry_run` |
| check | `--json-progress`, `--progress-interval`, `check`, `--read-data` and `--read-data-subset` if set, `check.extra_args` |

Environment: the Rustic process inherits the scheduler's environment, then `rustic.env`, then
the job's `env`. The scheduler adds one variable, `RUSTIC_PROMETHEUS=<pushgateway.url>`, when
a Pushgateway is configured.

## Nothing is overridden silently

Because the command line and environment beat the profiles, each scheduler-supplied option
could hide a profile value. Each case is handled explicitly:

| Scheduler option | If the profiles set it too |
|---|---|
| `--label` | Start-up **error** if the profile's `[backup] label` (or a `[[backup.snapshots]]` label) differs. Fix: remove it, or set `label:` on the job to the same value. |
| `--host` | Passed only if the job sets `host`. Then a different `[backup] host` is a start-up error. |
| `--progress-interval` | **Not passed** if a profile sets `[global] progress-interval`. |
| `RUSTIC_PROMETHEUS` | **Not set** if a profile sets `[global] prometheus`. Rustic then pushes where the profile says, and the scheduler does not verify delivery there. |
| `--json-progress` | Always passed; the scheduler cannot work without it. `[global] no-progress = true` still hides live progress (warning at start-up); results are unaffected. |
| `--filter-label` | A different `filter-labels` in `[forget]` or `[snapshot-filter]` is a start-up error when forget is enabled. |
| `--keep-*` | Rustic applies profile `[forget]` keep rules together with the YAML ones. Reported as a start-up warning. |
| `--prune` | Only with `forget.prune: true`. A profile's `[forget] prune = true` without it is a start-up error. |

Two Rustic facts worth knowing: a `--tag` or `--label` on the command line **replaces** the
profile's `tags` or `label` instead of adding to it, which is why the scheduler never adds
tags. And `[backup] init = true` or `[global] dry-run = true` in a profile is refused at
start-up unless allowed (`allow_init`), because either would change what a backup means.

Profile values the scheduler does not pass are left entirely to Rustic.

## Snapshot scope

A job's snapshots are the snapshots whose **label** equals the job's `label` (default: the
job name), and whose host equals `host` if that is set. Backups write that label; telemetry
and forget filter on it. Two jobs can therefore share a repository without seeing each
other's snapshots. Snapshots made by other tools, or by hand without that label, are outside
every job's scope and are never counted or removed.

As a second line of defence the scheduler checks the label (and host) of every snapshot
Rustic returns. A telemetry result containing a foreign snapshot is discarded; a forget
preview containing one aborts before anything is removed.

## Sources

`sources` are passed as arguments to one `rustic backup`, which creates **one snapshot
containing all of them**. Rustic resolves symbolic links in source paths and stores the real
path. If a source is missing, Rustic fails the whole snapshot.

To get one snapshot per folder, leave `sources` empty and define `[[backup.snapshots]]`
entries in a profile. Rustic then writes one snapshot per entry, each with the job's label;
the scheduler adds up their summaries. If one entry fails, Rustic exits non-zero and the
backup is `failed`, although the other snapshots were saved.

## What the scheduler reads from Rustic

**Progress** (`--json-progress`, one JSON object per line on stdout):

```json
{"message_type":"status","seconds_elapsed":12,"seconds_remaining":48,"percent_done":0.2,"total_bytes":5000000,"bytes_done":1000000}
```

- `percent_done` is a fraction from 0 to 1.
- `seconds_remaining`, `percent_done` and `total_bytes` are omitted when Rustic does not know
  them, for example before any byte is processed or with `no-scan`. The scheduler exports
  them as absent, never as zero.
- Only byte-counting phases report progress. Reading the index, listing packs, checking trees
  and similar phases are silent, so progress does not cover a whole operation, and a job with
  several snapshots restarts from zero for each. Treat it as "the current phase".
- Rustic emits a message when a phase starts, at each interval, and when it ends.

**Backup result**, one per snapshot:

```json
{"message_type":"summary","files_new":3,"files_changed":0,"files_unmodified":0,"dirs_new":11,"dirs_changed":0,"dirs_unmodified":0,"data_blobs":25,"tree_blobs":11,"data_added":30004315,"data_added_packed":30004780,"total_files_processed":3,"total_bytes_processed":30000012,"total_duration":0.149958,"snapshot_id":"22342e20..."}
```

`snapshot_id` is absent when no snapshot was saved.

**Snapshots** (`snapshots --json`): a list of groups, each with `snapshots`. The scheduler
uses `time`, `label`, `hostname`, `id` and, from the newest snapshot's `summary`,
`total_bytes_processed` and `total_files_processed`: the size and number of all files in the
snapshot, unchanged ones included.

**Forget** (`forget --json`): a list of groups, each with `items` holding `snapshot`, `keep`
and `reasons`.

**Logs**: Rustic's stderr goes to the run log and, at debug level, to the scheduler's log.
Warnings and errors are logged at their own level.

## Rustic behaviours to be aware of

- Exit status 0 after SIGTERM/SIGINT (see [behaviour.md](behaviour.md#outcomes)).
- A failed metrics push is a warning; the backup still exits 0.
- Rustic stores the full command line in each snapshot's summary. Never put a secret in
  `extra_args`; the scheduler refuses the obvious ones.
- Rustic takes no repository lock.
- `forget` without any keep rule fails; it does not remove everything.

## Using another Rustic version

The scheduler warns at start-up when the binary is not 0.11.4. Before upgrading Rustic, run
the integration tests against the new binary (`RUSTIC_BIN=/path/to/rustic just test`): they
check the JSON fields, merge order, exit statuses and interruption behaviour this page
relies on. See [operations.md](operations.md#upgrading).
