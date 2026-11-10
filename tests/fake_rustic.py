#!/usr/bin/env python3
"""Stand-in for the Rustic binary, driven by a JSON plan (unit tests only).

Plan file (path in $FAKE_RUSTIC_PLAN): maps a command key to a behaviour:
  {"backup": {"exit": 0, "stdout": [...], "stderr": [...], "sleep": 0.0,
              "ignore_term": false}}
A key may be "<command>:dry-run" for invocations carrying --dry-run, and a
behaviour may be a list, consumed one invocation at a time (the last repeats).
Every invocation is appended to $FAKE_RUSTIC_JOURNAL as a JSON line.
"""

import json
import os
import signal
import sys
import time

COMMANDS = {"backup", "snapshots", "forget", "prune", "check", "show-config", "init", "restore"}


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print("rustic v0.11.4")
        return 0
    command = next((arg for arg in argv if arg in COMMANDS), "")
    with open(os.environ["FAKE_RUSTIC_PLAN"]) as handle:
        plan = json.load(handle)
    key = f"{command}:dry-run" if "--dry-run" in argv and f"{command}:dry-run" in plan else command
    behaviour = plan.get(key, {})
    journal = os.environ.get("FAKE_RUSTIC_JOURNAL")
    count = 0
    if journal:
        try:
            with open(journal) as handle:
                count = sum(
                    1 for e in map(json.loads, handle) if e["key"] == key and e["event"] == "start"
                )
        except FileNotFoundError:
            pass
    if isinstance(behaviour, list):
        behaviour = behaviour[min(count, len(behaviour) - 1)]

    def log(event: str) -> None:
        if journal:
            with open(journal, "a") as handle:
                entry = {
                    "event": event,
                    "key": key,
                    "command": command,
                    "argv": argv,
                    "time": time.time(),
                    "env_prometheus": os.environ.get("RUSTIC_PROMETHEUS"),
                }
                handle.write(json.dumps(entry) + "\n")

    def on_term(signum, frame):  # mimic Rustic 0.11.4: log, then exit 0
        sys.stderr.write("[INFO] Ctrl-C received, shutting down...\n")
        sys.stderr.flush()
        log("interrupted")
        os._exit(0)

    if behaviour.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
    log("start")
    if command == "show-config":
        sys.stdout.write(behaviour.get("toml", '[repository]\nrepository = "/fake/repo"\n'))
        return behaviour.get("exit", 0)
    for line in behaviour.get("stderr", []):
        sys.stderr.write(line + "\n")
    sys.stderr.flush()
    out = behaviour.get("stdout", [])
    lines = out if isinstance(out, list) else [out]
    for line in lines:
        sys.stdout.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        sys.stdout.flush()
        time.sleep(behaviour.get("line_delay", 0))
    time.sleep(behaviour.get("sleep", 0))
    log("end")
    return behaviour.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
