"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import aiohttp

from . import __version__
from .app import prepare, serve
from .commands import JobRuntime, command_for, forget_command
from .config import Config, ConfigError, load_config
from .model import Operation
from .state import StateStore

DEFAULT_CONFIG = "/etc/rustic-scheduler/config.yaml"
#: Rustic commands that change a repository; the helper takes the repository lock for them.
_MUTATING = {
    "backup",
    "forget",
    "prune",
    "repair",
    "copy",
    "merge",
    "tag",
    "rewrite",
    "config",
    "key",
    "init",
}


def _fail(problems: list[str]) -> int:
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    return 2


def _load(args: argparse.Namespace) -> Config:
    return load_config(Path(args.config))


def _runtime(config: Config, job: str) -> JobRuntime:
    if job not in config.jobs:
        raise ConfigError([f"unknown job {job!r}; configured jobs: {', '.join(config.jobs)}"])
    runtimes, _, _ = asyncio.run(prepare(config))
    return runtimes[job]


def cmd_run(args: argparse.Namespace) -> int:
    return asyncio.run(serve(Path(args.config)))


def cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args)
    if args.offline:
        print(f"{args.config}: syntax and settings are valid ({len(config.jobs)} job(s)).")
        print("Profiles were not checked with Rustic (--offline).")
        return 0
    runtimes, version, warnings = asyncio.run(prepare(config))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"{args.config}: valid. Rustic {version}, {len(runtimes)} job(s).")
    for name, runtime in runtimes.items():
        enabled = [
            operation.value
            for operation in Operation
            if operation == Operation.BACKUP or getattr(runtime.job, operation.value).enabled
        ]
        print(
            f"  {name}: label={runtime.label} lock={runtime.lock_key} "
            f"operations={','.join(enabled)}"
        )
    return 0


def cmd_show_command(args: argparse.Namespace) -> int:
    config = _load(args)
    runtime = _runtime(config, args.job)
    print(shlex.join(command_for(config, runtime, Operation(args.operation))))
    return 0


def cmd_forget_preview(args: argparse.Namespace) -> int:
    """Run `forget --dry-run` for a job and list what would be removed."""
    config = _load(args)
    runtime = _runtime(config, args.job)
    forget = runtime.job.forget
    if not forget.keep and not forget.rules_from_profile:
        return _fail([f"job {args.job} has no retention rules (forget.keep or rules_from_profile)"])
    argv = forget_command(runtime, dry_run=True)
    print(f"$ {shlex.join(argv)}", file=sys.stderr)
    done = subprocess.run(argv, env=runtime.env, capture_output=True, check=False)
    if done.returncode != 0:
        sys.stderr.write(done.stderr.decode(errors="replace"))
        return 1
    removed = kept = 0
    for group in json.loads(done.stdout):
        key = group.get("group_key", {})
        print(f"group host={key.get('hostname')} label={key.get('label')} paths={key.get('paths')}")
        for item in group.get("items", []):
            snapshot = item["snapshot"]
            keep = item.get("keep")
            kept += bool(keep)
            removed += not keep
            verdict = "keep  " if keep else "REMOVE"
            reasons = ",".join(item.get("reasons", []))
            print(f"  {verdict} {snapshot['id'][:8]} {snapshot['time']}  {reasons}")
    print(
        f"Preview only, nothing was changed: {removed} snapshot(s) would be removed, {kept} kept."
    )
    if forget.prune:
        print("forget.prune is enabled: a real run would also prune repository data.")
    return 0


