"""Helpers shared by unit and integration tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from rustic_scheduler.app import prepare
from rustic_scheduler.config import Config, parse_config
from rustic_scheduler.engine import Engine
from rustic_scheduler.metrics import Metrics
from rustic_scheduler.state import StateStore

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_rustic.py"

SUMMARY = {
    "message_type": "summary",
    "files_new": 3,
    "files_changed": 1,
    "files_unmodified": 5,
    "dirs_new": 1,
    "dirs_changed": 0,
    "dirs_unmodified": 2,
    "data_blobs": 4,
    "tree_blobs": 2,
    "data_added": 1000,
    "data_added_packed": 800,
    "total_files_processed": 9,
    "total_bytes_processed": 5000,
    "total_duration": 0.5,
    "snapshot_id": "a" * 64,
}
STATUS = {
    "message_type": "status",
    "seconds_elapsed": 2,
    "seconds_remaining": 8,
    "percent_done": 0.2,
    "total_bytes": 5000,
    "bytes_done": 1000,
}


def snapshot(label: str, when: str, ident: str = "b", host: str = "h") -> dict[str, Any]:
    return {
        "time": when,
        "label": label,
        "hostname": host,
        "paths": ["/data"],
        "tags": [],
        "id": ident * 64,
        "summary": {"total_bytes_processed": 4242, "total_files_processed": 17},
    }


def snapshots_json(*snapshots: dict[str, Any]) -> str:
    return json.dumps([{"group_key": {}, "snapshots": list(snapshots)}] if snapshots else [])


def forget_json(*items: tuple[dict[str, Any], bool]) -> str:
    return json.dumps(
        [
            {
                "group_key": {},
                "items": [
                    {"snapshot": snap, "keep": keep, "reasons": ["last"] if keep else []}
                    for snap, keep in items
                ],
            }
        ]
    )


class Sandbox:
    """Builds configurations and engines around the fake or the real Rustic."""

    def __init__(self, root: Path, binary: str) -> None:
        self.root = root
        self.binary = binary
        self.profiles = root / "profiles"
        self.profiles.mkdir()
        self.state_dir = root / "state"
        self.plan_file = root / "plan.json"
        self.journal_file = root / "journal.jsonl"
        self.plan: dict[str, Any] = {}
        self.write_plan({})
        self.profile("repo", f'[repository]\nrepository = "{root}/repo"\n')

    def profile(self, name: str, text: str) -> Path:
        path = self.profiles / f"{name}.toml"
        path.write_text(text)
        return path

    def write_plan(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        self.plan_file.write_text(json.dumps(plan))

    def journal(self, event: str | None = "start") -> list[dict[str, Any]]:
        if not self.journal_file.exists():
            return []
        entries = [json.loads(line) for line in self.journal_file.read_text().splitlines()]
        entries = [e for e in entries if e["command"] != "show-config"]
        return [e for e in entries if event is None or e["event"] == event]

    def commands(self) -> list[str]:
        return [entry["key"] for entry in self.journal()]

    def config(self, jobs: dict[str, Any] | None = None, **sections: Any) -> Config:
        raw: dict[str, Any] = {
            "scheduler": {"timezone": "UTC", "state_dir": str(self.state_dir), "stop_grace": 2}
            | sections.pop("scheduler", {}),
            "rustic": {
                "binary": self.binary,
                "profile_dir": str(self.profiles),
                "progress_interval": 0.1,
                "env": {
                    "FAKE_RUSTIC_PLAN": str(self.plan_file),
                    "FAKE_RUSTIC_JOURNAL": str(self.journal_file),
                },
            }
            | sections.pop("rustic", {}),
            "jobs": jobs or {"docs": {"profiles": ["repo"], "sources": ["/data"]}},
            **sections,
        }
        self.last_raw = raw
        return parse_config(raw)

    async def engine(self, config: Config | None = None, **kwargs: Any) -> Engine:
        config = config or self.config()
        runtimes, _, _ = await prepare(config)
        state = StateStore(config.scheduler.state_dir, config.scheduler.run_logs_per_operation)
        return Engine(config, runtimes, Metrics(), state, **kwargs)


def metric(metrics: Metrics, name: str, labels: dict[str, str] | None = None) -> float | None:
    """Current value of a sample, or None when the series is absent.

    Tests write the label as ``job``; the exported name is ``backup_job``.
    """
    renamed = {
        ("backup_job" if key == "job" else key): value for key, value in (labels or {}).items()
    }
    return metrics.registry.get_sample_value(name, renamed)


def real_rustic() -> str | None:
    candidates = [os.environ.get("RUSTIC_BIN"), str(ROOT / ".tools" / "rustic")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
