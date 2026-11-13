from __future__ import annotations

import json
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

import ads.client
import ads.models.journal as journal_module
from ads.exceptions import DoesNotExist
from ads.models.journal import Journal
from ads.query import Query

RAW: dict[str, str] = json.loads((resources.files("ads") / "data" / "journals.json").read_text("utf-8"))


@pytest.fixture(autouse=True)
def no_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise AssertionError("journal queries must not need a client")

    monkeypatch.setattr(ads.client, "get_default_client", fail)


@pytest.fixture(autouse=True)
def empty_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the packaged snapshot even if the user has downloaded fresher data."""
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path / "cache"))
    journal_module._reset()


def abbreviations(journals: list[Journal]) -> list[str]:
    return [journal.abbreviation for journal in journals]


def test_every_journal_is_loaded_in_file_order() -> None:
    journals = Journal.select().all()
    assert len(journals) == len(RAW) == Journal.select().count() == 4502
    assert abbreviations(journals) == list(RAW)
    assert {j.abbreviation: j.title for j in journals} == RAW


def test_known_journal() -> None:
    apj = Journal.get(abbreviation="ApJ")
    assert apj.title == "The Astrophysical Journal" == RAW["ApJ"]
    assert str(apj) == "ApJ"
    assert repr(apj) == "<Journal: abbreviation='ApJ'>"
    assert apj == Journal(abbreviation="ApJ")


def test_get_missing_raises_does_not_exist() -> None:
    with pytest.raises(Journal.DoesNotExist) as info:
        Journal.get(abbreviation="NoSuchJournal")
    assert isinstance(info.value, DoesNotExist)


def test_lookup() -> None:
    assert Journal.lookup("MNRAS") is Journal.get(abbreviation="MNRAS")
    assert Journal.lookup("MNRAS").title == RAW["MNRAS"]
    assert Journal.lookup("NoSuchJournal") is None


def test_contains_order_by_and_limit() -> None:
    expected = sorted((t, a) for a, t in RAW.items() if "astro" in t.lower())
    query = Journal.select().where(Journal.title.contains("astro"))
    assert query.count() == len(expected) > 10
    ordered = query.order_by(Journal.title).limit(5).all()
    assert [j.title for j in ordered] == [t for t, _ in expected[:5]]
    descending = query.order_by(Journal.title.desc()).offset(1).limit(3).all()
    assert [j.title for j in descending] == sorted((t for t, _ in expected), reverse=True)[1:4]


def test_in_and_startswith() -> None:
    query = Journal.select().where(Journal.abbreviation.in_(["MNRAS", "ApJ", "NoSuchJournal"]))
    assert abbreviations(query.all()) == [a for a in RAW if a in ("MNRAS", "ApJ")]
    starts = Journal.select().where(Journal.abbreviation.startswith("ApJ"))
    # Like is case-insensitive in local evaluation, so 'APJAS' matches too.
    assert abbreviations(starts.all()) == [a for a in RAW if a.lower().startswith("apj")]
    assert {"ApJ", "ApJS", "APJAS"} <= set(abbreviations(starts.all()))
    either = Journal.select().where((Journal.abbreviation == "ApJ") | (Journal.title == RAW["MNRAS"]))
    assert sorted(abbreviations(either.all())) == ["ApJ", "MNRAS"]


def test_select_is_a_query_and_explains_locally() -> None:
    query = Journal.select().where(Journal.abbreviation == "ApJ")
    assert isinstance(query, Query)
    assert "locally over 4502 Journal rows" in str(query.explain())


def test_import_reads_no_files() -> None:
    script = """
import os, sys
opened = []
def hook(event, args):
    if event == "open" and not isinstance(args[0], int):
        path = os.fsdecode(os.fspath(args[0]))
        if path.endswith(("journals.json", ".tsv")):
            opened.append(path)
sys.addaudithook(hook)
import ads.models.journal
assert opened == [], opened
ads.models.journal.Journal.lookup("ApJ")
assert len(opened) == 1 and opened[0].endswith("journals.json"), opened
ads.models.journal.Journal.select().all()
assert len(opened) == 1, opened
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
