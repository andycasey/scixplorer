"""Tests for the Document model: fields, relations, operators and lazy loading."""

from __future__ import annotations

import gc
import pickle
import warnings
from typing import Any

import httpx
import pytest
from search_fakes import FakeSearch, bibcodes, make_docs

from scixplorer import _config
from scixplorer.exceptions import APIError, LazyLoadWarning, QueryError
from scixplorer.expr import Func, In, InLibrary, Order, Raw
from scixplorer.fields import (
    DateField,
    DateTimeField,
    FloatField,
    IntegerField,
    NameField,
    NumericStringField,
    StringField,
    TextField,
    VirtualField,
)
from scixplorer.models.affiliation import Affiliation
from scixplorer.models.document import Document
from scixplorer.models.journal import Journal
from scixplorer.models.library import Library
from scixplorer.evaluate import matches
from scixplorer.search import to_solr
from scixplorer.utils import flatten

# Fields.


@pytest.mark.parametrize(
    ("name", "kind", "multivalued"),
    [
        ("author", NameField, True),
        ("first_author", NameField, False),
        ("editor", NameField, True),
        ("title", TextField, True),
        ("abstract", TextField, False),
        ("keyword", TextField, True),
        ("bibcode", StringField, False),
        ("id", NumericStringField, False),
        ("volume", StringField, False),
        ("bibstem", StringField, True),
        ("aff_id", StringField, True),
        ("doi", StringField, True),
        ("citation", StringField, True),
        ("reference", StringField, True),
        ("year", IntegerField, False),
        ("simbid", IntegerField, True),
        ("classic_factor", IntegerField, False),
        ("cite_read_boost", FloatField, False),
        ("date", DateTimeField, False),
        ("pubdate", DateField, False),
        ("full", VirtualField, False),
        ("uat_name", StringField, True),
        ("uat_id", IntegerField, True),
        ("planetary_feature_name", TextField, True),
        ("planetary_feature_id", IntegerField, True),
        ("gpn", TextField, True),
    ],
)
def test_field_classification(name: str, kind: type, multivalued: bool) -> None:
    field = Document._fields[name]
    assert type(field) is kind
    assert field.multivalued is multivalued
    assert field.__doc__


def test_describe_lists_every_field_with_help() -> None:
    info = {i.name: i for i in Document.describe()}
    assert list(info) == list(Document._fields)
    assert all(i.doc for i in info.values())
    assert info["author"].match_kind == "name" and info["author"].type == "list[str]"
    assert info["lang"].stored is False and info["full"].stored is False


def test_deferred_fields_are_stored() -> None:
    assert Document._deferred == {"ack", "body", "caption", "citation", "credit", "mention", "reference"}
    assert all(Document._fields[name].stored for name in Document._deferred)


def test_search_only_fields_raise_on_access() -> None:
    document = Document(bibcode="2015ApJ...808...16N")
    with pytest.raises(AttributeError, match="search-only"):
        document.full
    with pytest.raises(AttributeError, match="search-only"):
        document.lang
    with pytest.raises(TypeError, match="search-only"):
        Document(lang="en")


def test_repr_and_str() -> None:
    document = Document(bibcode="2015ApJ...808...16N")
    assert repr(document) == "<Document: bibcode='2015ApJ...808...16N'>"
    assert str(document) == "2015ApJ...808...16N"
    assert repr(Document()) == str(Document()) == "<Document>"


def test_equality_by_bibcode() -> None:
    assert Document(bibcode="X", title=["a"]) == Document(bibcode="X")
    assert len({Document(bibcode="X"), Document(bibcode="X"), Document(bibcode="Y")}) == 2


def test_identifiers_are_declared_once() -> None:
    assert Document._identifiers == ("bibcode", "scix_id", "id")
    assert set(Document._identifiers) <= Document._not_lazy
    assert not hasattr(Document, "_key")


