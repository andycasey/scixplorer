"""Tests for the default collection (ads.config.collection) in document searches."""

from __future__ import annotations

from typing import Any

import pytest
from search_fakes import FakeSearch, bibcodes, make_docs

from ads import _config
from ads.exceptions import LazyLoadWarning
from ads.expr import Expr
from ads.models.document import Document
from ads.models.library import Library
from ads.search import SearchPlan, plan, to_solr

ASTRONOMY = 'database:"astronomy"'
BOTH = 'database:("astronomy" OR "physics")'


@pytest.fixture
def astronomy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "collection", "astronomy")


@pytest.fixture
def page_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "page_size", 200)


def plan_of(expr: Expr | None = None, **kwargs: Any) -> SearchPlan:
    query = Document.select()
    if expr is not None:
        query = query.where(expr)
    return plan(query, **kwargs)


def collection_notes(search: SearchPlan) -> list[str]:
    return [note for note in search.notes if "ads.config.collection" in note]


# Applied.


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (None, ASTRONOMY),
        (Document.year == 2020, f"year:2020 AND {ASTRONOMY}"),
        ((Document.year == 2020) & (Document.title == "star"), f'year:2020 AND title:"star" AND {ASTRONOMY}'),
        ((Document.year == 2020) | (Document.year == 2021), f"(year:2020 OR year:2021) AND {ASTRONOMY}"),
        (Document.year != 2020, f"-year:2020 AND {ASTRONOMY}"),
        (Document.journal == "ApJ", f'bibstem:"ApJ" AND {ASTRONOMY}'),
        (Document.journal.title == "The Astrophysical Journal", f'bibstem:"ApJ" AND {ASTRONOMY}'),
        (Document.citations(Document.bibcode == "X"), f'citations(bibcode:"X") AND {ASTRONOMY}'),
        (Document.raw("star"), f"(star) AND {ASTRONOMY}"),
    ],
)
def test_scope_is_a_final_top_level_condition(astronomy: None, expr: Expr | None, expected: str) -> None:
    search = plan_of(expr)
    assert search.q == expected
    assert collection_notes(search) == [
        f"limited to collection 'astronomy' (ads.config.collection): {ASTRONOMY} is added"
    ]


def test_several_collections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "collection", ["astronomy", " physics ", "", "astronomy"])
    search = plan_of(Document.year == 2020)
    assert search.q == f"year:2020 AND {BOTH}"
    assert collection_notes(search) == [
        f"limited to collection ['astronomy', 'physics'] (ads.config.collection): {BOTH} is added"
    ]
    assert to_solr(Document.journal == "ApJ") == f'bibstem:"ApJ" AND {BOTH}'


def test_unknown_collection_names_are_sent_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "collection", ("no such collection",))
    assert plan_of().q == 'database:"no such collection"'


def test_to_solr_reflects_the_scope(astronomy: None) -> None:
    assert to_solr(Document.year == 2020) == f"year:2020 AND {ASTRONOMY}"
    assert to_solr(Document.select().where(Document.year == 2020)) == f"year:2020 AND {ASTRONOMY}"


@pytest.mark.parametrize("setting", [None, "", "  ", [], ["", " "]])
def test_no_collection_is_todays_behaviour(monkeypatch: pytest.MonkeyPatch, setting: Any) -> None:
    monkeypatch.setattr(_config.config, "collection", setting)
    search = plan_of(Document.year == 2020)
    assert search.q == "year:2020"
    assert collection_notes(search) == []
    assert plan_of().q == "*:*"
    assert plan_of(Document.year != 2020).q == "*:* AND -year:2020"


def test_setting_is_read_at_plan_time(monkeypatch: pytest.MonkeyPatch) -> None:
    query = Document.select().where(Document.year == 2020)
    assert plan(query).q == "year:2020"
    monkeypatch.setattr(_config.config, "collection", "physics")
    assert plan(query).q == 'year:2020 AND database:"physics"'


def test_invalid_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "collection", ["astronomy", 3])
    with pytest.raises(TypeError, match="collection 2 must be a str, not int"):
        plan_of(Document.year == 2020)


# Exempt: the filter names the collection.


