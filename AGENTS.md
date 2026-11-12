# AGENTS.md

Guidance for coding agents working on this repository. User documentation is in `README.md`
and `docs/`; this file covers what you need to change the code safely.

## What this is

`rustic-scheduler` is a Python service that runs the [Rustic](https://github.com/rustic-rs/rustic)
backup CLI as an **external process** on a YAML-defined schedule and reports what happened
through Prometheus, MQTT and Apprise. It ships as a Docker image with a Compose file.

It is backup software. A wrong "succeeded" or an unintended deletion is worse than a crash.

## Commands

```sh
just setup        # uv sync + the pinned Rustic binary in .tools/ (checksum verified)
just lint         # ruff format --check, ruff check, mypy --strict
just fmt          # apply formatting and safe fixes
just test-unit    # unit tests: no Rustic binary, no Docker
just test         # unit + integration; integration tests skip without .tools/rustic or Docker
just test-strict  # same, but a missing Rustic or Docker is an error (what CI runs)
just e2e          # build the image, run it with Pushgateway and Prometheus
```

Run `just lint` and `just test-strict` before calling a change done. Python >= 3.12, managed
with `uv`; do not add dependencies without a reason and update `uv.lock` when you do.

## Layout

| Path | Role |
|---|---|
| `src/rustic_scheduler/config.py` | Pydantic models for the YAML, plus `_semantic_problems` for cross-field checks |
| `profiles.py`, `commands.py` | Reading merged Rustic profiles via `rustic show-config`; building Rustic command lines |
| `runner.py` | Process execution: process groups, SIGTERM then SIGKILL, run logs |
| `engine.py` | Scheduling loop, submission, per-repository locking, outcome classification, forget safeguards |
| `schedule.py`, `locks.py`, `state.py` | Schedule arithmetic (DST), repository locks, `state.json` |
| `metrics.py`, `httpserver.py` | `rustic_scheduler_*` metrics; `/metrics`, health, status, control socket |
| `mqtt.py` | MQTT connection: queued publishing, receiving, reconnect |
| `mqtt_commands.py` | Starting operations by MQTT message |
| `homeassistant.py` | Home Assistant MQTT discovery payloads |
| `notify.py`, `pushgateway.py` | Apprise notifications; Pushgateway delivery verification |
| `app.py`, `cli.py` | Start-up wiring; the `rustic-scheduler` command |
| `tests/unit/` | Uses `tests/fake_rustic.py`, a scripted stand-in driven by a JSON plan |
| `tests/integration/` | Real Rustic binary; `docker`-marked tests start real Pushgateway, Prometheus, Mosquitto and an S3 server |
| `tests/e2e/run.sh` | Tests the built image |
| `docs/`, `examples/` | User documentation; example config, profiles, Prometheus rules |

## Invariants: do not break these

1. **Never trust Rustic's exit status alone.** Rustic 0.11.4 exits 0 when interrupted by
   SIGTERM or SIGINT. A backup is `succeeded` only if Rustic emitted its JSON summary and the
   scheduler did not stop it. See `classify` and `_backup` in `engine.py`.
2. **Five outcomes, one per run:** `succeeded`, `failed`, `skipped`, `interrupted`,
   `unknown`. Queued or running is never a result. A crash mid-run becomes `unknown`.
3. **Nothing destructive by default.** `forget`, `prune`, repository `init` and Pushgateway
   group cleanup are off unless the YAML enables them. A destructive option found only in a
   Rustic profile is a start-up error. Requests (CLI or MQTT) for a `forget` or `prune` that
   is not enabled on the job are refused.
4. **Forget is two-phase and scoped.** Dry-run preview first, then checks (every selected
   snapshot carries the job's label and host, `max_remove`, at least one kept), then the real
   run. Do not add a path that skips the preview.
5. **Profiles belong to the user.** They are passed to Rustic in order with `-P` and merged
   by Rustic. Do not parse, rewrite or re-implement the profile schema. Where the scheduler
   sets an option that a profile also sets, fail at start-up; never override silently.
6. **One operation per repository at a time** (`locks.py`: asyncio lock plus `flock`).
7. **Reporting never changes an outcome.** MQTT, notifications, Pushgateway verification and
   telemetry failures are counted and logged, and must not block, delay or fail a run.
8. **Unknown is not zero.** A value Rustic did not report is an absent metric series or JSON
   `null`.
9. **Bounded metric labels.** Only `backup_job`, `operation` and small fixed vocabularies
   (`Outcome`, `Trigger`, `Reason`, ...). Never free text, paths or snapshot ids. The label is
   `backup_job`, not `job`, because Prometheus reserves `job`.
10. **Secrets** come from mounted files, never from the YAML, examples, logs, metrics, MQTT
    payloads or notifications. Text leaving the process goes through `redact.REDACTOR`;
    register new secret values with it. Never commit credentials.
11. **Network control surface.** The TCP port is read-only; triggers go through the Unix
    control socket. The only network path that can start work is the opt-in MQTT command
    topic. It ignores retained messages, allows only `mqtt.commands.operations` (default
    `backup`, `check`) and still applies rule 3. Do not widen it without the user asking.
12. **Two scrape endpoints** (scheduler and Pushgateway). Do not document a single combined
    endpoint unless you implement and test one.

## Working rules

- **Never run against a real repository or AWS.** Tests use throwaway local repositories and
  a local S3 server. Do not `init`, `forget`, `prune` or otherwise modify an existing
  repository, and do not deploy anything, without the user's explicit approval.
- **Do not claim what was not tested.** `docs/verification.md` separates verified from
  unverified behaviour; update it with every feature. Nothing about real S3 Glacier storage
  classes, restores or costs has been verified.
- **Check Rustic's behaviour, do not assume it.** When a change depends on what Rustic
  prints or does, add an integration test against the pinned binary. If `fake_rustic.py`
  learns a new output, assert in an integration test that the real binary produces it.
- **Pinned versions.** Rustic (`SUPPORTED_RUSTIC_VERSION`, `scripts/install-rustic.sh`,
  `docker/Dockerfile`), the base images and the service images in tests and `compose.yaml`
  are pinned, some by digest. Bump them deliberately and together, and rerun everything.
- **Configuration changes** need: the model in `config.py` (unknown keys are rejected), an
  actionable error message with the setting's path, a row in `docs/configuration.md`, an
  entry in `examples/config.yaml` if users will want it, and tests for both the valid and the
  invalid case. `on` is not usable as a YAML key (YAML reads it as a boolean).
- **New behaviour is opt-in** unless it is read-only and harmless, and its default is
  documented.
- **Documentation is part of the change:** `README.md` for anything a new user must know,
  the matching file in `docs/`, and `docs/verification.md`.

## Testing notes

- Unit tests drive `Engine` through `Sandbox` in `tests/helpers.py`; `sandbox.write_plan()`
  scripts what the fake Rustic prints, how long it takes and how it exits, and
  `sandbox.journal()` lists the commands it received.
- Tests write the metric label as `job`; `helpers.metric()` maps it to `backup_job`.
- Unix socket paths are short-limited, so fixtures use `/tmp` (`short_tmp`), not `tmp_path`.
- Docker-marked tests create containers named `rs-test-*` and remove them; if a run is
  killed, clean up with `docker ps -a --filter name=rs-test- -q | xargs docker rm -f`.
- Ruff rules are relaxed for tests (see `pyproject.toml`); `src` is under `mypy --strict`.

## Git

Commit or push only when asked. Never commit `.tools/`, state directories or secret files.