def test_identity_is_the_first_identifier_with_a_value() -> None:
    assert Document(bibcode="X", scix_id="S1", id="1") == Document(bibcode="X", scix_id="S2", id="2")
    assert Document(scix_id="S", id="1") == Document(scix_id="S", id="2")
    assert Document(id="1") == Document(id="1")
    assert Document(bibcode="X", id="1") != Document(bibcode="Y", id="1")
    assert Document(scix_id="S", id="1") != Document(scix_id="T", id="1")
    # The same value in a different identifier field is a different identity.
    assert Document(bibcode="X", id="1") != Document(id="1")
    assert Document(scix_id="X") != Document(bibcode="X")
    assert Document(id="1", bibcode=None) == Document(id="1")


def test_hash_is_consistent_with_equality() -> None:
    same = [Document(bibcode="X", id="1"), Document(bibcode="X", id="2")]
    assert hash(same[0]) == hash(same[1])
    assert hash(Document(scix_id="S")) == hash(Document(scix_id="S", id="9"))
    documents = {
        Document(bibcode="X"),
        Document(bibcode="X", scix_id="S"),
        Document(scix_id="S"),
        Document(scix_id="S", id="1"),
        Document(id="1"),
        Document(id="1"),
        Document(scix_id="1"),
    }
    assert len(documents) == 4


def test_documents_without_an_identifier_are_equal_only_to_themselves() -> None:
    first, second = Document(title=["a"]), Document(title=["a"])
    assert first == first and first != second
    assert len({first, second}) == 2


def test_repr_and_str_use_the_first_identifier() -> None:
    assert repr(Document(bibcode="X", scix_id="S", id="1")) == "<Document: bibcode='X'>"
    assert repr(Document(scix_id="S", id="1")) == "<Document: scix_id='S'>"
    assert repr(Document(id="1")) == "<Document: id='1'>"
    assert str(Document(bibcode="X", scix_id="S")) == "X"
    assert str(Document(scix_id="S", id="1")) == "S"
    assert str(Document(id="1")) == "1"


def test_fields_returned_but_not_indexed() -> None:
    assert {name for name, field in Document._fields.items() if not field.indexed} == {"links_data", "page_range"}
    info = {i.name: i for i in Document.describe()}
    assert info["page_range"].indexed is False and info["page_range"].stored is True
    assert info["title"].indexed is True and info["full"].indexed is True


@pytest.mark.parametrize(
    "build",
    [
        lambda: Document.page_range == "16-20",
        lambda: Document.page_range.like("16*"),
        lambda: Document.links_data.in_(["x"]),
        lambda: Document.links_data[0] == "x",
        lambda: Document.page_range.desc(),
        lambda: Document.top_n(5, "star", Document.page_range),
    ],
)
def test_fields_not_indexed_cannot_be_searched(build: Any) -> None:
    with pytest.raises(QueryError, match=r"Document\.(page_range|links_data) can be returned but not searched"):
        build()


@pytest.mark.parametrize("key", [Document.page_range, "page_range"])
def test_sorting_by_a_field_that_is_not_indexed_is_refused_when_planned(key: Any) -> None:
    query = Document.select().order_by(key)
    with pytest.raises(QueryError, match="Cannot sort by Document.page_range: it is not indexed"):
        query.explain()


def test_fields_not_indexed_can_be_selected_and_read() -> None:
    fake = FakeSearch(make_docs(2, page_range="16-20", links_data=['{"url": "x"}']))
    query = Document.select(Document.page_range, Document.links_data).using(fake.client())
    documents = query.all()
    assert fake.params()["fl"] == "bibcode,id,links_data,page_range,scix_id"
    assert [d.page_range for d in documents] == ["16-20", "16-20"]
    assert documents[0].links_data == ['{"url": "x"}']


def test_tab_completion_lists_operators() -> None:
    names = dir(Document)
    for name in ("citations", "join_citations", "top_n", "pos", "in_", "raw", "journal", "affiliation", "title"):
        assert name in names


# Operators.


