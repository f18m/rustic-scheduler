"""Shared fixtures."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from helpers import FAKE, Sandbox, docker_available, real_rustic


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short temporary path: Unix socket paths are limited to about 100 bytes."""
    path = Path(tempfile.mkdtemp(prefix="rs-", dir="/tmp")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def sandbox(short_tmp: Path) -> Sandbox:
    return Sandbox(short_tmp, str(FAKE))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    strict = os.environ.get("REQUIRE_INTEGRATION") == "1"
    have_rustic = real_rustic() is not None
    have_docker = docker_available() if any("docker" in i.keywords for i in items) else False
    for item in items:
        if "integration" in item.keywords and not have_rustic:
            if strict:
                raise pytest.UsageError("REQUIRE_INTEGRATION=1 but no Rustic binary was found")
            item.add_marker(
                pytest.mark.skip(reason="no Rustic binary; run scripts/install-rustic.sh")
            )
        if "docker" in item.keywords and not have_docker:
            if strict:
                raise pytest.UsageError("REQUIRE_INTEGRATION=1 but Docker is not available")
            item.add_marker(pytest.mark.skip(reason="Docker is not available"))
