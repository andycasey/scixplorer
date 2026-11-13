"""The whole test run reads reference data from an empty cache (tests/conftest.py:isolated_cache)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import scixplorer.data as data


def test_the_run_uses_an_empty_cache(isolated_cache: Path) -> None:
    assert os.environ["SCIX_CACHE_DIR"] == str(isolated_cache)
    assert data.cache_dir() == isolated_cache
    assert list(isolated_cache.iterdir()) == []
    sources = {status.name: status.source for status in data.status()}
    assert sources["affiliations"] == sources["journals"] == "packaged"


def test_a_test_can_set_its_own_cache(isolated_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path))
    assert data.cache_dir() == tmp_path != isolated_cache
