"""Tests for running per-author conditions: client-side refinement while streaming, count, explain, async.

The fake search service does not evaluate ``q``: it returns every row, as if
each satisfied the necessary condition sent to ADS. Rows are crafted so that
some of them are such false positives, which the refinement must drop.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from search_fakes import FakeSearch, bibcodes

from scixplorer import _config, search
from scixplorer.exceptions import LazyLoadWarning
from scixplorer.expr import Expr
from scixplorer.models.affiliation import Affiliation
from scixplorer.models.author import Author
from scixplorer.models.document import Document


def ids(country: str) -> list[str]:
    return [a.id for a in Affiliation.select().where(Affiliation.country == country) if a.id is not None]


AU, NZ = ids("Australia"), ids("New Zealand")
CASEY_AU = Document.authors.any((Author.name == "Casey, A") & (Author.affiliation.country == "Australia"))


@pytest.fixture(autouse=True)
def no_warnings() -> Iterator[None]:
    """Refined documents carry the fields the check read: nothing is lazily loaded."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


def row(i: int, **fields: Any) -> dict[str, Any]:
    return {"bibcode": bibcodes(i + 1)[i], "id": str(1000 + i), "title": [f"Paper {i}"], "year": 2020, **fields}


#: (fields, kept by CASEY_AU) for the kinds of row the server could return for its necessary condition.
CASEY_ROWS: list[tuple[dict[str, Any], bool]] = [
    # Casey at an Australian institution.
    ({"author": ["Casey, A.", "Ness, M."], "aff_id": [AU[0], NZ[0]]}, True),
    # Casey in New Zealand, a different author in Australia: correlated wrongly.
    ({"author": ["Casey, A.", "Ness, M."], "aff_id": [NZ[0], AU[0]]}, False),
    # Casey with two affiliations, one Australian.
    ({"author": ["Ness, M.", "Casey, A."], "aff_id": ["-", f"{NZ[1]}; {AU[1]}"]}, True),
    # The name matches only by its initial.
    ({"author": ["Casey, Andrew R."], "aff_id": [AU[2]]}, True),
    # Arrays of unequal length: Casey has no aff_id entry.
    ({"author": ["Hogg, D.", "Casey, A."], "aff_id": [AU[0]]}, False),
    # An Australian author, but no Casey.
    ({"author": ["Ness, M."], "aff_id": [AU[0]]}, False),
    # A longer aff_id array than author list.
    ({"author": ["Casey, Alice"], "aff_id": [f"{AU[3]}", NZ[0], NZ[1]]}, True),
    # Casey whose affiliation is "-".
    ({"author": ["Casey, A."], "aff_id": ["-"]}, False),
]


def casey_rows(n: int) -> tuple[list[dict[str, Any]], list[str]]:
    """``n`` rows cycling through :data:`CASEY_ROWS`, and the bibcodes of those to keep, in order."""
    rows, kept = [], []
    for i in range(n):
        fields, keep = CASEY_ROWS[i % len(CASEY_ROWS)]
        rows.append(row(i, **fields))
        if keep:
            kept.append(rows[-1]["bibcode"])
    return rows, kept


def page_size(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    monkeypatch.setattr(_config.config, "page_size", size)


def bibcodes_of(documents: list[Document]) -> list[str | None]:
    return [d.bibcode for d in documents]


# Refinement.


def test_refinement_drops_false_positives(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 200)
    rows, kept = casey_rows(len(CASEY_ROWS))
    fake = FakeSearch(rows)
    documents = Document.select(Document.title).where(CASEY_AU).using(fake.client()).all()
    assert bibcodes_of(documents) == kept
    (request,) = fake.searches
    params = dict(request.url.params)
    assert params["q"] == 'author:"Casey, A" AND aff_id:(' + " OR ".join(f'"{i}"' for i in AU) + ")"
    assert params["fl"] == "aff_id,author,bibcode,id,scix_id,title"
    assert (params["start"], params["rows"]) == ("0", "200")
    # The fields the check read are simply loaded on the documents.
    assert documents[0].author == ["Casey, A.", "Ness, M."] and documents[0].aff_id == [AU[0], NZ[0]]


@pytest.mark.parametrize(("offset", "limit"), [(0, None), (2, 3), (0, 4), (3, 100), (6, 1), (40, 3), (0, 0)])
def test_refinement_with_offset_and_limit_across_pages(
    monkeypatch: pytest.MonkeyPatch, offset: int, limit: int | None
) -> None:
    page_size(monkeypatch, 4)
    rows, kept = casey_rows(30)
    fake = FakeSearch(rows)
    query = Document.select(Document.title).where(CASEY_AU).offset(offset).limit(limit).using(fake.client())
    expected = kept[offset:] if limit is None else kept[offset : offset + limit]
    assert bibcodes_of(list(query)) == expected
    if limit == 0:
        assert fake.requests == []
        return
    assert all(r.url.params["rows"] == "4" for r in fake.searches)
    assert [r.url.params["start"] for r in fake.searches] == [str(4 * i) for i in range(len(fake.searches))]
    all_bibcodes = [r["bibcode"] for r in rows]
    # Paging stops at the page holding the last row needed, or at the end.
    pages = all_bibcodes.index(expected[-1]) // 4 + 1 if limit is not None and len(expected) == limit else 8
    assert len(fake.searches) == pages


def test_refinement_count(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 4)
    rows, kept = casey_rows(30)
    fake = FakeSearch(rows)
    query = Document.select(Document.title).where(CASEY_AU).using(fake.client())
    assert query.count() == len(kept)
    assert len(fake.searches) == 8
    assert all(r.url.params["fl"] == "bibcode,aff_id,author" for r in fake.searches)
    assert query.offset(3).limit(4).count() == 4
    assert query.offset(len(kept) - 2).count() == 2


def test_count_and_rows_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 3)
    rows, _ = casey_rows(17)
    fake = FakeSearch(rows)
    for expr in (CASEY_AU, ~CASEY_AU, Document.authors.all(Author.name.like("Casey*"))):
        query = Document.select().where(expr).using(fake.client())
        assert query.count() == len(query.all())


