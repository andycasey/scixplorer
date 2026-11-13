from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import scixplorer.client
import scixplorer.data as data
import scixplorer.models.planetary as planetary_module
from scixplorer.exceptions import DoesNotExist
from scixplorer.models.planetary import PlanetaryFeature

FIXTURE = (Path(__file__).parent / "fixtures" / "planetary_features.csv").read_text("utf-8")
HEADER = "Feature ID,Clean Feature Name,Target,Feature Type,Approval Status,Approval Date\n"


def install(text: str) -> data.RefreshResult:
    """Refresh the gazetteer from ``text`` served as the upstream file."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=text))
    [result] = data.refresh("planetary_features", transport=transport)
    return result


@pytest.fixture(autouse=True)
def gazetteer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The fixture gazetteer in an otherwise empty cache, and no API client."""

    def fail() -> None:
        raise AssertionError("planetary feature queries must not need a client")

    monkeypatch.setattr(scixplorer.client, "get_default_client", fail)
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path / "cache"))
    assert install(FIXTURE).ok
    yield
    data.clear("planetary_features")


def ids(features: Any) -> list[int | None]:
    return [feature.id for feature in features]


def test_every_feature_is_loaded_in_file_order() -> None:
    assert ids(PlanetaryFeature.select()) == [2071, 1, 16115, 6981, 4, 90001, 7097]


def test_fields() -> None:
    assert PlanetaryFeature.lookup(2071).to_dict() == {
        "id": 2071,
        "name": "Gale",
        "target": "Mars",
        "feature_type": "Crater",
        "approval_status": "Approved",
        "approval_year": 1991,
    }
    aarna = PlanetaryFeature.lookup(16115)
    assert (aarna.feature_type, aarna.approval_year) == ("Crater", 2022)
    dropped = PlanetaryFeature.lookup(90001)
    assert (dropped.feature_type, dropped.approval_status, dropped.approval_year) == ("Dorsum", "Dropped", None)
    assert PlanetaryFeature.lookup(6981).feature_type == "Albedo Feature"
    schema = {info.name: (info.type, info.match_kind) for info in PlanetaryFeature.describe()}
    assert schema == {
        "id": ("int", "keyword"),
        "name": ("str", "text"),
        "target": ("str", "keyword"),
        "feature_type": ("str", "keyword"),
        "approval_status": ("str", "keyword"),
        "approval_year": ("int", "keyword"),
    }


def test_lookup_and_get() -> None:
    assert PlanetaryFeature.lookup(2071) is PlanetaryFeature.lookup("2071") is PlanetaryFeature.get(id=2071)
    assert PlanetaryFeature.lookup(5) is None
    assert PlanetaryFeature.lookup("Gale") is None
    with pytest.raises(PlanetaryFeature.DoesNotExist) as info:
        PlanetaryFeature.get(name="Olympus Mons")
    assert isinstance(info.value, DoesNotExist)


def test_where_order_by_limit() -> None:
    mars_craters = PlanetaryFeature.select().where(
        (PlanetaryFeature.target == "Mars") & (PlanetaryFeature.feature_type == "Crater")
    )
    assert ids(mars_craters) == [2071, 16115]
    assert ids(mars_craters.order_by(PlanetaryFeature.name)) == [16115, 2071]
    on_mars = PlanetaryFeature.select().where(PlanetaryFeature.target == "Mars").order_by(PlanetaryFeature.name)
    assert [f.name for f in on_mars.limit(2)] == ["Aarna", "Abalos Colles"]
    assert [f.name for f in on_mars.offset(2)] == ["Gale", "Gale Dorsa"]
    assert PlanetaryFeature.select().where(PlanetaryFeature.target == "mars").count() == 0
    recent = PlanetaryFeature.select().where(PlanetaryFeature.approval_year >= 2006).order_by(PlanetaryFeature.id)
    assert ids(recent) == [6981, 7097, 16115]
    assert ids(PlanetaryFeature.select().order_by(PlanetaryFeature.approval_year.desc()))[-1] == 90001


def test_name_matching() -> None:
    # TextField: == finds the words as a phrase, ignoring case; exact() matches the whole name.
    assert ids(PlanetaryFeature.select().where(PlanetaryFeature.name == "gale")) == [2071, 90001]
    assert ids(PlanetaryFeature.select().where(PlanetaryFeature.name.exact("GALE"))) == [2071]
    assert ids(PlanetaryFeature.select().where(PlanetaryFeature.name.startswith("Aa"))) == [1, 16115, 6981]


def test_str_and_repr() -> None:
    gale = PlanetaryFeature.lookup(2071)
    assert str(gale) == "Gale"
    assert repr(gale) == "<PlanetaryFeature: id=2071 (Gale, Mars)>"
    assert repr(PlanetaryFeature()) == "<PlanetaryFeature>"


def test_headings_are_found_by_name() -> None:
    text = (
        "Approval Date,Target,Extra,Feature  ID,Clean Feature Name,Feature Type,Approval Status\n"
        "2001,Io,x,7,Loki,Patera,Approved\n"
    )
    [loki] = planetary_module._parse({"planetary_features.csv": text}.__getitem__)
    assert loki.to_dict() == {
        "id": 7,
        "name": "Loki",
        "target": "Io",
        "feature_type": "Patera",
        "approval_status": "Approved",
        "approval_year": 2001,
    }


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("Feature ID,Name,Target\n1,A,Mars\n", "no Clean Feature Name, Feature Type, Approval Status, Approval Date"),
        (HEADER + "x1,A,Mars,Crater,Approved,2000\n", r"line 2: feature id 'x1' is not a new integer"),
        (HEADER + "1,A,Mars,Crater,Approved,2000\n1,B,Mars,Crater,Approved,2000\n", "line 3: feature id '1'"),
        (HEADER + "1,,Mars,Crater,Approved,2000\n", "feature 1 has no name or target"),
        (HEADER + "1,A,Mars\n", "line 2 has 3 columns, not 6"),
    ],
)
def test_malformed_files_are_rejected(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        planetary_module._parse({"planetary_features.csv": text}.__getitem__)
    result = install(text)
    assert not result.ok and "could not be parsed" in result.message
    assert PlanetaryFeature.select().count() == 7


def test_blank_lines_are_skipped() -> None:
    text = HEADER + "\n1,A,Mars,Crater,Approved,2000\n,,,,,\n"
    features = planetary_module._parse({"planetary_features.csv": text}.__getitem__)
    assert ids(features) == [1]
