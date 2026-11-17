"""Build the documentation site with Hugo and check the key pages exist.

Opt-in, because it needs Hugo (extended) and Go on PATH and, the first time,
network access to fetch the theme: set ``SCIX_TEST_DOCS=1`` to run it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.skipif(os.environ.get("SCIX_TEST_DOCS") != "1", reason="set SCIX_TEST_DOCS=1 to build the docs"),
    pytest.mark.skipif(shutil.which("hugo") is None, reason="hugo is not on PATH"),
]

KEY_PAGES = [
    "index.html",
    "getting-started/install/index.html",
    "getting-started/token-management/index.html",
    "getting-started/first-search/index.html",
    "getting-started/coming-from-ads/index.html",
    "usage/searching/index.html",
    "usage/authors/index.html",
    "usage/errors-and-limits/index.html",
    "guides/index.html",
    "reference/document-fields/index.html",
    "reference/expressions/index.html",
    "reference/api/query/index.html",
    "development/live-tests/index.html",
]


def test_site_builds_cleanly(tmp_path: Path) -> None:
    destination = tmp_path / "public"
    result = subprocess.run(
        [
            "hugo",
            "--source", str(ROOT / "docs"),
            "--destination", str(destination),
            "--gc", "--minify", "--panicOnWarning", "--printPathWarnings",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN" not in result.stdout + result.stderr, result.stdout + result.stderr
    missing = [page for page in KEY_PAGES if not (destination / page).is_file()]
    assert not missing, f"pages missing from the build: {missing}"
    assert "Coming from ads" in (destination / "getting-started/coming-from-ads/index.html").read_text(encoding="utf-8")