@pytest.mark.parametrize(
    "expr",
    [
        Document.database == "physics",
        Document.database.in_(["physics", "general"]),
        Document.collection == "earthscience",
        (Document.year == 2020) & ~(Document.database == "physics"),
        (Document.year == 2020) | (Document.collection == "physics"),
        Document.citations(Document.database == "physics"),
        Document.database.like("phys*"),
    ],
)
def test_filter_on_the_collection_is_not_scoped(astronomy: None, expr: Expr) -> None:
    search = plan_of(expr)
    assert "astronomy" not in search.q
    (note,) = collection_notes(search)
    assert note.startswith("collection 'astronomy' (ads.config.collection) not applied: the filter names a collection")


@pytest.mark.parametrize("text", ["database:physics", "year:2020 AND COLLECTION:physics", "Database:(a OR b)"])
def test_raw_text_naming_the_collection_is_not_scoped(astronomy: None, text: str) -> None:
    search = plan_of(Document.raw(text) & (Document.year == 2020))
    assert search.q == f"({text}) AND year:2020"
    (note,) = collection_notes(search)
    assert note.endswith("not applied: the raw query names a collection itself")


# Exempt: the filter names specific documents.


@pytest.mark.parametrize(
    ("expr", "expected", "field"),
    [
        (Document.bibcode == "X", 'bibcode:"X"', "bibcode"),
        (Document.bibcode.in_(["X", "Y"]), 'bibcode:("X" OR "Y")', "bibcode"),
        (Document.in_(["X", "Y"]) & (Document.year == 2020), 'bibcode:("X" OR "Y") AND year:2020', "bibcode"),
        ((Document.year == 2020) & (Document.id == "12312494"), 'year:2020 AND id:"12312494"', "id"),
        (Document.id == 12312494, 'id:"12312494"', "id"),
        (Document.scix_id == "S", 'scix_id:"S"', "scix_id"),
        (Document.identifier == "arXiv:1234.5678", 'identifier:"arXiv:1234.5678"', "identifier"),
        (Document.doi.in_(["10.1/a", "10.1/b"]), 'doi:("10.1/a" OR "10.1/b")', "doi"),
        (Document.alternate_bibcode == "X", 'alternate_bibcode:"X"', "alternate_bibcode"),
    ],
)
def test_top_level_identifier_condition_is_not_scoped(astronomy: None, expr: Expr, expected: str, field: str) -> None:
    search = plan_of(expr)
    assert search.q == expected
    assert collection_notes(search) == [
        f"collection 'astronomy' (ads.config.collection) not applied: the filter names specific documents by {field}"
    ]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ((Document.bibcode == "X") | (Document.year == 2020), f'(bibcode:"X" OR year:2020) AND {ASTRONOMY}'),
        (Document.bibcode != "X", f'-bibcode:"X" AND {ASTRONOMY}'),
        (Document.not_in(["X", "Y"]) & (Document.year == 2020), f'-bibcode:("X" OR "Y") AND year:2020 AND {ASTRONOMY}'),
        (Document.citations(Document.bibcode == "X"), f'citations(bibcode:"X") AND {ASTRONOMY}'),
        (Document.bibcode.like("2020ApJ*"), f"bibcode:2020ApJ* AND {ASTRONOMY}"),
        (Document.references(Document.in_(Library(id="abc"))), f"references(docs(library/abc)) AND {ASTRONOMY}"),
        ((Document.in_(Library(id="abc"))) | (Document.year == 2020), f"(docs(library/abc) OR year:2020) AND {ASTRONOMY}"),
    ],
)
def test_identifier_condition_that_is_not_top_level_and_positive_is_scoped(
    astronomy: None, expr: Expr, expected: str
) -> None:
    assert plan_of(expr).q == expected


def test_large_bibcode_set_goes_to_bigquery_unscoped(astronomy: None) -> None:
    codes = bibcodes(60)
    search = plan_of(Document.bibcode.in_(codes) & (Document.year == 2020))
    assert search.path == "/search/bigquery" and search.bigquery_bibcodes == tuple(codes)
    assert search.q == "year:2020"
    assert collection_notes(search)[0].endswith("names specific documents by bibcode")


