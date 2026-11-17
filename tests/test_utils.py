from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from scixplorer.client import Request, Response
from scixplorer.exceptions import APIError
from scixplorer.utils import (
    _require_bibcodes,
    _response_field,
    flatten,
    is_valid_orcid,
    parse_bibcode,
    to_bibcodes,
)


class FakeLibrary:
    """Has ``bibcodes`` but must never be iterated (that would fetch documents)."""

    def __init__(self, *bibcodes: str) -> None:
        self.bibcodes = list(bibcodes)

    def __iter__(self) -> Iterator[Any]:
        raise AssertionError("a library must not be iterated")


def doc(bibcode: Any) -> SimpleNamespace:
    return SimpleNamespace(bibcode=bibcode)


# parse_bibcode


@pytest.mark.parametrize(
    "bibcode, expected",
    [
        ("2015ApJ...808...16N", ("2015", "ApJ", "808", "", "16", "N")),
        ("2000A&AS..143...41K", ("2000", "A&AS", "143", "", "41", "K")),
        ("2015ApJ...808L..16N", ("2015", "ApJ", "808", "L", "16", "N")),
        ("1996PhRvB..5411169K", ("1996", "PhRvB", "54", "", "11169", "K")),
        ("2020arXiv200101234A", ("2020", "arXiv", "2001", "", "01234", "A")),
        ("2016PhRvL.116f1102A", ("2016", "PhRvL", "116", "f", "1102", "A")),
        ("2020svos.conf...11E", ("2020", "svos", "conf", "", "11", "E")),
        ("2018A&A...616A...1G", ("2018", "A&A", "616", "A", "1", "G")),
    ],
)
def test_parse_bibcode(bibcode: str, expected: tuple[str, ...]) -> None:
    keys = (
        "year",
        "journal_abbreviation",
        "volume",
        "qualifier",
        "page_number",
        "first_letter_of_last_name",
    )
    assert parse_bibcode(bibcode) == dict(zip(keys, expected))


@pytest.mark.parametrize(
    "bibcode",
    [
        "",
        "2015ApJ...808...16",  # 18 characters
        "2015ApJ...808...16NX",  # 20 characters
        " 2015ApJ...808...16N",
        "2015ApJ...808...16n",  # author initial must be upper case
        "15ApJ.....808...16N",  # year
        "2015ApJ...808...1aN",  # page
        "10.1088/0004-637X/808/1/16",
    ],
)
def test_parse_bibcode_rejects(bibcode: str) -> None:
    with pytest.raises(ValueError, match="invalid bibcode"):
        parse_bibcode(bibcode)


def test_parse_bibcode_needs_str() -> None:
    with pytest.raises(TypeError, match="int"):
        parse_bibcode(2015)  # type: ignore[arg-type]


# to_bibcodes


def test_to_bibcodes_strings_are_stripped() -> None:
    assert to_bibcodes(" 2015ApJ...808...16N\n", "X") == ["2015ApJ...808...16N", "X"]


def test_to_bibcodes_accepts_identifiers_that_are_not_bibcodes() -> None:
    assert to_bibcodes("10.1088/0004-637X/808/1/16", "arXiv:1501.07604") == [
        "10.1088/0004-637X/808/1/16",
        "arXiv:1501.07604",
    ]


def test_to_bibcodes_documents() -> None:
    assert to_bibcodes(doc("A"), doc(" B ")) == ["A", "B"]


def test_to_bibcodes_library_is_not_iterated() -> None:
    assert to_bibcodes(FakeLibrary("A", "B")) == ["A", "B"]


def test_to_bibcodes_iterables() -> None:
    assert to_bibcodes(["A", "B"], ("C",), {"D"}, (x for x in ["E", doc("F")])) == list("ABCDEF")


def test_to_bibcodes_nested_mixture() -> None:
    items = ["A", [doc("B"), ("C", [FakeLibrary("D", "E")])], iter([doc("F")])]
    assert to_bibcodes(items, "G") == list("ABCDEFG")


def test_to_bibcodes_deduplicates_keeping_first_occurrence() -> None:
    assert to_bibcodes("B", ["A", doc("B")], FakeLibrary("C", "A"), " C") == ["B", "A", "C"]


def test_to_bibcodes_empty() -> None:
    assert to_bibcodes() == []
    assert to_bibcodes([], ()) == []


@pytest.mark.parametrize("item", ["", "   ", ["A", ""], doc(" ")])
def test_to_bibcodes_rejects_empty_strings(item: Any) -> None:
    with pytest.raises(ValueError, match="empty bibcode"):
        to_bibcodes(item)