@pytest.mark.parametrize(
    ("method", "solr_name"),
    [
        ("citations", "citations"),
        ("references", "references"),
        ("trending", "trending"),
        ("useful", "useful"),
        ("reviews", "reviews"),
        ("similar", "similar"),
        ("citis", "citis"),
        ("classic_relevance", "classic_relevance"),
        ("instructive", "instructive"),
        ("join_citations", "joincitations"),
        ("join_references", "joinreferences"),
        ("reviews2", "reviews2"),
        ("useful2", "useful2"),
    ],
)
def test_operators(method: str, solr_name: str) -> None:
    build = getattr(Document, method)
    assert build.__doc__ and build.__name__ == method
    assert build(Document.bibcode == "X") == Func(solr_name, (Document.bibcode == "X",))
    assert to_solr(build(Document.bibcode == "X")) == f'{solr_name}(bibcode:"X")'
    assert build("abs:exoplanets") == Func(solr_name, (Raw("abs:exoplanets"),))
    with pytest.raises(QueryError):
        build("  ")
    with pytest.raises(TypeError):
        build(42)


def test_top_n() -> None:
    expr = Document.year == 2020
    assert to_solr(Document.top_n(10, expr)) == "topn(10, year:2020)"
    assert to_solr(Document.top_n(10, expr, Document.citation_count.desc())) == "topn(10, year:2020, citation_count desc)"
    assert Document.top_n(5, "star", Document.year) == Func("topn", (5, Raw("star"), Order(Document._fields["year"])))
    with pytest.raises(QueryError):
        Document.top_n(0, expr)
    with pytest.raises(TypeError):
        Document.top_n(10, expr, "citation_count desc")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Document.top_n(10, expr, Journal.title.asc())


def test_pos() -> None:
    expr = Document.author == "Casey, A"
    assert to_solr(Document.pos(expr, 1)) == 'pos(author:"Casey, A", 1)'
    assert to_solr(Document.pos(expr, 2, 3)) == 'pos(author:"Casey, A", 2, 3)'
    assert to_solr(Document.pos(expr, 1)) == to_solr(Document.author[0] == "Casey, A")
    for bad in [(0,), (2, 1), (True,)]:
        with pytest.raises((QueryError, TypeError)):
            Document.pos(expr, *bad)


def test_raw() -> None:
    assert Document.raw("year:2020") == Raw("year:2020")
    assert to_solr(Document.raw("a OR b") & (Document.year == 2020)) == "(a OR b) AND year:2020"
    with pytest.raises(QueryError):
        Document.raw("")
    with pytest.raises(TypeError):
        Document.raw(2020)  # type: ignore[arg-type]


def test_in_with_bibcodes_and_documents() -> None:
    field = Document._fields["bibcode"]
    expected = In(field, ("A", "B", "C"))
    assert Document.in_(["A", Document(bibcode="B"), ("C", "A")]) == expected
    assert Document.in_("A") == In(field, ("A",))
    assert to_solr(Document.not_in(["A", "B"])) == '*:* AND -bibcode:("A" OR "B")'
    assert to_solr(Document.in_([])) == "*:* AND -*:*"


def test_in_with_a_library() -> None:
    assert Document.in_(Library(id="abc")) == InLibrary("abc")
    assert Document.not_in(Library(id="abc")) == ~InLibrary("abc")


# Relations on instances.


def test_journal_from_bibcode() -> None:
    assert Document(bibcode="2015ApJ...808...16N").journal is Journal.lookup("ApJ")
    assert Document(bibcode="2019A&A...623A..72S").journal is Journal.lookup("A&A")
    assert Document(bibcode="2015ZZZZZ.808...16N").journal is None
    assert Document().journal is None


def test_journal_falls_back_to_bibstem() -> None:
    assert Document(bibcode="not-a-bibcode", bibstem=["MNRAS", "ApJ"]).journal is Journal.lookup("MNRAS")
    assert Document(bibcode="not-a-bibcode", bibstem=[]).journal is None


def test_affiliation_per_author() -> None:
    ids = [a.id for a in Affiliation.select().limit(2)]
    assert Affiliation.lookup("Z0000") is None
    document = Document(bibcode="X", aff_id=[ids[0], "-", f"{ids[0]}; {ids[1]}", "Z0000", f"Z0000; {ids[1]}"])
    first, second = Affiliation.lookup(ids[0]), Affiliation.lookup(ids[1])
    assert document.affiliation == [first, None, (first, second), None, second]
    assert flatten(document.affiliation) == [first, first, second, second]
    assert Document(bibcode="X", aff_id=None).affiliation is None


