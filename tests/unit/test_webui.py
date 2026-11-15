"""Exercise the page's next-runs rendering without adding a browser dependency."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_next_runs_page():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the simulated DOM test")
    script = Path(__file__).with_name("webui_next_runs.js")
    subprocess.run([node, str(script)], check=True)
