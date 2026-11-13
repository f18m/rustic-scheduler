"""Builds the exact Rustic command lines the scheduler runs.

Everything the scheduler adds to a command is produced here and documented in
docs/rustic-integration.md, so there is a single place to audit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import Config, JobConfig
from .model import Operation
from .profiles import MergedProfile, profile_args


@dataclass(frozen=True)
class JobRuntime:
    """A validated job together with what Rustic reported about its profiles."""

    name: str
    job: JobConfig
    label: str
    merged: MergedProfile
    binary: str
    base_args: tuple[str, ...]
    env: dict[str, str]
    pushgateway_url: str | None

    @property
    def lock_key(self) -> str:
        return self.merged.lock_key


def build_env(config: Config, job: JobConfig, pushgateway_url: str | None) -> dict[str, str]:
    """Environment for a Rustic process: inherited, then rustic.env, then job.env."""
    env = dict(os.environ)
    env.update(config.rustic.env)
    env.update(job.env)
    if pushgateway_url:
        env["RUSTIC_PROMETHEUS"] = pushgateway_url
    return env


def _format_seconds(seconds: float) -> str:
    return f"{int(seconds)}s" if seconds >= 1 else f"{int(seconds * 1000)}ms"


def _scope(runtime: JobRuntime) -> list[str]:
    args = ["--filter-label", runtime.label]
    if runtime.job.host:
        args += ["--filter-host", runtime.job.host]
    return args


def _progress(config: Config, runtime: JobRuntime) -> list[str]:
    args = ["--json-progress"]
    if not runtime.merged.progress_interval_set:
        seconds = config.rustic.progress_interval.total_seconds()
        args += ["--progress-interval", _format_seconds(seconds)]
    return args


def backup_command(config: Config, runtime: JobRuntime) -> list[str]:
    job = runtime.job
    args = [runtime.binary, *runtime.base_args, *_progress(config, runtime), "backup"]
    args += ["--label", runtime.label]
    if job.host:
        args += ["--host", job.host]
    args += job.backup.extra_args
    return args + job.sources


def snapshots_command(runtime: JobRuntime) -> list[str]:
    return [runtime.binary, *runtime.base_args, "snapshots", "--json", *_scope(runtime)]


def forget_command(runtime: JobRuntime, *, dry_run: bool) -> list[str]:
    """`rustic forget`, always limited to the job's own snapshots.

    ``--prune`` is only ever added for a real run with ``forget.prune`` enabled.
    """
    forget = runtime.job.forget
    args = [runtime.binary, *runtime.base_args]
    if dry_run:
        args.append("--dry-run")
    args += ["forget", "--json", *_scope(runtime)]
    for key, value in forget.keep.items():
        if isinstance(value, bool):
            if value:
                args.append(f"--{key}")
        elif isinstance(value, list):
            for item in value:
                args += [f"--{key}", str(item)]
        else:
            args += [f"--{key}", str(value)]
    if forget.prune and not dry_run:
        args.append("--prune")
    return args + forget.extra_args


def prune_command(runtime: JobRuntime) -> list[str]:
    prune = runtime.job.prune
    args = [runtime.binary, *runtime.base_args]
    if prune.dry_run:
        args.append("--dry-run")
    return [*args, "prune", *prune.extra_args]


def check_command(config: Config, runtime: JobRuntime) -> list[str]:
    check = runtime.job.check
    args = [runtime.binary, *runtime.base_args, *_progress(config, runtime), "check"]
    if check.read_data:
        args.append("--read-data")
    if check.read_data_subset:
        args += ["--read-data-subset", check.read_data_subset]
    return args + check.extra_args


def command_for(config: Config, runtime: JobRuntime, operation: Operation) -> list[str]:
    match operation:
        case Operation.BACKUP:
            return backup_command(config, runtime)
        case Operation.SNAPSHOTS:
            return snapshots_command(runtime)
        case Operation.FORGET:
            return forget_command(runtime, dry_run=runtime.job.forget.dry_run)
        case Operation.PRUNE:
            return prune_command(runtime)
        case Operation.CHECK:
            return check_command(config, runtime)


def make_runtime(config: Config, name: str, binary: str, merged: MergedProfile) -> JobRuntime:
    job = config.jobs[name]
    # A Pushgateway URL already present in the profiles is the user's choice;
    # the scheduler only supplies its own when the profiles are silent.
    url = None if merged.prometheus_url else config.pushgateway.url
    return JobRuntime(
        name=name,
        job=job,
        label=config.label_of(name),
        merged=merged,
        binary=binary,
        base_args=tuple(profile_args(config, job)),
        env=build_env(config, job, url),
        pushgateway_url=merged.prometheus_url or config.pushgateway.url,
    )
