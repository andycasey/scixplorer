"""Sorting and paging on /search/query, and searches of an uploaded bibcode set on /search/bigquery (items 11 and 12)."""

from __future__ import annotations

from collections import Counter

import pytest

from ads.models.document import Document
from ads.search import BIGQUERY_PATH, MAX_PAGE_SIZE, plan

from .harness import Live, Outcome, closed_set
from .support import Findings, LiveTransport

pytestmark = pytest.mark.live

#: A start offset far into the index (which holds well over a million records).
DEEP_START = 1_000_000


@pytest.fixture(scope="module")
def first_page(live: Live) -> Outcome:
    """The five most cited records of the closed set (sorted citation_count desc, id asc)."""
    return live.search(closed_set(), fields=("citation_count",), order=(Document.citation_count.desc(),), rows=5)


def everything(live: Live, start: int, rows: int) -> Outcome:
    """The request the client sends for Document.select(Document.bibcode) (q=*:*) at ``start``, ``rows``."""
    search = plan(Document.select(Document.bibcode))
    return live.send(search.request(start, rows), search.q)


def test_default_sort_is_accepted(pinned_outcome: Outcome) -> None:
    """sort=score desc,id asc (the client's default: relevance, then id so that pages are stable) is accepted.

    Depends on it: search._sort, which adds id asc to every sort.
    """
    pinned_outcome.require("a search with the default sort was rejected")
    assert pinned_outcome.request is not None and pinned_outcome.request.params is not None
    assert pinned_outcome.request.params["sort"] == "score desc,id asc"


def test_sort_by_citation_count(first_page: Outcome) -> None:
    """sort=citation_count desc,id asc is accepted and returns non-increasing citation counts.

    Depends on it: solr.compile_order and search._sort.
    """
    first_page.require("rejected")
    assert first_page.request is not None and first_page.request.params is not None
    assert first_page.request.params["sort"] == "citation_count desc,id asc"
    counts = [doc.get("citation_count") for doc in first_page.docs]
    assert len(counts) == 5 and counts == sorted(counts, reverse=True), first_page.explain(f"counts {counts}")


def test_consecutive_pages_do_not_overlap(live: Live, first_page: Outcome) -> None:
    """With start/rows paging and the id tiebreaker, page 2 continues page 1 without overlap.

    Depends on it: SearchBackend.run advancing start by the rows received.
    """
    second = live.search(
        closed_set(), fields=("citation_count",), order=(Document.citation_count.desc(),), start=5, rows=5
    ).require("rejected")
    first_page.require("rejected")
    overlap = set(first_page.bibcodes) & set(second.bibcodes)
    lowest = min(doc["citation_count"] for doc in first_page.docs)
    highest = max(doc["citation_count"] for doc in second.docs)
    assert len(second.docs) == 5 and not overlap and highest <= lowest, (
        f"pages overlap ({sorted(overlap)}) or are out of order ({lowest} then {highest})\n"
        f"{first_page.describe()}\n{second.describe()}"
    )


def test_rows_zero_returns_the_count(live: Live) -> None:
    """rows=0 returns numFound and no documents.

    Depends on it: SearchBackend.count and Query.count() (one request, no rows).
    """
    outcome = live.count(closed_set()).require("rejected")
    assert outcome.num_found and outcome.docs == [], outcome.explain("expected numFound > 0 and no documents")


def test_largest_page_is_returned_in_full(live: Live) -> None:
    """rows=2000 (search.MAX_PAGE_SIZE) returns 2000 documents.

    Depends on it: search.MAX_PAGE_SIZE, the most rows the client asks for per request.
    """
    outcome = everything(live, 0, MAX_PAGE_SIZE).require("rows=2000 was rejected")
    assert len(outcome.docs) == MAX_PAGE_SIZE, outcome.explain(f"expected {MAX_PAGE_SIZE} documents")


def test_rows_above_the_largest_page(live: Live, findings: Findings) -> None:
    """rows=2001 is capped at 2000 documents or rejected, so 2000 is the real maximum.

    Depends on it: search.MAX_PAGE_SIZE (if more are returned, it could be raised).
    """
    outcome = everything(live, 0, MAX_PAGE_SIZE + 1)
    findings.add("rows=2001", outcome.error or f"answered with {len(outcome.docs)} documents")
    assert not outcome.ok or len(outcome.docs) <= MAX_PAGE_SIZE, outcome.explain(
        f"rows=2001 returned {len(outcome.docs)} documents: MAX_PAGE_SIZE could be higher"
    )


