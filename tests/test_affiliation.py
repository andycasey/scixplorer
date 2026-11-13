from __future__ import annotations

import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

import scixplorer.client
import scixplorer.models.affiliation as affiliation_module
from scixplorer.evaluate import matches
from scixplorer.exceptions import DoesNotExist
from scixplorer.models.affiliation import Affiliation
from scixplorer.query import Query


def raw_rows(name: str) -> list[list[str]]:
    """Data rows of a packaged TSV file, parsed independently of the module under test."""
    lines = (resources.files("scixplorer") / "data" / name).read_text("utf-8").splitlines()[1:]
    return [[value.strip() for value in line.split("\t")] for line in lines if line.strip()]


AFFILIATIONS = raw_rows("affiliations.tsv")  # parent, child, abbreviation, canonical name
# ISO code, country, parent ('' for none), child, abbreviation, canonical name. Counted from
# the end, the child is always third: one upstream row has an extra empty field before it.
COUNTRIES = raw_rows("affiliations_country.tsv")
CHILD_IDS = list(dict.fromkeys(row[1] for row in AFFILIATIONS))
PARENTS: dict[str, list[str]] = {child: [] for child in CHILD_IDS}
for _parent, _child, *_ in AFFILIATIONS:
    if _parent != "0" and _parent not in PARENTS[_child]:
        PARENTS[_child].append(_parent)


def expected_country(child: str) -> tuple[str, str] | None:
    """The first (country, code) listed for ``child`` in the country file."""
    return next(((row[1], row[0]) for row in COUNTRIES if row[-3] == child and row[1]), None)


def ids(affiliations: list[Affiliation]) -> list[str]:
    return [affiliation.id for affiliation in affiliations]


@pytest.fixture(autouse=True)
def no_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> None:
        raise AssertionError("affiliation queries must not need a client")

    monkeypatch.setattr(scixplorer.client, "get_default_client", fail)