def test_all(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 200)
    cases = [
        ({"author": ["A, A", "B, B"], "aff_id": [NZ[0], f"{AU[0]}; {NZ[1]}"]}, True),
        ({"author": ["A, A", "B, B"], "aff_id": [NZ[0], AU[0]]}, False),
        ({"author": ["A, A", "B, B"], "aff_id": [NZ[0], "-"]}, False),
        ({"author": ["A, A", "B, B"], "aff_id": [NZ[0]]}, False),
        ({"author": [], "aff_id": [NZ[0]]}, False),
        ({"aff_id": [NZ[0]]}, False),
        ({"author": ["A, A"], "aff_id": [NZ[2]]}, True),
    ]
    rows = [row(i, **fields) for i, (fields, _) in enumerate(cases)]
    fake = FakeSearch(rows)
    expr = Document.authors.all(Author.affiliation.country == "New Zealand")
    documents = Document.select(Document.title).where(expr).using(fake.client()).all()
    assert bibcodes_of(documents) == [r["bibcode"] for r, (_, keep) in zip(rows, cases) if keep]
    assert fake.params()["q"] == "aff_id:(" + " OR ".join(f'"{i}"' for i in NZ) + ")"


@pytest.mark.parametrize(
    ("name", "build", "rows", "kept"),
    [
        (
            "orcid",
            lambda: Document.authors[0].orcid == "0000-0002-1825-0097",
            [
                {"author": ["A, A", "B, B"], "orcid_pub": ["0000-0002-1825-0097", "-"]},
                {"author": ["A, A", "B, B"], "orcid_pub": ["-", "0000-0002-1825-0097"]},
                {"author": ["A, A"], "orcid_pub": ["-"], "orcid_user": ["0000-0002-1825-0097"]},
                {"author": ["A, A"], "orcid_other": ["0000-0002-1825-0097"]},
                {"author": ["A, A", "B, B"], "orcid_user": ["-", "0000-0002-1825-0097"]},
            ],
            [0, 2, 3],
        ),
        (
            "email",
            lambda: Document.authors[0].email == "x@y.z",
            [
                {"author": ["A, A", "B, B"], "email": ["x@y.z", "-"]},
                {"author": ["A, A", "B, B"], "email": ["-", "x@y.z"]},
                {"author": ["A, A"], "email": ["X@Y.Z"]},
            ],
            [0, 2],
        ),
    ],
)
def test_positions_on_fields_not_searched_by_position(
    monkeypatch: pytest.MonkeyPatch, name: str, build: Callable[[], Expr], rows: list[dict[str, Any]], kept: list[int]
) -> None:
    page_size(monkeypatch, 200)
    docs = [row(i, **fields) for i, fields in enumerate(rows)]
    fake = FakeSearch(docs)
    query = Document.select(Document.title).where(build()).using(fake.client())
    assert bibcodes_of(query.all()) == [docs[i]["bibcode"] for i in kept]
    assert "pos(" not in fake.params()["q"]

    # Searched by position: the server decides, so every row it returns is kept.
    monkeypatch.setattr(search, "POSITIONAL_FIELDS", search.POSITIONAL_FIELDS | {name})
    fake = FakeSearch(docs)
    documents = Document.select(Document.title).where(build()).offset(1).limit(1).using(fake.client()).all()
    assert bibcodes_of(documents) == [docs[1]["bibcode"]]
    assert fake.params()["q"].startswith("pos(")
    assert (fake.params()["start"], fake.params()["rows"]) == ("1", "1")