def test_relation_cache_is_cleared_when_the_key_changes() -> None:
    document = Document(bibcode="2015ApJ...808...16N")
    assert document.journal is Journal.lookup("ApJ")
    document.bibcode = "2015MNRAS.808...16N"
    assert document.journal is Journal.lookup("MNRAS")


def test_documents_pickle_without_their_page() -> None:
    fake = FakeSearch(make_docs(2))
    document = Document.select().using(fake.client()).all()[0]
    copy = pickle.loads(pickle.dumps(document))
    assert copy == document and copy._page is None and copy.title == document.title


# Lazy loading.


def three(fake: FakeSearch) -> list[Document]:
    return Document.select(Document.citation_count).where(Document.year == 2020).using(fake.client()).all()


def test_lazy_loading_loads_the_whole_page_once() -> None:
    fake = FakeSearch(make_docs(3, abstract="Stars."))
    documents = three(fake)
    assert len(fake.searches) == 1
    with pytest.warns(LazyLoadWarning, match=r"Document\.title .* 3 document") as caught:
        assert documents[1].title == ["Paper 1"]
    assert len(caught) == 1
    assert caught[0].filename == __file__
    assert len(fake.searches) == 2
    assert fake.params()["fl"] == "bibcode,id,scix_id,title"
    first, second, third = bibcodes(3)
    assert fake.params()["q"] == f'bibcode:("{second}" OR "{first}" OR "{third}")'
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert [d.title for d in documents] == [["Paper 0"], ["Paper 1"], ["Paper 2"]]
    assert len(fake.searches) == 2


def test_lazy_loading_a_deferred_field() -> None:
    fake = FakeSearch(make_docs(3, citation=["2000ApJ...500....1A"]))
    documents = Document.select().using(fake.client()).all()
    assert "citation" not in fake.params()["fl"].split(",")
    with pytest.warns(LazyLoadWarning, match="Document.citation"):
        assert documents[0].citation == ["2000ApJ...500....1A"]
    assert [d.citation for d in documents[1:]] == [["2000ApJ...500....1A"]] * 2
    assert len(fake.searches) == 2


def test_selected_deferred_field_is_fetched() -> None:
    fake = FakeSearch(make_docs(1, reference=["X"]))
    (document,) = Document.select(Document.reference).using(fake.client()).all()
    assert document.reference == ["X"]
    assert len(fake.searches) == 1


def test_field_ads_does_not_return_is_none_and_not_requested_again() -> None:
    fake = FakeSearch(make_docs(3))
    documents = three(fake)
    with pytest.warns(LazyLoadWarning):
        assert documents[0].abstract is None
    assert [d.abstract for d in documents] == [None, None, None]
    assert len(fake.searches) == 2


def test_document_missing_from_the_reload_gets_none() -> None:
    fake = FakeSearch(make_docs(3))
    documents = three(fake)
    del fake.docs[1]
    with pytest.warns(LazyLoadWarning):
        assert documents[0].title == ["Paper 0"]
    assert documents[1].title is None
    assert len(fake.searches) == 2


def test_lazy_loading_skips_documents_that_already_have_the_field() -> None:
    fake = FakeSearch(make_docs(3))
    documents = three(fake)
    documents[2].title = ["Mine"]
    with pytest.warns(LazyLoadWarning, match="2 document"):
        documents[0].title
    assert documents[2].title == ["Mine"]
    assert bibcodes(3)[2] not in fake.params()["q"]


def test_lazy_loading_uses_the_pages_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch(make_docs(2))
    other = FakeSearch(make_docs(2))
    monkeypatch.setattr("scixplorer.client._default_client", other.client())
    documents = three(fake)
    with pytest.warns(LazyLoadWarning):
        documents[0].title
    assert len(fake.searches) == 2 and other.requests == []


def test_lazy_loading_is_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "page_size", 2)
    fake = FakeSearch(make_docs(5))
    documents = three(fake)
    assert len(fake.searches) == 3
    with pytest.warns(LazyLoadWarning, match="2 document"):
        documents[2].title
    assert documents[3]._data.get("title") == ["Paper 3"] and "title" not in documents[0]._data