def test_to_bibcodes_rejects_document_without_bibcode() -> None:
    with pytest.raises(ValueError, match="SimpleNamespace object has no bibcode"):
        to_bibcodes(["A", doc(None)])


@pytest.mark.parametrize(
    "item, name",
    [
        (42, "int"),
        (None, "NoneType"),
        (["A", 3.5], "float"),
        (b"2015ApJ...808...16N", "bytes"),
        (doc(123), "int"),
        (FakeLibrary("A", 7), "int"),  # type: ignore[arg-type]
        (SimpleNamespace(bibcodes="A"), "str"),
        (SimpleNamespace(bibcodes=None), "NoneType"),
    ],
)
def test_to_bibcodes_rejects_other_types(item: Any, name: str) -> None:
    with pytest.raises(TypeError, match=rf"\b{name}\b"):
        to_bibcodes(item)


def test_require_bibcodes() -> None:
    assert _require_bibcodes(("A", ["A", "B"])) == ["A", "B"]
    with pytest.raises(ValueError, match="no documents given"):
        _require_bibcodes(())
    with pytest.raises(ValueError, match="no documents given"):
        _require_bibcodes(([], FakeLibrary()))


# flatten


class Aff:
    def __init__(self, country: str) -> None:
        self.country = country


def test_flatten_affiliation_idiom() -> None:
    au, de, us = Aff("Australia"), Aff("Germany"), Aff("USA")
    affiliation = [de, None, (us, au), None]
    assert flatten(affiliation) == [de, us, au]
    assert any(aff.country == "Australia" for aff in flatten(affiliation))
    assert flatten(None) == []


def test_flatten_containers() -> None:
    nested = [1, (2, [3, {4}]), {"a": 5, "b": [6, None]}, (x for x in [7, [8]]), frozenset([9])]
    assert flatten(nested) == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_flatten_keeps_leaves_whole() -> None:
    library = FakeLibrary("A")
    assert flatten(["abc", b"de", library, 0, False, ""]) == ["abc", b"de", library, 0, False, ""]
    assert flatten("abc") == ["abc"]
    assert flatten(3) == [3]


# _response_field

REQUEST = Request("POST", "/export/bibtex", json={"bibcode": ["A"]})


def test_response_field() -> None:
    response = Response(200, {}, {"links": {"records": [1]}})
    assert _response_field(REQUEST, response, "links", "records") == [1]


@pytest.mark.parametrize(
    "data, missing",
    [
        ({"msg": "ok"}, r"\['links'\]"),
        ({"links": {}}, r"\['links'\]\['records'\]"),
        ({"links": "x"}, r"\['links'\]\['records'\]"),
        ("<html>", r"\['links'\] in str body"),
        (None, r"NoneType body"),
    ],
)
def test_response_field_missing(data: Any, missing: str) -> None:
    response = Response(200, {}, data)
    with pytest.raises(APIError, match=rf"POST /export/bibtex: .*{missing}") as info:
        _response_field(REQUEST, response, "links", "records")
    assert info.value.status_code == 200
    assert info.value.response is response


@pytest.mark.parametrize(
    "orcid",
    [
        "0000-0002-1825-0097",  # the iD in ORCID's own examples
        "0000-0001-5109-3700",
        "0000-0002-1694-233X",  # check digit X
        "0000-0002-1694-233x",
    ],
)
def test_is_valid_orcid(orcid: str) -> None:
    assert is_valid_orcid(orcid)
    assert is_valid_orcid(orcid, strict=True)


@pytest.mark.parametrize(
    "orcid",
    [
        "0000000218250097",  # no hyphens
        "000000021694233X",
        "0000-00021825-0097",  # one hyphen missing
        "00000002-18250097",
    ],
)
def test_is_valid_orcid_allows_missing_hyphens_unless_strict(orcid: str) -> None:
    assert is_valid_orcid(orcid)
    assert not is_valid_orcid(orcid, strict=True)


@pytest.mark.parametrize(
    "orcid",
    [
        "0000-0002-1825-0098",  # the last digit changed
        "0000-0002-1852-0097",  # two neighbouring digits swapped
        "0000-0002-1825-009",  # too short
        "0000000218250098",  # no hyphens, and the last digit changed
        "https://orcid.org/0000-0002-1825-0097",  # a URL, not the bare iD
        " 0000-0002-1825-0097",
        "0000-0002-1825-009Y",
        "",
    ],
)
def test_is_valid_orcid_rejects(orcid: str) -> None:
    assert not is_valid_orcid(orcid)
    assert not is_valid_orcid(orcid, strict=True)


def test_is_valid_orcid_rejects_other_types() -> None:
    assert not is_valid_orcid(None)  # type: ignore[arg-type]
    assert not is_valid_orcid(218250097)  # type: ignore[arg-type]