def test_negated_refinement(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 200)
    monash = Affiliation.lookup(AU[0])
    expr = ~Document.authors.any((Author.name == "Casey, A") & (Author.affiliation == monash)) & (Document.year == 2020)
    rows = [
        row(0, author=["Casey, A.", "Ness, M."], aff_id=[AU[0], NZ[0]]),
        row(1, author=["Casey, A.", "Ness, M."], aff_id=[NZ[0], AU[0]]),
        row(2, author=["Ness, M."], aff_id=[AU[0]]),
        row(3, author=["Ness, M.", "Casey, Andrew"], aff_id=[NZ[0], f"{NZ[1]}; {AU[0]}"]),
        row(4),
    ]
    fake = FakeSearch(rows)
    documents = Document.select(Document.title).where(expr).using(fake.client()).all()
    assert bibcodes_of(documents) == [rows[i]["bibcode"] for i in (1, 2, 4)]
    assert fake.params()["q"] == "year:2020"


def test_refinement_and_large_negated_bibcode_set(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 4)
    rows, kept = casey_rows(30)
    excluded = set(bibcodes(30)[::3]) | set(bibcodes(60, prefix="2021MNRAS"))
    fake = FakeSearch(rows)
    query = Document.select(Document.title).where(Document.not_in(sorted(excluded)) & CASEY_AU).using(fake.client())
    survivors = [b for b in kept if b not in excluded]
    assert 0 < len(survivors) < len(kept)
    assert bibcodes_of(query.offset(1).limit(3).all()) == survivors[1:4]
    assert query.count() == len(survivors)
    assert all(r.url.params["q"].startswith('author:"Casey, A" AND aff_id:(') for r in fake.searches)


def test_refinement_with_bigquery(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 200)
    rows, kept = casey_rows(70)
    fake = FakeSearch(rows)
    uploaded = bibcodes(70)[5:65]
    documents = Document.select(Document.title).where(Document.in_(uploaded) & CASEY_AU).using(fake.client()).all()
    assert bibcodes_of(documents) == [b for b in kept if b in uploaded]
    assert fake.searches[0].method == "POST"


def test_exact_conditions_are_ordinary_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 200)
    rows, _ = casey_rows(40)
    fake = FakeSearch(rows)
    expr = Document.authors.any((Author.name == "Casey, A") | (Author.orcid == "X")) & (Document.authors[0].aff == "M")
    documents = Document.select(Document.title).where(expr).offset(30).limit(5).using(fake.client()).all()
    assert bibcodes_of(documents) == [r["bibcode"] for r in rows[30:35]]
    assert dict(fake.searches[0].url.params) == {
        "q": '(author:"Casey, A" OR orcid:"X") AND pos(aff:"M", 1)',
        "fl": "bibcode,id,scix_id,title",
        "sort": "score desc,id asc",
        "start": "30",
        "rows": "5",
    }
    assert len(fake.searches) == 1
    assert Document.select().where(expr).using(fake.client()).count() == 40
    assert fake.params()["rows"] == "0"


def test_refined_documents_belong_to_one_page(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 200)
    rows, kept = casey_rows(len(CASEY_ROWS))
    for r in rows:
        r["abstract"] = f"About {r['bibcode']}"
    fake = FakeSearch(rows)
    documents = Document.select(Document.title).where(CASEY_AU).using(fake.client()).all()
    page = documents[0]._page
    assert page is not None and page.members() == documents
    with pytest.warns(LazyLoadWarning) as caught:
        assert documents[1].abstract == f"About {kept[1]}"
    assert len(caught) == 1 and f"for {len(kept)} document(s)" in str(caught[0].message)


def test_explain_refinement(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 200)
    explanation = Document.select(Document.title).where(CASEY_AU).offset(5).limit(3).explain()
    (request,) = explanation.requests
    assert request.params is not None
    assert (request.params["start"], request.params["rows"]) == (0, 200)
    assert request.params["fl"] == "aff_id,author,bibcode,id,scix_id,title"
    text = str(explanation)
    assert "checked client-side on each returned document" in text
    assert "reading author, aff_id" in text and "initial/full-name rule" in text


# Async.


def test_async_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    page_size(monkeypatch, 4)
    rows, kept = casey_rows(30)
    sync_fake, async_fake = FakeSearch(rows), FakeSearch(rows)
    query = Document.select(Document.title).where(Document.not_in(bibcodes(60)[50:]) & CASEY_AU).offset(2).limit(5)
    sync_rows = query.using(sync_fake.client()).all()
    sync_count = query.using(sync_fake.client()).count()

    async def main() -> tuple[list[Document], int]:
        return await query.using(async_fake.client()).aall(), await query.using(async_fake.client()).acount()

    async_rows, async_count = asyncio.run(main())
    assert bibcodes_of(async_rows) == bibcodes_of(sync_rows) == kept[2:7]
    assert async_count == sync_count == 5
    assert [(r.method, str(r.url)) for r in async_fake.requests] == [(r.method, str(r.url)) for r in sync_fake.requests]
