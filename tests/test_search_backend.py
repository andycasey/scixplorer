"""Tests for SearchBackend: requests, paging, bigquery, exclusion, libraries, count, explain."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

import httpx
import pytest
from search_fakes import FakeSearch, bibcodes, make_docs

from scixplorer import _config
from scixplorer.exceptions import APIError, DataWarning, UnsupportedQueryError
from scixplorer.models.affiliation import Affiliation
from scixplorer.models.document import Document
from scixplorer.models.journal import Journal
from scixplorer.models.library import Library


@pytest.fixture
def page_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "page_size", 200)


def search_params(fake: FakeSearch) -> list[tuple[str, str]]:
    return [(r.url.params["start"], r.url.params["rows"]) for r in fake.searches]


# Requests and paging.


def test_query_request(page_size: None) -> None:
    fake = FakeSearch(make_docs(3))
    docs = Document.select(Document.title).where(Document.author == "Ness, M").using(fake.client()).all()
    assert [d.bibcode for d in docs] == bibcodes(3)
    request = fake.searches[0]
    assert request.method == "GET" and request.url.path == "/v1/search/query"
    assert dict(request.url.params) == {
        "q": 'author:"Ness, M"',
        "fl": "bibcode,id,scix_id,title",
        "sort": "score desc,id asc",
        "start": "0",
        "rows": "200",
    }


def test_three_pages(page_size: None) -> None:
    fake = FakeSearch(make_docs(450))
    docs = Document.select().using(fake.client()).all()
    assert len(docs) == 450 and len({d.bibcode for d in docs}) == 450
    assert search_params(fake) == [("0", "200"), ("200", "200"), ("400", "200")]


def test_limit_across_pages(page_size: None) -> None:
    fake = FakeSearch(make_docs(450))
    assert len(Document.select().limit(250).using(fake.client()).all()) == 250
    assert search_params(fake) == [("0", "200"), ("200", "50")]


def test_offset_and_limit(page_size: None) -> None:
    fake = FakeSearch(make_docs(450))
    docs = Document.select().offset(30).limit(10).using(fake.client()).all()
    assert [d.bibcode for d in docs] == bibcodes(40)[30:]
    assert search_params(fake) == [("30", "10")]


def test_offset_beyond_the_end(page_size: None) -> None:
    fake = FakeSearch(make_docs(5))
    assert Document.select().offset(10).using(fake.client()).all() == []
    assert len(fake.searches) == 1


def test_streaming_requests_a_page_only_when_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "page_size", 2)
    fake = FakeSearch(make_docs(5))
    rows = iter(Document.select().using(fake.client()))
    next(rows)
    assert len(fake.searches) == 1
    next(rows)
    assert len(fake.searches) == 1
    next(rows)
    assert len(fake.searches) == 2
    assert len(list(rows)) == 2
    assert search_params(fake) == [("0", "2"), ("2", "2"), ("4", "2")]


def test_page_size_is_capped_at_the_api_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "page_size", 5000)
    fake = FakeSearch(make_docs(1))
    Document.select().using(fake.client()).all()
    assert fake.params()["rows"] == "2000"


def test_invalid_page_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "page_size", 0)
    with pytest.raises(ValueError, match="page_size"):
        Document.select().using(FakeSearch().client()).all()


def test_short_page_from_server_advances_by_what_was_returned(page_size: None) -> None:
    fake = FakeSearch(make_docs(5))
    real = fake._search

    def capped(request: httpx.Request, path: str) -> httpx.Response:
        response = real(request, path)
        body = response.json()
        body["response"]["docs"] = body["response"]["docs"][:2]
        return httpx.Response(200, json=body)

    fake._search = capped  # type: ignore[method-assign]
    docs = Document.select().using(fake.client()).all()
    assert [d.bibcode for d in docs] == bibcodes(5)
    assert [s for s, _ in search_params(fake)] == ["0", "2", "4"]


def test_empty_page_stops(page_size: None) -> None:
    fake = FakeSearch()
    fake.reply = lambda request: httpx.Response(200, json={"response": {"numFound": 10, "docs": []}})
    assert Document.select().using(fake.client()).all() == []
    assert len(fake.searches) == 1


def test_count_and_first(page_size: None) -> None:
    fake = FakeSearch(make_docs(450))
    client = fake.client()
    assert Document.select().where(Document.year == 2020).using(client).count() == 450
    assert fake.params() == {"q": "year:2020", "fl": "bibcode", "sort": "score desc,id asc", "start": "0", "rows": "0"}
    assert Document.select().offset(400).limit(100).using(client).count() == 50
    first = Document.select().using(client).first()
    assert first is not None and first.bibcode == bibcodes(1)[0]
    assert fake.params()["rows"] == "1"
    assert Document.select().using(client).exists()
    assert len(fake.searches) == 4


def test_get(page_size: None) -> None:
    fake = FakeSearch(make_docs(2))
    client = fake.client()
    fake.reply = lambda request: httpx.Response(200, json={"response": {"numFound": 0, "docs": []}})
    with pytest.raises(Document.DoesNotExist):
        Document.select().where(Document.bibcode == "2015ApJ...808...16N").using(client).get()
    fake.reply = None
    document = Document.select().where(Document.affiliation.country == "Australia").using(client).get()
    assert document.bibcode == bibcodes(1)[0]
    assert fake.params()["q"].startswith("aff_id:(")


def test_model_get_uses_the_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSearch(make_docs(1))
    monkeypatch.setattr("scixplorer.client._default_client", fake.client())
    assert Document.get(Document.affiliation.country == "Australia").bibcode == bibcodes(1)[0]
    fake.reply = lambda request: httpx.Response(200, json={"response": {"numFound": 0, "docs": []}})
    with pytest.raises(Document.DoesNotExist):
        Document.get(bibcode="2015ApJ...808...16N")
    assert fake.params()["q"] == 'bibcode:"2015ApJ...808...16N"'


def test_empty_plan_sends_nothing() -> None:
    fake = FakeSearch(make_docs(3))
    query = Document.select().where(Document.journal.title == "No Such Journal").using(fake.client())
    assert query.all() == [] and query.count() == 0 and query.first() is None
    assert fake.requests == []


def test_limit_zero_sends_nothing() -> None:
    fake = FakeSearch(make_docs(3))
    assert Document.select().limit(0).using(fake.client()).all() == []
    assert fake.requests == []


@pytest.mark.parametrize(
    "body",
    [
        {"error": "boom"},
        {"response": {"docs": [], "numFound": "3"}},
        {"response": {"docs": ["x"], "numFound": 1}},
        {"response": {"numFound": 1}},
        "not json",
    ],
)
def test_unexpected_response_shape(body: Any) -> None:
    fake = FakeSearch()
    fake.reply = lambda request: (
        httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
    )
    with pytest.raises(APIError, match="unexpected response"):
        Document.select().using(fake.client()).all()
    with pytest.raises(APIError, match="unexpected response"):
        Document.select().using(fake.client()).count()


# Bigquery and exclusion.


def test_bigquery_request(page_size: None) -> None:
    codes = bibcodes(60)
    fake = FakeSearch(make_docs(80))
    docs = Document.select().where(Document.bibcode.in_(codes) & (Document.year == 2020)).using(fake.client()).all()
    assert [d.bibcode for d in docs] == codes
    request = fake.searches[0]
    assert request.method == "POST" and request.url.path == "/v1/search/bigquery"
    params = dict(request.url.params)
    assert params.pop("fl").startswith("abstract,")
    assert params == {"q": "year:2020", "sort": "score desc,id asc", "start": "0", "rows": "200", "fq": "{!bitset}"}
    assert request.headers["content-type"] == "big-query/csv"
    assert request.content.decode() == "bibcode\n" + "\n".join(codes)


def test_bigquery_is_retried_as_a_read(page_size: None) -> None:
    fake = FakeSearch(make_docs(60))
    real = fake._search
    attempts = []

    def flaky(request: httpx.Request, path: str) -> httpx.Response:
        attempts.append(path)
        return httpx.Response(503) if len(attempts) == 1 else real(request, path)

    fake._search = flaky  # type: ignore[method-assign]
    assert len(Document.select().where(Document.in_(bibcodes(60))).using(fake.client()).all()) == 60
    assert attempts == ["/search/bigquery", "/search/bigquery"]


def test_bigquery_count(page_size: None) -> None:
    fake = FakeSearch(make_docs(80))
    assert Document.select().where(Document.in_(bibcodes(60))).using(fake.client()).count() == 60
    assert fake.searches[0].method == "POST" and fake.params()["rows"] == "0"


@pytest.mark.parametrize(("offset", "limit"), [(0, None), (3, 5), (0, 4), (5, 100), (7, 1), (40, 3)])
def test_client_side_exclusion_across_pages(monkeypatch: pytest.MonkeyPatch, offset: int, limit: int | None) -> None:
    monkeypatch.setattr(_config.config, "page_size", 4)
    docs = make_docs(30)
    excluded = set(bibcodes(30)[1::2]) | set(bibcodes(60, prefix="2021MNRAS"))
    fake = FakeSearch(docs)
    query = (
        Document.select()
        .where(~Document.bibcode.in_(sorted(excluded)) & (Document.year == 2020))
        .offset(offset)
        .limit(limit)
        .using(fake.client())
    )
    survivors = [d["bibcode"] for d in docs if d["bibcode"] not in excluded]
    expected = survivors[offset:] if limit is None else survivors[offset : offset + limit]
    assert [d.bibcode for d in query] == expected
    assert all(r.method == "GET" and r.url.params["q"] == "year:2020" for r in fake.searches)
    assert all(r.url.params["rows"] == "4" for r in fake.searches)
    assert fake.searches[0].url.params["start"] == "0"
    # Paging stops at the page holding the last row needed, or at the end.
    pages = bibcodes(30).index(expected[-1]) // 4 + 1 if limit is not None and expected else 8
    assert len(fake.searches) == pages


def test_client_side_exclusion_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "page_size", 4)
    excluded = bibcodes(30)[:20] + bibcodes(40, prefix="2021MNRAS")
    fake = FakeSearch(make_docs(30))
    query = Document.select().where(Document.not_in(excluded) & (Document.year == 2020)).using(fake.client())
    assert query.count() == 10
    assert len(fake.searches) == 8
    assert all(r.url.params["fl"] == "bibcode" for r in fake.searches)
    assert query.limit(3).count() == 3


# Libraries.


def test_library_docs_strategy_is_one_search(page_size: None) -> None:
    lib_1, lib_2, lib_3 = (Library(id=i) for i in ("L1", "L2", "L3"))
    fake = FakeSearch(make_docs(2))
    expr = (
        (Document.in_(lib_1) & (Document.year == 2020))
        | ((Document.year == 2021) & Document.in_(lib_2) & Document.not_in(lib_3))
        | (Document.year == 2019)
    )
    Document.select().where(expr).using(fake.client()).all()
    assert fake.calls() == [("GET", "/search/query")]


@pytest.mark.parametrize(("size", "path"), [(3, "/search/query"), (80, "/search/bigquery")])
def test_library_bibcodes_strategy(monkeypatch: pytest.MonkeyPatch, size: int, path: str) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    members = bibcodes(size)
    fake = FakeSearch(make_docs(100), libraries={"abc": members})
    query = Document.select().where(Document.in_(Library(id="abc")) & (Document.year == 2020)).using(fake.client())
    assert [d.bibcode for d in query] == members
    assert fake.calls()[0] == ("GET", "/biblib/libraries/abc")
    assert dict(fake.requests[0].url.params) == {"start": "0", "rows": "2000"}
    assert fake.calls()[1][1] == path
    if size == 3:
        assert fake.params()["q"].startswith('bibcode:("') and fake.params()["q"].endswith(" AND year:2020")
    assert Document.select().where(Document.in_(Library(id="abc"))).using(fake.client()).count() == size


def test_library_bibcodes_strategy_fetches_each_library_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    fake = FakeSearch(make_docs(10), libraries={"a": bibcodes(3), "b": bibcodes(2)})
    lib_a, lib_b = Library(id="a"), Library(id="b")
    expr = (Document.in_(lib_a) & (Document.year == 2020)) | (Document.in_(lib_b) & Document.not_in(lib_a))
    Document.select().where(expr).using(fake.client()).all()
    assert [path for _, path in fake.calls()] == ["/biblib/libraries/a", "/biblib/libraries/b", "/search/query"]


def test_library_bibcodes_strategy_does_not_fetch_irrelevant_libraries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    fake = FakeSearch(make_docs(3), libraries={"abc": bibcodes(3)})
    expr = ((Document.journal.title == "No Such Journal") & Document.in_(Library(id="abc"))) | (Document.year == 2020)
    assert len(Document.select().where(expr).using(fake.client()).all()) == 3
    assert fake.calls() == [("GET", "/search/query")]
    assert fake.params()["q"] == "year:2020"


def test_library_bibcodes_strategy_unsupported_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    fake = FakeSearch(make_docs(10), libraries={"abc": bibcodes(80)})
    with pytest.raises(UnsupportedQueryError, match="library_membership"):
        Document.select().where(Document.in_(Library(id="abc")) | (Document.year == 2020)).using(fake.client()).all()
    assert fake.calls() == [("GET", "/biblib/libraries/abc")]


# Explain.


def test_explain_relation_query(page_size: None) -> None:
    query = Document.select(Document.title).where(Document.journal.title.contains("gravitation")).limit(5)
    explanation = query.explain()
    assert len(explanation.requests) == 1
    request = explanation.requests[0]
    assert request.method == "GET" and request.path == "/search/query"
    assert request.params is not None and request.params["rows"] == 5
    text = str(explanation)
    assert "Document.journal:" in text and "matched as bibstem in" in text
    assert "GET /search/query" in text and "q = bibstem:(" in text


def test_explain_bigquery_and_exclusion(page_size: None) -> None:
    explanation = Document.select().where(Document.in_(bibcodes(60))).explain()
    assert explanation.requests[0].method == "POST" and "Uploads 60 bibcodes" in explanation.summary
    assert "(body:" in str(explanation)
    explanation = Document.select().where(Document.not_in(bibcodes(60)) & (Document.year == 2020)).offset(5).limit(3).explain()
    request = explanation.requests[0]
    assert request.params is not None and (request.params["start"], request.params["rows"]) == (0, 200)
    assert any("excluding 60 bibcode(s)" in note for note in explanation.notes)


def test_explain_empty_plan() -> None:
    explanation = Document.select().where(Document.journal.title == "No Such Journal").explain()
    assert explanation.requests == ()
    assert "No request is made" in str(explanation)
    assert "No request is made" in Document.select().limit(0).explain().summary


def test_explain_library_bibcodes_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    explanation = Document.select().where(Document.in_(Library(id="abc"))).explain()
    assert [(r.method, r.path) for r in explanation.requests] == [("GET", "/biblib/libraries/abc")]
    assert "search request follows" in explanation.summary


def test_explain_makes_no_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scixplorer.client._default_client", FakeSearch().client())
    Document.select().where(Affiliation.country == "Australia").explain()
    Document.select().where(Journal.abbreviation == "ApJ").explain()


# Async.


def test_async_sends_the_same_requests(page_size: None) -> None:
    sync_fake, async_fake = FakeSearch(make_docs(450)), FakeSearch(make_docs(450))
    query = Document.select(Document.title).where(Document.year == 2020).limit(300)
    sync_rows = query.using(sync_fake.client()).all()
    sync_count = query.using(sync_fake.client()).count()

    async def main() -> tuple[list[Document], int]:
        return await query.using(async_fake.client()).aall(), await query.using(async_fake.client()).acount()

    async_rows, async_count = asyncio.run(main())
    assert [d.bibcode for d in async_rows] == [d.bibcode for d in sync_rows]
    assert async_count == sync_count == 300
    assert [(r.method, str(r.url)) for r in async_fake.requests] == [(r.method, str(r.url)) for r in sync_fake.requests]
    assert len(sync_fake.requests) == 3


# Values from ADS.


def test_values_are_converted_to_declared_types() -> None:
    fake = FakeSearch()
    doc = {
        "bibcode": "2015ApJ...808...16N",
        "id": 12312494,
        "year": "2015",
        "volume": 808,
        "title": "A single title",
        "author": ["Ness, M.", "Hogg, D. W."],
        "citation_count": "120",
        "citation_count_norm": "1.5",
        "date": "2015-07-01T00:00:00Z",
        "pubdate": "2015-07-00",
        "entry_date": "not a date",
        "simbid": [1, "2", "x"],
        "page": 16,
        "property": "REFEREED",
    }
    fake.reply = lambda request: httpx.Response(200, json={"response": {"numFound": 1, "docs": [doc]}})
    with pytest.warns(DataWarning) as caught:
        (document,) = Document.select().using(fake.client()).all()
    assert {str(w.message).split(":")[0] for w in caught} == {"Document.entry_date", "Document.simbid"}
    assert document.id == "12312494"
    assert document.year == 2015
    assert document.volume == "808"
    assert document.title == ["A single title"]
    assert document.citation_count == 120
    assert document.citation_count_norm == 1.5
    assert document.date == datetime(2015, 7, 1, tzinfo=timezone.utc)
    assert document.pubdate == date(2015, 7, 1)
    assert document.entry_date is None and document.simbid is None
    assert document.page == ["16"]
    assert document.property == ["REFEREED"]


def test_requested_fields_missing_from_the_response_are_none(recwarn: pytest.WarningsRecorder) -> None:
    fake = FakeSearch([{"bibcode": "2015ApJ...808...16N", "id": "1"}])
    (document,) = Document.select().using(fake.client()).all()
    assert document.abstract is None and document.title is None and document.journal is not None
    assert len(fake.requests) == 1
    assert not recwarn.list


def test_raw_query_request(page_size: None) -> None:
    fake = FakeSearch(make_docs(10))
    query = (
        Document.select(Document.bibcode, Document.title)
        .where(Document.raw("author:'Ness, M' AND year:2018"))
        .order_by(Document.citation_count.desc())
        .limit(5)
    )
    assert len(query.using(fake.client()).all()) == 5
    assert fake.params() == {
        "q": "author:'Ness, M' AND year:2018",
        "fl": "bibcode,id,scix_id,title",
        "sort": "citation_count desc,id asc",
        "start": "0",
        "rows": "5",
    }