def test_large_negated_set_is_excluded_and_scoped(astronomy: None) -> None:
    codes = bibcodes(60)
    search = plan_of(Document.not_in(codes) & (Document.year == 2020))
    assert search.exclude_bibcodes == frozenset(codes)
    assert search.q == f"year:2020 AND {ASTRONOMY}"


def test_library_docs_strategy_is_not_scoped(astronomy: None) -> None:
    search = plan_of(Document.in_(Library(id="abc")) & (Document.year == 2020))
    assert search.q == "docs(library/abc) AND year:2020"
    assert collection_notes(search) == [
        "collection 'astronomy' (ads.config.collection) not applied: the filter asks for the documents of library abc"
    ]
    assert plan_of(Document.not_in(Library(id="abc"))).q == f"-docs(library/abc) AND {ASTRONOMY}"


@pytest.mark.parametrize("size", [1, 3, 80])
def test_library_bibcodes_strategy_is_not_scoped(monkeypatch: pytest.MonkeyPatch, astronomy: None, size: int) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    members = bibcodes(size)
    search = plan_of(Document.in_(Library(id="abc")) & (Document.year == 2020), library_bibcodes={"abc": members})
    assert "database" not in search.q
    assert collection_notes(search)[0].endswith("names specific documents by bibcode")
    assert (search.bigquery_bibcodes is not None) == (size == 80)


def test_empty_plans_stay_empty(monkeypatch: pytest.MonkeyPatch, astronomy: None) -> None:
    for expr in [
        Document.journal.title == "No Such Journal",
        Document.in_([]),
        Document.in_(bibcodes(60)) & Document.in_(bibcodes(60, prefix="2021MNRAS")),
    ]:
        search = plan_of(expr)
        assert search.empty, expr
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    assert plan_of(Document.in_(Library(id="abc")), library_bibcodes={"abc": []}).empty


# Execution.


def test_search_count_and_explain_send_the_scope(page_size: None, astronomy: None) -> None:
    fake = FakeSearch(make_docs(3))
    query = Document.select(Document.title).where(Document.year == 2020).using(fake.client())
    assert len(query.all()) == 3
    assert fake.params()["q"] == f"year:2020 AND {ASTRONOMY}"
    assert Document.select().where(Document.year == 2020).using(fake.client()).count() == 3
    assert fake.params()["q"] == f"year:2020 AND {ASTRONOMY}" and fake.params()["rows"] == "0"
    explanation = query.explain()
    request = explanation.requests[0]
    assert request.params is not None and request.params["q"] == f"year:2020 AND {ASTRONOMY}"
    assert "limited to collection 'astronomy'" in str(explanation)


def test_lazy_loading_is_never_scoped(astronomy: None) -> None:
    fake = FakeSearch(make_docs(3))
    documents = Document.select(Document.year).where(Document.year == 2020).using(fake.client()).all()
    assert fake.params()["q"] == f"year:2020 AND {ASTRONOMY}"
    with pytest.warns(LazyLoadWarning):
        assert documents[0].title == ["Paper 0"]
    first, second, third = bibcodes(3)
    assert fake.params()["q"] == f'bibcode:("{first}" OR "{second}" OR "{third}")'


def test_lazy_loading_by_another_identifier_is_never_scoped(monkeypatch: pytest.MonkeyPatch, astronomy: None) -> None:
    fake = FakeSearch([{"id": "7", "title": ["No bibcode"]}])
    monkeypatch.setattr("ads.client._default_client", fake.client())
    with pytest.warns(LazyLoadWarning):
        assert Document(id="7").title == ["No bibcode"]
    assert fake.params()["q"] == 'id:"7"'


def test_get_by_bibcode_is_never_scoped(monkeypatch: pytest.MonkeyPatch, astronomy: None) -> None:
    fake = FakeSearch(make_docs(2))
    monkeypatch.setattr("ads.client._default_client", fake.client())
    assert Document.get(bibcode=bibcodes(2)[1]).bibcode == bibcodes(2)[1]
    assert fake.params()["q"] == f'bibcode:"{bibcodes(2)[1]}"'