def test_deep_start(live: Live) -> None:
    """start=1000000 is accepted and returns the record at that position.

    Depends on it: SearchBackend.run paging through large results with start/rows.
    """
    outcome = everything(live, DEEP_START, 1).require("a deep start was rejected")
    assert outcome.num_found is not None
    expected = 1 if outcome.num_found > DEEP_START else 0
    assert len(outcome.docs) == expected, outcome.explain(f"expected {expected} document(s)")


def test_bigquery_upload_with_bitset_filter(live: Live, live_transport: LiveTransport, sample: Outcome) -> None:
    """More bibcodes than config.max_inline_bibcodes are POSTed to /search/bigquery (fq={!bitset}, big-query/csv) and only uploaded records come back.

    Depends on it: search.SearchPlan.request for bigquery, used by Document.bibcode.in_(<large set>).
    """
    upload = sample.bibcodes
    query = Document.select(Document.bibcode, Document.score).where(Document.bibcode.in_(upload)).limit(5)
    assert plan(query).path == BIGQUERY_PATH
    documents = live.fetch(query)
    exchange = live_transport.last("search/bigquery")
    assert exchange is not None and exchange.method == "POST"
    assert exchange.params.get("fq") == "{!bitset}" and exchange.content_type == "big-query/csv"
    found = [doc.bibcode for doc in documents]
    assert len(found) == 5 and set(found) <= set(upload), f"expected 5 of the uploaded bibcodes, got {found}"


def test_bigquery_returns_score(live: Live, sample: Outcome) -> None:
    """/search/bigquery returns score when fl includes it.

    Depends on it: Document.score being readable on documents from a large bibcode set.
    """
    query = Document.select(Document.bibcode, Document.score).where(Document.bibcode.in_(sample.bibcodes)).limit(5)
    scores = [doc.score for doc in live.fetch(query)]
    assert scores and all(isinstance(score, float) for score in scores), f"scores: {scores}"


def test_bigquery_count_is_the_upload_size(live: Live, sample: Outcome) -> None:
    """With q=*:* and rows=0, /search/bigquery reports numFound equal to the number of uploaded bibcodes.

    Depends on it: Query.count() for a large bibcode set.
    """
    count = live.tally(Document.select().where(Document.bibcode.in_(sample.bibcodes)))
    assert count == len(sample.bibcodes), f"expected {len(sample.bibcodes)}, got {count}"


def test_bigquery_query_filters_the_upload(live: Live, sample: Outcome) -> None:
    """A q other than *:* filters the uploaded set: numFound is the size of the intersection.

    The filter is year == the most common year among the uploaded records.
    Depends on it: SearchPlan.q being applied to the uploaded bibcodes.
    """
    years = Counter(str(doc.get("year")) for doc in sample.docs)
    year, expected = years.most_common(1)[0]
    if expected == len(sample.docs):
        pytest.skip(f"every uploaded record is from {year}, so a year filter cannot show filtering")
    query = Document.select().where(Document.bibcode.in_(sample.bibcodes) & (Document.year == int(year)))
    count = live.tally(query)
    assert count == expected, f"expected {expected} uploaded records from {year}, got {count} (q={plan(query).q})"


def test_bigquery_sort_start_and_rows(live: Live, sample: Outcome) -> None:
    """/search/bigquery honours sort, start and rows: bibcode asc, offset 5, limit 5 gives the 6th to 10th bibcodes.

    Depends on it: SearchBackend.run paging bigquery searches like /search/query ones.
    """
    query = (
        Document.select(Document.bibcode)
        .where(Document.bibcode.in_(sample.bibcodes))
        .order_by(Document.bibcode.asc())
        .offset(5)
        .limit(5)
    )
    found = [doc.bibcode for doc in live.fetch(query)]
    expected = sorted(sample.bibcodes)[5:10]
    assert found == expected, f"expected {expected}, got {found}"