def test_lazy_loading_a_large_page_uses_bigquery(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch(make_docs(80))
    documents = Document.select(Document.year).using(fake.client()).all()
    with pytest.warns(LazyLoadWarning, match="80 document"):
        documents[0].title
    request = fake.searches[-1]
    assert request.method == "POST" and request.url.params["q"] == "*:*"
    assert all(d.title is not None for d in documents)


def test_hand_built_document_loads_only_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch(make_docs(3))
    monkeypatch.setattr("scixplorer.client._default_client", fake.client())
    document = Document(bibcode=bibcodes(3)[1])
    with pytest.warns(LazyLoadWarning, match="1 document"):
        assert document.title == ["Paper 1"]
    assert fake.params()["q"] == f'bibcode:"{bibcodes(3)[1]}"'


def test_document_without_bibcode_loads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch(make_docs(1))
    monkeypatch.setattr("scixplorer.client._default_client", fake.client())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert Document().title is None
        assert Document(title=["x"]).abstract is None
    assert fake.requests == []


@pytest.mark.parametrize("name", ["bibcode", "scix_id", "id", "score"])
def test_identifiers_and_score_are_never_lazy_loaded(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    fake = FakeSearch()
    fake.reply = lambda request: httpx.Response(
        200, json={"response": {"numFound": 1, "docs": [{"bibcode": "2015ApJ...808...16N"}]}}
    )
    monkeypatch.setattr("scixplorer.client._default_client", fake.client())
    (document,) = Document.select(Document.year).using(fake.client()).all()
    document._data.pop(name, None)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert getattr(document, name) is None
    assert len(fake.requests) == 1


def test_document_without_bibcode_loads_through_its_other_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch([{"scix_id": "S1", "id": "1", "title": ["By scix_id"]}, {"id": "2", "title": ["By id"]}])
    monkeypatch.setattr("scixplorer.client._default_client", fake.client())
    with pytest.warns(LazyLoadWarning, match="1 document"):
        assert Document(scix_id="S1", id="1").title == ["By scix_id"]
    assert fake.params()["q"] == 'scix_id:"S1"'
    with pytest.warns(LazyLoadWarning, match="1 document"):
        assert Document(id="2").title == ["By id"]
    assert fake.params()["q"] == 'id:"2"'


def test_lazy_loading_a_mixed_page_groups_documents_by_identifier() -> None:
    rows = [
        {"bibcode": "2020ApJ...00000000A", "scix_id": "S0", "id": "10", "title": ["Paper 0"]},
        {"scix_id": "S1", "id": "11", "title": ["Paper 1"]},
        {"id": "12", "title": ["Paper 2"]},
        {"bibcode": "2020ApJ...00000003A", "id": "13", "title": ["Paper 3"]},
        {"scix_id": "S4", "id": "14", "title": ["Paper 4"]},
        {"title": ["Paper 5"]},
    ]
    fake = FakeSearch(rows)
    documents = Document.select(Document.year).using(fake.client()).all()
    assert [d._key_value() for d in documents] == [
        ("bibcode", "2020ApJ...00000000A"),
        ("scix_id", "S1"),
        ("id", "12"),
        ("bibcode", "2020ApJ...00000003A"),
        ("scix_id", "S4"),
        None,
    ]
    with pytest.warns(LazyLoadWarning, match=r"5 document\(s\) with 3 more search requests"):
        assert documents[1].title == ["Paper 1"]
    assert [fake.params(i)["q"] for i in (1, 2, 3)] == [
        'scix_id:("S1" OR "S4")',
        'bibcode:("2020ApJ...00000000A" OR "2020ApJ...00000003A")',
        'id:"12"',
    ]
    assert len(fake.searches) == 4
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert [d.title for d in documents[:5]] == [[f"Paper {i}"] for i in range(5)]
        assert documents[5].title is None
    assert "title" not in documents[5]._data
    assert len(fake.searches) == 4


def test_rows_without_scix_id_have_none_and_do_not_load_it() -> None:
    fake = FakeSearch(make_docs(2))
    documents = Document.select(Document.year).using(fake.client()).all()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert [d.scix_id for d in documents] == [None, None]
    assert len(fake.searches) == 1


def test_relation_access_lazy_loads_its_key_once() -> None:
    fake = FakeSearch(make_docs(2, aff_id=["-"]))
    documents = Document.select(Document.title).using(fake.client()).all()
    with pytest.warns(LazyLoadWarning, match="Document.aff_id") as caught:
        assert documents[0].affiliation == [None]
    assert caught[0].filename == __file__
    assert documents[1].affiliation == [None]
    assert len(fake.searches) == 2


def test_lazy_loading_errors_leave_the_field_unloaded() -> None:
    fake = FakeSearch(make_docs(2))
    documents = three(fake)
    fake.reply = lambda request: httpx.Response(500, json={"error": "down"})
    with pytest.warns(LazyLoadWarning), pytest.raises(APIError, match="down"):
        documents[0].title
    fake.reply = None
    with pytest.warns(LazyLoadWarning):
        assert documents[0].title == ["Paper 0"]


def test_page_does_not_keep_documents_alive() -> None:
    fake = FakeSearch(make_docs(3))
    documents = three(fake)
    page = documents[0]._page
    assert page is not None and len(page.members()) == 3
    del documents[1:]
    gc.collect()
    assert page.members() == [documents[0]]


def test_lazy_loading_inside_get(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch(make_docs(1))
    monkeypatch.setattr("scixplorer.client._default_client", fake.client())
    document: Any = Document.select(Document.year).where(Document.bibcode == bibcodes(1)[0]).get()
    with pytest.warns(LazyLoadWarning):
        assert document.citation_count == 0


# Integer ids.


@pytest.mark.parametrize(
    ("as_int", "as_str"),
    [
        (Document.id == 12312494, Document.id == "12312494"),
        (Document.id.exact(12312494), Document.id.exact("12312494")),  # type: ignore[arg-type]
        (Document.id.in_([12312494]), Document.id.in_(["12312494"])),
    ],
)
def test_int_id_is_its_decimal_string(as_int: Any, as_str: Any) -> None:
    assert as_int == as_str
    assert to_solr(as_int) == to_solr(as_str) == 'id:"12312494"'
    assert matches(as_int, Document(id="12312494")) and matches(as_str, Document(id="12312494"))
    assert not matches(as_int, Document(id="1231249"))


def test_int_ids_in_a_set() -> None:
    assert to_solr(Document.id.in_([1, "2"])) == 'id:("1" OR "2")'
    assert to_solr(Document.id.not_in([1, 2])) == '*:* AND -id:("1" OR "2")'
    assert matches(Document.id.in_([1, "2"]), Document(id="2"))


@pytest.mark.parametrize("value", [-1, True, False, 1.0, 12312494.0, None, [1], ""])
def test_id_rejects_other_values(value: Any) -> None:
    with pytest.raises(QueryError):
        Document.id == value  # noqa: B015


@pytest.mark.parametrize("build", [lambda: Document.title == 5, lambda: Document.bibcode == 5, lambda: Document.scix_id == 5])
def test_other_string_fields_still_reject_ints(build: Any) -> None:
    with pytest.raises(QueryError, match="needs a str"):
        build()


def test_int_id_on_an_instance_is_held_as_a_string() -> None:
    document = Document(id=12312494)  # type: ignore[arg-type]
    assert document.id == "12312494"
    assert document == Document(id="12312494") and hash(document) == hash(Document(id="12312494"))
    assert repr(document) == "<Document: id='12312494'>"


def test_get_by_int_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch(make_docs(3))
    monkeypatch.setattr("scixplorer.client._default_client", fake.client())
    document = Document.get(id=1001)
    assert document.bibcode == bibcodes(3)[1] and document.id == "1001"
    assert fake.params()["q"] == 'id:"1001"'


def test_lazy_loading_by_int_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch([{"id": "1001", "title": ["No bibcode"]}])
    monkeypatch.setattr("scixplorer.client._default_client", fake.client())
    document = Document(id=1001)  # type: ignore[arg-type]
    with pytest.warns(LazyLoadWarning, match="1 document"):
        assert document.title == ["No bibcode"]
    assert fake.params()["q"] == 'id:"1001"'
