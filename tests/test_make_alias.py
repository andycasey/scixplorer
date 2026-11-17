"""Tests for scripts/make_alias.py, which writes the ``sciexplorer`` alias project."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import scixplorer

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_alias.py"


@pytest.fixture(scope="module")
def make_alias() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_alias", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_alias"] = module
    spec.loader.exec_module(module)
    return module


def test_alias_installs_exactly_this_version(make_alias: ModuleType, tmp_path: Path) -> None:
    """The alias has the version of scixplorer and depends on exactly that version."""
    assert make_alias.main([str(tmp_path / "alias")]) == 0
    pyproject = (tmp_path / "alias" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "sciexplorer"' in pyproject
    assert f'version = "{scixplorer.__version__}"' in pyproject
    assert f'dependencies = ["scixplorer=={scixplorer.__version__}"]' in pyproject


def test_alias_ships_no_code(make_alias: ModuleType, tmp_path: Path) -> None:
    """The project is a pyproject.toml and a README: nothing that could be imported."""
    make_alias.write_alias(tmp_path, version="9.9.9")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["README.md", "pyproject.toml"]
    assert 'dependencies = ["scixplorer==9.9.9"]' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "bypass-selection = true" in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "pip install scixplorer" in (tmp_path / "README.md").read_text(encoding="utf-8")
