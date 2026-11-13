"""Type-check ``tests/typing_checks.py`` with mypy."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(importlib.util.find_spec("mypy") is None, reason="mypy is not installed")
def test_static_types(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--strict",
        "--warn-unused-ignores",
        "--cache-dir",
        str(tmp_path),
        str(ROOT / "tests" / "typing_checks.py"),
    ]
    # Resolve ``ads`` from the source tree, so the check does not depend on how
    # (or whether) the package is installed.
    env = {**os.environ, "MYPYPATH": str(ROOT / "src")}
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
