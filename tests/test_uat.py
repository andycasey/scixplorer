from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import scixplorer.client
import scixplorer.data as data
import scixplorer.models.uat as uat_module
from scixplorer.exceptions import DoesNotExist
from scixplorer.models.uat import UATConcept
from scixplorer.query import Query

FIXTURE = (Path(__file__).parent / "fixtures" / "uat.json").read_text("utf-8")
URL = data.DATASETS["uat"].files[0].url


def install(text: str) -> data.RefreshResult:
    """Refresh the thesaurus from ``text`` served as the upstream file."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=text))
    [result] = data.refresh("uat", transport=transport)
    return result


@pytest.fixture(autouse=True)
def thesaurus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The fixture thesaurus in an otherwise empty cache, and no API client."""

    def fail() -> None:
        raise AssertionError("UAT queries must not need a client")

    monkeypatch.setattr(scixplorer.client, "get_default_client", fail)
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path / "cache"))
    assert install(FIXTURE).ok
    yield
    data.clear("uat")


def ids(concepts: Any) -> list[int | None]:
    return [concept.id for concept in concepts]


def test_every_concept_is_loaded_in_file_order() -> None:
    concepts = UATConcept.select().all()
    assert ids(concepts) == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert UATConcept.select().count() == 9


def test_fields() -> None:
    binary = UATConcept.lookup(4)
    assert binary.to_dict() == {
        "id": 4,
        "name": "Binary stars",
        "alt_names": ("Double stars", "Binaries"),
        "parent_ids": (2,),
        "uri": "http://astrothesaurus.org/uat/4",
        "deprecated": False,
    }
    top = UATConcept.lookup(1)
    assert (top.parent_ids, top.alt_names) == ((), ("Astronomical sciences",))
    old = UATConcept.lookup(9)
    assert (old.name, old.deprecated, old.parent_ids, old.alt_names) == ("Old stars", True, (), ())
    schema = {info.name: (info.type, info.match_kind) for info in UATConcept.describe()}
    assert schema == {
        "id": ("int", "keyword"),
        "name": ("str", "text"),
        "alt_names": ("list[str]", "text"),
        "parent_ids": ("list[int]", "keyword"),
        "uri": ("str", "keyword"),
        "deprecated": ("bool", "keyword"),
    }


def test_lookup_and_get() -> None:
    assert UATConcept.lookup(2) is UATConcept.lookup("2") is UATConcept.get(id=2)
    assert UATConcept.lookup(99) is None
    assert UATConcept.lookup("Stars") is None
    assert UATConcept.lookup("") is None
    with pytest.raises(UATConcept.DoesNotExist) as info:
        UATConcept.get(id=99)
    assert isinstance(info.value, DoesNotExist)


def test_name_matching() -> None:
    # TextField: == finds the words as a phrase, ignoring case; exact() matches the whole name.
    stars = UATConcept.select().where(UATConcept.name == "STARS")
    assert ids(stars) == [2, 4, 6, 9]
    assert ids(UATConcept.select().where(UATConcept.name == "binary stars")) == [4, 6]
    assert ids(UATConcept.select().where(UATConcept.name.exact("stars"))) == [2]
    assert ids(UATConcept.select().where(UATConcept.alt_names == "double stars")) == [4]
    assert ids(UATConcept.select().where(UATConcept.name.startswith("loop"))) == [7, 8]


def test_where_order_by_limit_offset() -> None:
    current = UATConcept.select().where(UATConcept.deprecated == False)  # noqa: E712
    assert current.count() == 8
    by_name = current.order_by(UATConcept.name).limit(3).all()
    assert [c.name for c in by_name] == ["Astronomy", "Binary stars", "Eclipsing binary stars"]
    descending = current.order_by(UATConcept.name.desc()).offset(1).limit(2).all()
    assert [c.name for c in descending] == ["Stars", "Loop B"]
    assert ids(UATConcept.select().where(UATConcept.id.between(3, 5)).order_by(UATConcept.id.desc())) == [5, 4, 3]
    assert ids(UATConcept.select().where(UATConcept.parent_ids.in_([3, 7]))) == [5, 8]
    assert ids(UATConcept.select().where(UATConcept.uri == "http://astrothesaurus.org/uat/6")) == [6]


