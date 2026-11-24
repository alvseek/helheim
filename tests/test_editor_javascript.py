from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_editor_javascript_regressions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable: editor JavaScript regressions were NOT run")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/editors_newlines.test.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