@pytest.fixture(autouse=True)
def empty_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the packaged snapshot even if the user has downloaded fresher data."""
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path / "cache"))
    affiliation_module._reset()


def test_one_affiliation_per_child_id() -> None:
    affiliations = Affiliation.select().all()
    assert len(AFFILIATIONS) == len(COUNTRIES) == 7814
    assert len(affiliations) == len(CHILD_IDS) == Affiliation.select().count() == 7382
    assert ids(affiliations) == CHILD_IDS


def test_parent_ids_merge_every_row_in_file_order() -> None:
    assert sum(1 for parents in PARENTS.values() if len(parents) > 1) == 282
    assert {a.id: a.parent_ids for a in Affiliation.select()} == {
        child: tuple(parents) for child, parents in PARENTS.items()
    }
    supa = Affiliation.lookup("A11755")
    assert supa.abbreviation == "SUPA"
    assert supa.parent_ids == tuple(PARENTS["A11755"])
    assert len(supa.parent_ids) == 7


def test_disagreeing_rows_keep_the_first_names() -> None:
    rows = [row for row in AFFILIATIONS if row[1] == "A00075"]
    assert len({row[3] for row in rows}) > 1
    ames = Affiliation.lookup("A00075")
    assert (ames.abbreviation, ames.canonical_name) == (rows[0][2], rows[0][3])


def test_surrounding_whitespace_is_stripped() -> None:
    raw = (resources.files("scixplorer") / "data" / "affiliations.tsv").read_text("utf-8")
    assert "0\tA06826\tDLSU\t De La Salle University, Philippines\n" in raw
    assert Affiliation.lookup("A06826").canonical_name == "De La Salle University, Philippines"


def test_countries_and_codes() -> None:
    affiliations = Affiliation.select().all()
    assert {a.id: (a.country, a.country_code) for a in affiliations} == {
        child: expected_country(child) or (None, None) for child in CHILD_IDS
    }
    assert sum(1 for a in affiliations if a.country is not None) == 7382
    assert sum(1 for a in affiliations if a.country_code is not None) == 7382
    assert Affiliation.select().where(Affiliation.country_code.like("*")).count() == 7382


def test_countries_are_spelled_out_as_upstream_spells_them() -> None:
    assert (Affiliation.lookup("A00007").country, Affiliation.lookup("A00007").country_code) == ("United Kingdom", "GB")
    assert Affiliation.select().where(Affiliation.country == "USA").count() == 0
    assert Affiliation.select().where(Affiliation.country == "United States").count() == (
        Affiliation.select().where(Affiliation.country_code == "US").count()
    ) > 1000


def test_row_with_an_extra_empty_field_is_read() -> None:
    # Upstream has 'PH, Philippines, <empty>, <empty>, A06826, ...': one tab too many.
    [row] = [row for row in COUNTRIES if "A06826" in row]
    assert len(row) == 7 and row[2:4] == ["", ""]
    assert (Affiliation.lookup("A06826").country, Affiliation.lookup("A06826").country_code) == ("Philippines", "PH")


def test_country_through_the_parentless_fallback_key() -> None:
    # Harvard is top-level: parent '0' in affiliations.tsv but '' in the country file,
    # so the exact 'parent|child' key misses and '|child' supplies the country.
    pairs = {(row[2], row[3]) for row in COUNTRIES}
    assert ["0", "A00211", "Harvard U", "Harvard University, Massachusetts"] in AFFILIATIONS
    assert ("0", "A00211") not in pairs and ("", "A00211") in pairs
    assert (Affiliation.lookup("A00211").country, Affiliation.lookup("A00211").country_code) == (
        "United States",
        "US",
    )


def test_parents_and_parent() -> None:
    supa = Affiliation.lookup("A11755")
    assert ids(supa.parents) == PARENTS["A11755"]
    assert supa.parent.id == PARENTS["A11755"][0]
    harvard = Affiliation.lookup("A00211")
    assert harvard.parents == [] and harvard.parent is None
    orphan = Affiliation(id="X1", parent_ids=("A00211", "NoSuchId"))
    assert ids(orphan.parents) == ["A00211"]


def test_children() -> None:
    expected = [child for child in CHILD_IDS if "A00007" in PARENTS[child]]
    aberdeen = Affiliation.lookup("A00007")
    assert isinstance(aberdeen.children, Query)
    assert ids(aberdeen.children.all()) == expected
    expected_many = [child for child in CHILD_IDS if "A00211" in PARENTS[child]]
    assert len(expected_many) > 5
    by_name = Affiliation.lookup("A00211").children.order_by(Affiliation.abbreviation.desc())
    by_abbreviation = {a.id: a.abbreviation for a in Affiliation.select()}
    assert ids(by_name.all()) == sorted(expected_many, key=by_abbreviation.__getitem__, reverse=True)
    assert Affiliation.lookup("A99999").children.all() == []


def test_siblings() -> None:
    mine = set(PARENTS["A11755"])
    expected = [c for c in CHILD_IDS if c != "A11755" and mine & set(PARENTS[c])]
    siblings = Affiliation.lookup("A11755").siblings
    assert isinstance(siblings, Query)
    assert ids(siblings.all()) == expected
    assert len(expected) > 10
    top_level = Affiliation.lookup("A00211").siblings
    assert isinstance(top_level, Query)
    assert top_level.all() == [] and top_level.count() == 0


def test_where_order_by_limit_and_in() -> None:
    australian = [child for child in CHILD_IDS if expected_country(child) == ("Australia", "AU")]
    query = Affiliation.select().where(Affiliation.country_code == "AU")
    assert query.count() == len(australian) > 10
    assert Affiliation.select().where(Affiliation.country == "Australia").all() == query.all()
    by_abbreviation = {a.id: a.abbreviation for a in Affiliation.select()}
    first_three = query.order_by(Affiliation.abbreviation).limit(3).all()
    assert ids(first_three) == sorted(australian, key=by_abbreviation.__getitem__)[:3]
    picked = Affiliation.select().where(Affiliation.id.in_(["A00211", "A00007", "nope"]))
    assert ids(picked.all()) == [c for c in CHILD_IDS if c in ("A00211", "A00007")]
    harvards = Affiliation.select().where(Affiliation.canonical_name.contains("Harvard"))
    assert "A00211" in ids(harvards.all())


def test_get_and_lookup() -> None:
    assert Affiliation.get(id="A00211") is Affiliation.lookup("A00211")
    assert Affiliation.lookup("nope") is None
    with pytest.raises(Affiliation.DoesNotExist) as info:
        Affiliation.get(id="nope")
    assert isinstance(info.value, DoesNotExist)


def test_str_and_repr() -> None:
    harvard = Affiliation.lookup("A00211")
    assert str(harvard) == "Harvard University, Massachusetts"
    assert repr(harvard) == "<Affiliation: id='A00211' (Harvard U)>"
    assert repr(Affiliation()) == "<Affiliation>"


def test_loader_merges_and_resolves_countries() -> None:
    files: dict[str, str] = {}
    files["affiliations.tsv"] = (
        b"Parent\tChild\tAbbrev\tCanonical Name\r\n"
        b"0\tA1\tOne \tFirst One\r\n"
        b"A1\tA2\tTwo\tSecond\r\n"
        b"A3\tA2\tTwo-b\tSecond (b)\r\n"
        b"A1\tA2\tTwo\tSecond\r\n"
        b"0\tA3\tThree\tThird\r\n"
        b"A3\tA4\tFour\tFourth\r\n"
        b"\r\n"
    ).decode()
    files["affiliations_country.tsv"] = (
        b"Country\t[Aff_id_parent]\tAff_id_child\tInst\tAffiliation\r\n"
        b"Narnia\t\tA1\tOne\tFirst One\r\n"
        b"\tA1\tA2\tTwo\tSecond\r\n"
        b"Gondor\tA3\tA2\tTwo-b\tSecond (b)\r\n"
        b"Rohan \tA3\t\tThree\tThird\r\n"
    ).decode()
    loaded = {a.id: a for a in affiliation_module._parse(files.__getitem__)}
    assert list(loaded) == ["A1", "A2", "A3", "A4"]
    # The older layout has no ISO codes.
    assert all(a.country_code is None for a in loaded.values())
    assert (loaded["A1"].abbreviation, loaded["A1"].country, loaded["A1"].parent_ids) == ("One", "Narnia", ())
    # Empty country for (A1, A2): the later (A3, A2) row supplies one; names come from the first row.
    assert (loaded["A2"].abbreviation, loaded["A2"].canonical_name) == ("Two", "Second")
    assert (loaded["A2"].country, loaded["A2"].parent_ids) == ("Gondor", ("A1", "A3"))
    assert loaded["A3"].country is None
    # No (A3, A4) or (, A4) row: the 'parent|' key supplies the parent's country.
    assert (loaded["A4"].country, loaded["A4"].parent_ids) == ("Rohan", ("A3",))
    assert not matches(Affiliation.country.like("*"), loaded["A3"])


def test_loader_reads_the_iso_layout_and_fits_rows_without_guessing() -> None:
    files = {
        "affiliations.tsv": "Parent\tChild\tAbbrev\tCanonical Name\n"
        "0\tA1\tOne\tFirst\n0\tA2\tTwo\tSecond\n0\tA3\tThree\tThird\n0\tA4\tFour\tFourth\n",
        "affiliations_country.tsv": "ISO Country\tCountry\tParent\tChild\tAbbrev\tCanonical Name\n"
        # An extra empty field next to the empty parent: dropping either gives the same row.
        "NA\tNarnia\t\t\tA1\tOne\tFirst\n"
        # Short rows are padded.
        "GO\tGondor\t\tA2\n"
        # Too many fields and the empty ones are not adjacent: which to drop is a guess, so skipped.
        "RO\tRohan\t\tA3\t\tThree\t\n"
        # No code.
        "\tMordor\t\tA4\tFour\tFourth\n",
    }
    loaded = {a.id: (a.country, a.country_code) for a in affiliation_module._parse(files.__getitem__)}
    assert loaded == {
        "A1": ("Narnia", "NA"),
        "A2": ("Gondor", "GO"),
        "A3": (None, None),
        "A4": ("Mordor", None),
    }


def test_import_reads_no_files() -> None:
    script = """
import os, sys
opened = []
def hook(event, args):
    if event == "open" and not isinstance(args[0], int):
        path = os.fsdecode(os.fspath(args[0]))
        if path.endswith(("journals.json", ".tsv")):
            opened.append(os.path.basename(path))
sys.addaudithook(hook)
import scixplorer.models.affiliation
assert opened == [], opened
scixplorer.models.affiliation.Affiliation.lookup("A00211").children.all()
assert sorted(opened) == ["affiliations.tsv", "affiliations_country.tsv"], opened
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