def test_parents_with_several_parents_in_file_order() -> None:
    populations = UATConcept.lookup(5)
    assert populations.parent_ids == (3, 2)
    assert ids(populations.parents) == [3, 2]
    assert UATConcept.lookup(1).parents == []
    assert ids(UATConcept(id=50, parent_ids=(2, 404)).parents) == [2]


def test_children() -> None:
    children = UATConcept.lookup(2).children
    assert isinstance(children, Query)
    assert ids(children) == [4, 5, 8]
    assert ids(children.order_by(UATConcept.name)) == [4, 8, 5]
    assert UATConcept.lookup(6).children.all() == []
    assert UATConcept().children.all() == []


def test_ancestors_nearest_first_without_duplicates() -> None:
    assert ids(UATConcept.lookup(6).ancestors) == [4, 2, 1]
    # Both parents first, then their shared parent once.
    assert ids(UATConcept.lookup(5).ancestors) == [3, 2, 1]
    assert UATConcept.lookup(1).ancestors == []


def test_descendants_nearest_first() -> None:
    assert ids(UATConcept.lookup(1).descendants) == [2, 3, 4, 5, 8, 6, 7]
    assert ids(UATConcept.lookup(3).descendants) == [5]
    assert UATConcept.lookup(6).descendants == []
    assert UATConcept().descendants == []


def test_hierarchy_with_a_cycle_terminates() -> None:
    # 7 and 8 are each other's parent; 8 is also under Stars.
    loop_a, loop_b = UATConcept.lookup(7), UATConcept.lookup(8)
    assert ids(loop_a.ancestors) == [8, 2, 1]
    assert ids(loop_b.ancestors) == [7, 2, 1]
    assert ids(loop_a.descendants) == [8]
    assert ids(loop_b.descendants) == [7]
    assert ids(loop_a.children) == [8]


def test_str_and_repr() -> None:
    stars = UATConcept.lookup(2)
    assert str(stars) == "Stars"
    assert repr(stars) == "<UATConcept: id=2 (Stars)>"
    assert repr(UATConcept()) == "<UATConcept>"
    assert "locally over 9 UATConcept rows" in str(UATConcept.select().explain())


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('{"uri": "x"}', "must be a JSON list"),
        ("[1]", "must be a JSON object"),
        ('[{"uri": "http://astrothesaurus.org/uat/x1", "name": "A"}]', "not a UAT concept URI"),
        ('[{"uri": "https://example.org/uat/1", "name": "A"}]', "not a UAT concept URI"),
        ('[{"uri": "http://astrothesaurus.org/uat/1", "name": " "}]', "concept 1 has no name"),
        ('[{"uri": "http://astrothesaurus.org/uat/1", "name": "A", "altNames": "B"}]', "altNames of concept 1"),
        ('[{"uri": "http://astrothesaurus.org/uat/1", "name": "A", "broader": ["B"]}]', "broader of concept 1"),
        (
            '[{"uri": "http://astrothesaurus.org/uat/1", "name": "A"},'
            ' {"uri": "http://astrothesaurus.org/uat/1", "name": "B"}]',
            r"listed more than once: \[1\]",
        ),
    ],
)
def test_malformed_files_are_rejected(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        uat_module._parse({"uat.json": text}.__getitem__)
    result = install(text)
    assert not result.ok and "could not be parsed" in result.message
    assert UATConcept.select().count() == 9


def test_refresh_replaces_the_thesaurus() -> None:
    concepts = json.loads(FIXTURE)
    concepts[1]["name"] = "Stars and stellar systems"
    result = install(json.dumps(concepts))
    assert result.ok and result.message == "9 records (was 9)"
    assert UATConcept.lookup(2).name == "Stars and stellar systems"
    assert UATConcept.lookup(4).parents[0].name == "Stars and stellar systems"
