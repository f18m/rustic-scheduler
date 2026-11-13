"""Fixtures for tests that run the real, pinned Rustic binary on throwaway data."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from helpers import Sandbox, real_rustic


class RealSandbox(Sandbox):
    """A disposable local repository plus helpers to look inside it."""

    def __init__(self, root: Path) -> None:
        binary = real_rustic()
        assert binary is not None
        super().__init__(root, binary)
        self.repo = root / "repo"
        self.password_file = root / "password"
        self.password_file.write_text("integration-test-password")
        self.password_file.chmod(0o600)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "a.txt").write_text("alpha\n")
        (self.source / "b.txt").write_text("beta\n" * 1000)
        self.home = root / "home"
        self.home.mkdir()
        self.profile(
            "repo",
            f'[repository]\nrepository = "{self.repo}"\n'
            f'password-file = "{self.password_file}"\nno-cache = true\n',
        )
        self.rustic("init")

    @property
    def env(self) -> dict[str, str]:
        # An isolated HOME keeps the developer's own Rustic profiles out of the tests.
        return {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
        }

    def rustic(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, "-P", str(self.profiles / "repo.toml"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
        )

    def snapshots(self, label: str | None = None) -> list[dict[str, Any]]:
        args = ["snapshots", "--json"] + (["--filter-label", label] if label else [])
        groups = json.loads(self.rustic(*args).stdout)
        return sorted((s for g in groups for s in g["snapshots"]), key=lambda s: s["time"])

    def files_in(self, snapshot_id: str) -> list[str]:
        return self.rustic("ls", snapshot_id).stdout.split()

    def job(self, **overrides: Any) -> dict[str, Any]:
        return {"profiles": ["repo"], "sources": [str(self.source)], "host": "testhost"} | overrides

    def config(self, jobs: dict[str, Any] | None = None, **sections: Any):  # type: ignore[override]
        rustic = {"env": self.env} | sections.pop("rustic", {})
        return super().config(jobs or {"docs": self.job()}, rustic=rustic, **sections)


@pytest.fixture
def real(short_tmp: Path) -> RealSandbox:
    return RealSandbox(short_tmp)
