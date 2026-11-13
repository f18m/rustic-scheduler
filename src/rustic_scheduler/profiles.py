"""Inspection of the configuration Rustic itself derives from a job's profiles.

The scheduler never parses or merges profile files. It asks the pinned Rustic
binary (`rustic show-config`) for the merged result and reads a handful of keys
from it, only to detect conflicts with options the scheduler passes and to
identify the repository for locking. The output can contain secrets, so it is
kept in memory and never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .config import Config, JobConfig

_SECRETISH = re.compile(r"(?i)password|passwd|secret|token|key")


class ProfileError(Exception):
    """Rustic could not load the job's profiles."""


@dataclass(frozen=True)
class MergedProfile:
    """Facts about a job's merged Rustic configuration."""

    lock_key: str
    repository: str | None
    repo_hot: str | None
    prometheus_url: str | None
    progress_interval_set: bool
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default=())


def _get(raw: dict[str, Any], *path: str) -> Any:
    node: Any = raw
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def repository_lock_key(raw: dict[str, Any]) -> str:
    """Stable identifier of the repository a merged configuration points at."""
    repository = _get(raw, "repository") or {}
    identity = {
        "repository": repository.get("repository"),
        "repo-hot": repository.get("repo-hot"),
    }
    for section in ("options", "options-hot", "options-cold"):
        options = repository.get(section) or {}
        identity[section] = {
            key: value for key, value in sorted(options.items()) if not _SECRETISH.search(key)
        }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return f"repo-{digest[:16]}"


def analyse(name: str, job: JobConfig, label: str, raw: dict[str, Any]) -> MergedProfile:
    """Compare a merged Rustic configuration with what the scheduler will pass."""
    problems: list[str] = []
    warnings: list[str] = []
    where = f"jobs.{name}"
    repository = _get(raw, "repository", "repository")
    if not repository:
        problems.append(
            f"{where}: the profiles do not set [repository] repository, so Rustic has no "
            "repository to work on"
        )

    def conflict(found: Any, wanted: str | None, option: str, setting: str) -> None:
        if found and wanted is not None and found != wanted:
            problems.append(
                f"{where}: the profiles set {option} = {found!r} but the scheduler would pass "
                f"{wanted!r} and override it. Remove it from the profile or set "
                f"`{setting}: {found}` on the job."
            )

    conflict(_get(raw, "backup", "label"), label, "[backup] label", "label")
    conflict(_get(raw, "backup", "host"), job.host, "[backup] host", "host")
    snapshots = _get(raw, "backup", "snapshots") or []
    for index, snapshot in enumerate(snapshots):
        conflict(snapshot.get("label"), label, f"[[backup.snapshots]] #{index + 1} label", "label")
        conflict(snapshot.get("host"), job.host, f"[[backup.snapshots]] #{index + 1} host", "host")
        if snapshot.get("init") and not job.allow_init:
            problems.append(
                f"{where}: a [[backup.snapshots]] entry sets init = true (see allow_init)"
            )
    if not job.sources and not any(snapshot.get("sources") for snapshot in snapshots):
        problems.append(
            f"{where}: nothing to back up. Set `sources` on the job or define "
            "[[backup.snapshots]] sources in a profile."
        )
    if _get(raw, "backup", "init") and not job.allow_init:
        problems.append(
            f"{where}: the profiles set [backup] init = true, which lets Rustic create a new "
            "repository. Set `allow_init: true` on the job if that is intended."
        )
    if _get(raw, "global", "dry-run"):
        problems.append(
            f"{where}: the profiles set [global] dry-run = true, so no backup would be stored. "
            "Use the scheduler's `dry_run` settings for previews."
        )
    if _get(raw, "global", "no-progress"):
        warnings.append(
            f"{where}: [global] no-progress = true hides live progress; results are still reported"
        )

    forget = _get(raw, "forget") or {}
    if forget.get("prune") and not (job.forget.enabled and job.forget.prune):
        problems.append(
            f"{where}: the profiles set [forget] prune = true but `forget.prune` is not enabled "
            "on the job. Pruning deletes repository data, so it must be enabled explicitly."
        )
    profile_keeps = sorted(
        key
        for key, value in forget.items()
        if key.startswith("keep-") and value not in (None, [], False)
    )
    if job.forget.enabled:
        if job.forget.rules_from_profile and not profile_keeps:
            problems.append(
                f"{where}: forget.rules_from_profile is set but the profiles have no keep-* "
                "option in [forget]"
            )
        extra = [key for key in profile_keeps if key not in job.forget.keep]
        if job.forget.keep and extra:
            warnings.append(
                f"{where}: [forget] in the profiles also sets {', '.join(extra)}; Rustic applies "
                "these together with forget.keep from the scheduler configuration"
            )
        for section in ("forget", "snapshot-filter"):
            labels = _get(raw, section, "filter-labels") or []
            if labels and labels != [label]:
                problems.append(
                    f"{where}: [{section}] filter-labels = {labels} in the profiles differs from "
                    f"the job's snapshot scope (label {label!r}). Remove it to keep retention "
                    "scoped to this job."
                )
    return MergedProfile(
        lock_key=job.repository_lock or repository_lock_key(raw),
        repository=repository,
        repo_hot=_get(raw, "repository", "repo-hot"),
        prometheus_url=_get(raw, "global", "prometheus"),
        progress_interval_set=_get(raw, "global", "progress-interval") is not None,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


async def show_config(binary: str, profile_args: list[str], env: dict[str, str]) -> dict[str, Any]:
    """Run `rustic show-config` and return the merged configuration."""
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            *profile_args,
            "show-config",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise ProfileError(f"cannot run {binary}: {exc.strerror or exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
    except TimeoutError as exc:
        process.kill()
        raise ProfileError("`rustic show-config` did not finish within 60 seconds") from exc
    if process.returncode != 0:
        # Rustic's parse errors quote the offending line; keep only the message.
        lines = [line for line in stderr.decode(errors="replace").splitlines() if line.strip()]
        useful = [
            line.strip()
            for line in lines
            if not line.startswith(("Backtrace", "disabled backtrace", "Some additional"))
            and not re.match(r"^\s*\d*\s*\|", line)
        ]
        raise ProfileError("Rustic rejected the profiles: " + " / ".join(useful[-6:]))
    try:
        return tomllib.loads(stdout.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ProfileError(f"could not read the output of `rustic show-config`: {exc}") from exc


def profile_args(config: Config, job: JobConfig) -> list[str]:
    """`-P` arguments in the order the job lists its profiles."""
    args: list[str] = []
    for profile in job.profiles:
        args += ["-P", str(config.profile_path(profile))]
    return args