def cmd_rustic(args: argparse.Namespace) -> int:
    """Run Rustic by hand with a job's profiles and environment."""
    config = _load(args)
    runtime = _runtime(config, args.job)
    rest = args.rustic_args[1:] if args.rustic_args[:1] == ["--"] else args.rustic_args
    argv = [runtime.binary, *runtime.base_args, *rest]
    command = next((arg for arg in rest if not arg.startswith("-")), "")
    lock_dir = StateStore(config.scheduler.state_dir).lock_dir
    handle = None
    if command in _MUTATING and not args.no_lock and lock_dir is not None:
        lock_dir.mkdir(parents=True, exist_ok=True)
        handle = os.open(lock_dir / f"{runtime.lock_key}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("the repository is in use by the scheduler; waiting for it ...", file=sys.stderr)
            fcntl.flock(handle, fcntl.LOCK_EX)
    try:
        return subprocess.run(argv, env=runtime.env, check=False).returncode
    finally:
        if handle is not None:
            os.close(handle)


async def _control(config: Config, method: str, path: str) -> tuple[int, Any]:
    socket_path = StateStore(config.scheduler.state_dir).control_socket
    if socket_path is None or not socket_path.exists():
        raise ConfigError(
            [f"no control socket at {socket_path}; is the scheduler running with this state_dir?"]
        )
    connector = aiohttp.UnixConnector(path=str(socket_path))
    async with (
        aiohttp.ClientSession(connector=connector) as session,
        session.request(method, f"http://scheduler{path}") as response,
    ):
        return response.status, await response.json()


def cmd_trigger(args: argparse.Namespace) -> int:
    config = _load(args)
    status, body = asyncio.run(
        _control(config, "POST", f"/api/v1/jobs/{args.job}/{args.operation}/run")
    )
    print(body.get("message", body))
    if status != 202:
        return 1
    if not args.wait:
        print("Queued. This is not a result: follow it with `rustic-scheduler status`.")
        return 0
    started = time.time()
    while True:
        time.sleep(1)
        _, state = asyncio.run(_control(config, "GET", "/api/v1/status"))
        operation = state["jobs"][args.job]["operations"].get(args.operation, {})
        last = operation.get("last_result")
        skip = operation.get("last_skip")
        if operation.get("state") == "idle":
            done = last or skip or {}
            if skip and (not last or skip["finished_at"] > (last["finished_at"] or "")):
                done = skip
            outcome = done.get("outcome", "unknown")
            print(f"{args.operation} {outcome}: {done.get('detail', '')}".rstrip(": "))
            return 0 if done.get("outcome") == "succeeded" else 1
        if time.time() - started > args.timeout:
            print("still running; giving up waiting", file=sys.stderr)
            return 3


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    _, state = asyncio.run(_control(config, "GET", "/api/v1/status"))
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    print(f"ready: {state['ready']}   heartbeat: {state['heartbeat']}")
    for name, job in state["jobs"].items():
        print(f"{name} (label {job['label']})")
        if job.get("description"):
            print(f"  {job['description']}")
        snapshots = job.get("snapshots")
        if snapshots and snapshots["latest_at"]:
            size, files = snapshots["latest_bytes"], snapshots["latest_files"]
            print(
                f"  latest snapshot {snapshots['latest_at']}: "
                f"{'unknown size' if size is None else f'{size} bytes'}, "
                f"{'unknown file count' if files is None else f'{files} files'}"
            )
        for operation, info in job["operations"].items():
            last = info["last_result"] or {}
            outcome = last.get("outcome", "never run")
            reason = f" ({last['reason']})" if last.get("reason", "none") != "none" else ""
            line = f"  {operation:<9} {info['state']:<8} last: {outcome}{reason}"
            if last.get("finished_at"):
                line += f" at {last['finished_at']}"
            if info["next_run"]:
                line += f"   next: {info['next_run']}"
            print(line)
            if last.get("detail"):
                print(f"            {last['detail']}")
            if last.get("log_file"):
                print(f"            log: {last['log_file']}")
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    """Exit 0 only if the scheduler reports itself ready (for Docker HEALTHCHECK)."""
    try:
        port = _load(args).http.port
    except ConfigError:
        return 1
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=5) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, OSError):
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rustic-scheduler", description="Schedule Rustic backups from a YAML file."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("RUSTIC_SCHEDULER_CONFIG", DEFAULT_CONFIG),
        help=f"configuration file (default: $RUSTIC_SCHEDULER_CONFIG or {DEFAULT_CONFIG})",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("run", help="run the scheduler").set_defaults(func=cmd_run)

    validate = sub.add_parser("validate", help="check the configuration and exit")
    validate.add_argument(
        "--offline", action="store_true", help="do not run Rustic to check profiles"
    )
    validate.set_defaults(func=cmd_validate)

    operations = [operation.value for operation in Operation]
    show = sub.add_parser("show-command", help="print the Rustic command a job would run")
    show.add_argument("job")
    show.add_argument("operation", nargs="?", default="backup", choices=operations)
    show.set_defaults(func=cmd_show_command)

    preview = sub.add_parser(
        "forget-preview", help="show what forget would remove; changes nothing"
    )
    preview.add_argument("job")
    preview.set_defaults(func=cmd_forget_preview)

    trigger = sub.add_parser("trigger", help="ask the running scheduler to run a job now")
    trigger.add_argument("job")
    trigger.add_argument("operation", nargs="?", default="backup", choices=operations)
    trigger.add_argument("--wait", action="store_true", help="wait for the result")
    trigger.add_argument(
        "--timeout", type=float, default=86400, help="seconds to wait (with --wait)"
    )
    trigger.set_defaults(func=cmd_trigger)

    status = sub.add_parser("status", help="show the running scheduler's state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    rustic = sub.add_parser(
        "rustic", help="run Rustic by hand with a job's profiles, e.g. `rustic JOB -- snapshots`"
    )
    rustic.add_argument("job")
    rustic.add_argument(
        "--no-lock", action="store_true", help="do not wait for the repository lock"
    )
    rustic.add_argument("rustic_args", nargs=argparse.REMAINDER)
    rustic.set_defaults(func=cmd_rustic)

    sub.add_parser("healthcheck", help="exit 0 if the scheduler is ready").set_defaults(
        func=cmd_healthcheck
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        return _fail(exc.problems)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
