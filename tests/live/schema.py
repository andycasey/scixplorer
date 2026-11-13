"""The published Solr schema of the ADS index, and ``scripts/schema_report.py`` to compare it with ``Document``.

The schema is fetched from the public ``adsabs/montysolr`` repository without
credentials and treated as untrusted data: its size is capped, a document
declaring a DTD or entities is refused, and it is only ever parsed as XML.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx

SCHEMA_URL = (
    "https://raw.githubusercontent.com/adsabs/montysolr/master/"
    "deploy/adsabs/server/solr/collection1/conf/schema.xml"
)

#: Largest schema accepted (the file is about 80 kB).
MAX_SCHEMA_BYTES = 2_000_000

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "schema_report.py"


def download(directory: Path) -> Path:
    """Download the schema into ``directory`` (which should be new and empty) and return its path."""
    chunks: list[bytes] = []
    size = 0
    with httpx.Client(timeout=30.0, follow_redirects=False) as http, http.stream("GET", SCHEMA_URL) as response:
        if response.status_code != 200:
            raise AssertionError(f"GET {SCHEMA_URL} answered HTTP {response.status_code}")
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_SCHEMA_BYTES:
                raise AssertionError(f"{SCHEMA_URL} is larger than {MAX_SCHEMA_BYTES} bytes; refusing it")
            chunks.append(chunk)
    content = b"".join(chunks)
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise AssertionError(f"{SCHEMA_URL} declares a DTD or entities; refusing to parse it")
    path = directory / "schema.xml"
    path.write_bytes(content)
    return path


def report_module() -> ModuleType:
    """``scripts/schema_report.py`` as a module (``scripts`` is not a package)."""
    loaded = sys.modules.get("schema_report")
    if loaded is not None and getattr(loaded, "__file__", None) == str(SCRIPT):
        return loaded
    spec = importlib.util.spec_from_file_location("schema_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it executes
    spec.loader.exec_module(module)
    return module


def sections(lines: list[str]) -> dict[str, list[str]]:
    """The report's lines grouped under its ``# heading`` lines (blank lines dropped)."""
    grouped: dict[str, list[str]] = {}
    current = ""
    for line in lines:
        if line.startswith("# "):
            current = line[2:]
            grouped[current] = []
        elif line.strip():
            grouped.setdefault(current, []).append(line)
    return grouped
