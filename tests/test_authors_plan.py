"""Tests for planning per-author conditions (Document.authors): pure, no I/O."""

from __future__ import annotations

from typing import Any

import pytest
from search_fakes import bibcodes

from scixplorer import _config, search
from scixplorer.exceptions import NotLocallyEvaluable, QueryError, UnsupportedQueryError
from scixplorer.expr import EVERYTHING, And, Expr, Not, SolrLiteral, simplify
from scixplorer.models.affiliation import Affiliation
from scixplorer.models.author import Author
from scixplorer.models.document import Document
from scixplorer.search import MAX_RELATED_VALUES, SearchPlan, plan, to_solr


def ids(expr: Expr) -> list[str]:
    return [a.id for a in Affiliation.select().where(expr) if a.id is not None]


def any_of(field: str, values: list[str]) -> str:
    if len(values) == 1:
        return f'{field}:"{values[0]}"'
    return f"{field}:(" + " OR ".join(f'"{v}"' for v in values) + ")"


AU = ids(Affiliation.country == "Australia")
NZ = ids(Affiliation.country == "New Zealand")
#: An Australian institution, standing in for "Monash" (ids and names change with the data).
MONASH = Affiliation.lookup(AU[0])
assert MONASH is not None and MONASH.id is not None
MONASH_ID: str = MONASH.id


def plan_of(expr: Expr, *fields: Any) -> SearchPlan:
    return plan(Document.select(*fields).where(expr))


def test_fixture_data() -> None:
    assert 1 < len(NZ) < len(AU) <= MAX_RELATED_VALUES


# Exact: the condition is the Solr query; nothing is checked client-side.


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (Document.authors.any(Author.affiliation.country == "New Zealand"), any_of("aff_id", NZ)),
        (Document.authors.any(Author.name == "Casey, A"), 'author:"Casey, A"'),
        (Document.authors.any(Author.name.exact("Casey, A")), '=author:"Casey, A"'),
        (Document.authors.any(Author.orcid == "0000-0002-1825-0097"), 'orcid:"0000-0002-1825-0097"'),
        (Document.authors.any(Author.orcid.exact("0000-0002-1825-0097")), 'orcid:"0000-0002-1825-0097"'),
        (Document.authors.any(Author.orcid_pub == "0000-0002-1825-0097"), 'orcid_pub:"0000-0002-1825-0097"'),
        (Document.authors.any(Author.email.like("*@monash.edu")), "email:*@monash.edu"),
        (Document.authors.any(Author.aff == "Monash"), 'aff:"Monash"'),
        (Document.authors.any(Author.aff.exact("Monash")), '=aff:"Monash"'),
        (Document.authors.any(Author.aff_id == MONASH_ID), f'aff_id:"{MONASH_ID}"'),
        (Document.authors.any(Author.affiliation == MONASH), f'aff_id:"{MONASH_ID}"'),
        (Document.authors.any(Author.name.in_(["Casey, A", "Ness, M"])), 'author:("Casey, A" OR "Ness, M")'),
        (Document.authors[0].name == "Casey, A", 'pos(author:"Casey, A", 1)'),
        (Document.authors[1].name.exact("Casey, A"), 'pos(=author:"Casey, A", 2)'),
        (Document.authors[0].aff == "Monash", 'pos(aff:"Monash", 1)'),
        (Document.authors[0].affiliation.country == "New Zealand", f"pos({any_of('aff_id', NZ)}, 1)"),
        (Document.authors[0].affiliation == MONASH, f'pos(aff_id:"{MONASH_ID}", 1)'),
        (
            Document.authors[0].any((Author.name == "Casey, A") & (Author.affiliation == MONASH)),
            f'pos(author:"Casey, A", 1) AND pos(aff_id:"{MONASH_ID}", 1)',
        ),
        (
            Document.authors[2:3].any((Author.name == "Casey, A") & (Author.aff == "Monash")),
            'pos(author:"Casey, A", 3) AND pos(aff:"Monash", 3)',
        ),
        (Document.authors[0:3].any(Author.name == "Casey, A"), 'pos(author:"Casey, A", 1, 3)'),
        (
            Document.authors[0:3].any(Author.affiliation.country == "New Zealand"),
            f"pos({any_of('aff_id', NZ)}, 1, 3)",
        ),
        (Document.authors.any((Author.name == "A, B") | (Author.orcid == "0000-0002-1825-0097")), 'author:"A, B" OR orcid:"0000-0002-1825-0097"'),
        (
            Document.authors[0].any((Author.name == "A, B") | (Author.affiliation == MONASH)),
            f'pos(author:"A, B", 1) OR pos(aff_id:"{MONASH_ID}", 1)',
        ),
        (
            (Document.year == 2020) | ~Document.authors.any((Author.name == "A, B") | (Author.orcid == "0000-0002-1825-0097")),
            'year:2020 OR (*:* AND -(author:"A, B" OR orcid:"0000-0002-1825-0097"))',
        ),
        (
            ~Document.authors.any(Author.name == "A, B") & (Document.year == 2020),
            '-author:"A, B" AND year:2020',
        ),
        (
            (Document.year == 2020) & ~((Document.title == "x") | Document.authors.any(Author.orcid == "0000-0002-1825-0097")),
            'year:2020 AND -(title:"x" OR orcid:"0000-0002-1825-0097")',
        ),
        (
            Document.citations(Document.authors[0].name == "Casey, A"),
            'citations(pos(author:"Casey, A", 1))',
        ),
        (
            Document.authors.any(Author.name == "Casey, A") & Document.authors.any(Author.aff == "Monash"),
            'author:"Casey, A" AND aff:"Monash"',
        ),
    ],
)
def test_exact_conditions(expr: Expr, expected: str) -> None:
    search = plan_of(expr, Document.title)
    assert search.q == expected
    assert search.refinement is None and search.refinement_fl == () and not search.filters_locally
    assert search.fl == ("bibcode", "id", "scix_id", "title")
    assert search.path == "/search/query"


@pytest.mark.parametrize(
    ("long", "short"),
    [
        (Document.authors.any(Author.affiliation.country == "New Zealand"), Document.affiliation.country == "New Zealand"),
        (Document.authors.any(Author.affiliation.country == "Chile"), Affiliation.country == "Chile"),
        (Document.authors.any(Author.name == "Casey, A"), Document.author == "Casey, A"),
        (Document.authors[0].name == "Casey, A", Document.author[0] == "Casey, A"),
        (Document.authors[1:4].any(Author.name.exact("Casey, A")), Document.author[1:4].exact("Casey, A")),
        (Document.authors.any(Author.email.like("*@monash.edu")), Document.email.like("*@monash.edu")),
    ],
)
def test_single_conditions_compile_as_the_shorthand(long: Expr, short: Expr) -> None:
    assert to_solr(long) == to_solr(short)


def test_exact_notes() -> None:
    notes = plan_of(Document.authors[0].affiliation == MONASH).notes
    assert f'Document.authors[0].any(...): expressed exactly in the query as pos(aff_id:"{MONASH_ID}", 1)' in notes


# Refined: a necessary condition is sent, and the condition is checked client-side.

CASEY_AU = Document.authors.any((Author.name == "Casey, A") & (Author.affiliation.country == "Australia"))


def test_and_of_conditions_on_any_author_is_refined() -> None:
    search = plan_of(CASEY_AU, Document.title)
    assert search.q == f'author:"Casey, A" AND {any_of("aff_id", AU)}'
    assert search.refinement == CASEY_AU and search.filters_locally
    assert search.fl == ("aff_id", "author", "bibcode", "id", "scix_id", "title")
    assert search.refinement_fl == ("aff_id", "author")
    assert "also fetching author, aff_id for the client-side check" in search.notes
    (note,) = [n for n in search.notes if "checked client-side" in n]
    assert note.startswith(
        "Document.authors.any(...): checked client-side on each returned document (conditions joined "
        "with & may be met by different authors), reading author, aff_id; ADS is sent the necessary condition "
        'author:"Casey, A" AND aff_id:('
    )
    assert "initial/full-name rule" in note and "synonyms known only to ADS" in note
    assert note.endswith("Offset and limit are then applied locally")


def test_fields_already_fetched_are_not_added() -> None:
    search = plan_of(CASEY_AU)
    assert search.fl == plan(Document.select()).fl
    assert not any(note.startswith("also fetching") for note in search.notes)


def test_all_is_refined() -> None:
    expr = Document.authors.all(Author.affiliation.country == "New Zealand")
    search = plan_of(expr, Document.title)
    assert search.q == any_of("aff_id", NZ)
    assert search.refinement == expr
    assert search.refinement_fl == ("aff_id", "author")
    assert any("(all(...) needs every author checked)" in note for note in search.notes)


@pytest.mark.parametrize(
    ("name", "build", "q", "read"),
    [
        ("orcid", lambda: Document.authors[0].orcid == "0000-0002-1825-0097", 'orcid:"0000-0002-1825-0097"', ("author", "orcid_other", "orcid_pub", "orcid_user")),
        ("email", lambda: Document.authors[0].email == "x@y.z", 'email:"x@y.z"', ("author", "email")),
    ],
)
def test_positions_on_fields_not_searched_by_position(
    monkeypatch: pytest.MonkeyPatch, name: str, build: Any, q: str, read: tuple[str, ...]
) -> None:
    assert name not in search.POSITIONAL_FIELDS
    refined = plan_of(build(), Document.title)
    assert refined.q == q and refined.refinement == build()
    assert refined.refinement_fl == read
    assert any(f"({name} is not searched by position (search.POSITIONAL_FIELDS))" in n for n in refined.notes)

    monkeypatch.setattr(search, "POSITIONAL_FIELDS", search.POSITIONAL_FIELDS | {name})
    exact = plan_of(build(), Document.title)
    assert exact.q == f"pos({q}, 1)" and exact.refinement is None


def test_positional_fields_are_read_at_planning_time(monkeypatch: pytest.MonkeyPatch) -> None:
    query = Document.select().where(Document.authors[0].aff == "Monash")
    assert plan(query).q == 'pos(aff:"Monash", 1)'
    monkeypatch.setattr(search, "POSITIONAL_FIELDS", frozenset({"author"}))
    refined = plan(query)
    assert refined.q == 'aff:"Monash"' and refined.refinement is not None


def test_negated_refinement_sends_nothing_for_it() -> None:
    correlated = Document.authors.any((Author.name == "Casey, A") & (Author.affiliation == MONASH))
    search = plan_of(~correlated & (Document.year == 2020))
    assert search.q == "year:2020"
    assert search.refinement == Not(correlated)
    assert any(n.startswith("~Document.authors.any(...): checked client-side") for n in search.notes)
    assert any("nothing is sent to ADS for it (it is negated)" in n for n in search.notes)
    with pytest.raises(UnsupportedQueryError, match="download every document"):
        plan_of(~correlated)


@pytest.mark.parametrize(
    ("expr", "q", "reason"),
    [
        (Document.authors.any(~(Author.name == "Casey, A")), "year:2020", "a ~ inside it"),
        (
            Document.authors.any((Author.name == "Casey, A") & ~(Author.aff == "Monash")),
            'author:"Casey, A" AND year:2020',
            "conditions joined with & may be met by different authors",
        ),
        (
            Document.authors[0].any((Author.name == "Casey, A") & ~(Author.aff == "Monash")),
            'pos(author:"Casey, A", 1) AND year:2020',
            "a ~ inside it",
        ),
        (Document.authors.any(Author.position == 0), "year:2020", "Author.position cannot be searched by ADS"),
        (Document.authors[3].any(EVERYTHING), "year:2020", "whether there is any author at those positions"),
        (Document.authors.all(EVERYTHING), "year:2020", "whether there is any author"),
        (
            Document.authors.any((Author.name == "Casey, A") | ((Author.aff == "x") & (Author.email == "y"))),
            '(author:"Casey, A" OR (aff:"x" AND email:"y")) AND year:2020',
            "conditions joined with & may be met by different authors",
        ),
    ],
)
def test_other_refined_conditions(expr: Expr, q: str, reason: str) -> None:
    search = plan_of(expr & (Document.year == 2020))
    assert search.q == q
    assert search.refinement == simplify(expr)
    assert any(f"({reason})" in n for n in search.notes)


def test_two_refinements_are_both_checked() -> None:
    first = Document.authors.all(Author.affiliation.country == "New Zealand")
    second = ~Document.authors.any((Author.name == "Casey, A") & (Author.aff == "x"))
    search = plan_of(first & (Document.year == 2020) & second)
    assert search.q == f'{any_of("aff_id", NZ)} AND year:2020'
    assert search.refinement == And((first, second))
    assert search.refinement_fl == ("aff", "aff_id", "author")


def test_refinement_and_large_negated_bibcode_set() -> None:
    codes = bibcodes(60)
    search = plan_of(Document.not_in(codes) & CASEY_AU & (Document.year == 2020))
    assert search.exclude_bibcodes == frozenset(codes)
    assert search.refinement == CASEY_AU
    assert search.q == f'author:"Casey, A" AND {any_of("aff_id", AU)} AND year:2020'


def test_refinement_with_bigquery() -> None:
    codes = bibcodes(60)
    search = plan_of(Document.in_(codes) & CASEY_AU)
    assert search.bigquery_bibcodes == tuple(codes) and search.refinement == CASEY_AU
    assert search.path == "/search/bigquery"


def test_to_solr_gives_the_necessary_condition() -> None:
    assert to_solr(CASEY_AU) == f'author:"Casey, A" AND {any_of("aff_id", AU)}'


# Where a refined condition cannot go.


@pytest.mark.parametrize(
    "build",
    [
        lambda c: c | (Document.year == 2020),
        lambda c: Document.citations(c),
        lambda c: Document.trending(c & (Document.year == 2020)),
        lambda c: ~(c & (Document.year == 2020)),
        lambda c: (Document.year == 2020) & (c | (Document.title == "x")),
        lambda c: Document.pos(c, 1),
    ],
)
@pytest.mark.parametrize(
    "condition",
    [CASEY_AU, Document.authors.all(Author.name == "Casey, A"), Document.authors[0].email == "x@y.z"],
)
def test_refined_condition_elsewhere_is_unsupported(build: Any, condition: Expr) -> None:
    with pytest.raises(UnsupportedQueryError) as info:
        plan_of(build(condition))
    message = str(info.value)
    assert "needs a client-side check" in message
    assert "must be a top-level & condition" in message


def test_condition_that_cannot_be_evaluated_locally() -> None:
    expr = Document.authors.any((Author.name == SolrLiteral("Casey, A*")) & (Author.aff == "x"))
    with pytest.raises(NotLocallyEvaluable, match=r"Document\.authors\.any\(\.\.\.\) needs a client-side check"):
        plan_of(expr)
    assert to_solr(Document.authors.any(Author.name == SolrLiteral("Casey, A*"))) == "author:Casey, A*"


# Affiliations of an author resolve like document affiliations.


def test_author_affiliation_notes_and_limit() -> None:
    notes = plan_of(Document.authors.any(Author.affiliation.country == "Chile")).notes
    n = len(ids(Affiliation.country == "Chile"))
    assert any(
        note.startswith("Document.authors.any(...): Author.affiliation:") and f"matched as aff_id in {n} value(s)" in note
        for note in notes
    )
    keys = [f"A{i:05d}" for i in range(MAX_RELATED_VALUES + 1)]
    assert plan_of(Document.authors.any(Author.affiliation.in_(keys[:-1]))).q.startswith("aff_id:(")
    with pytest.raises(UnsupportedQueryError, match=f"at most {MAX_RELATED_VALUES} aff_id values"):
        plan_of(Document.authors.any(Author.affiliation.in_(keys)))
    with pytest.raises(UnsupportedQueryError, match=f"at most {MAX_RELATED_VALUES} aff_id values"):
        plan_of(Document.authors[0].any((Author.affiliation.in_(keys)) & (Author.name == "x")))


def test_author_affiliation_matching_nothing() -> None:
    nowhere = Author.affiliation.country == "Nowhere At All"
    for expr in (
        Document.authors.any(nowhere),
        Document.authors.all(nowhere),
        Document.authors.any(nowhere & (Author.name == "Casey, A")),
        Document.authors[0].affiliation.country == "Nowhere At All",
    ):
        assert plan_of(expr).empty, expr
    search = plan_of(~Document.authors.any(nowhere & (Author.name == "Casey, A")) & (Document.year == 2020))
    assert search.q == "year:2020" and search.refinement is None
    assert plan_of(Document.authors.any(nowhere | (Author.name == "Casey, A"))).q == 'author:"Casey, A"'


def test_relation_condition_that_cannot_be_evaluated_locally() -> None:
    with pytest.raises(NotLocallyEvaluable, match="Author.affiliation"):
        plan_of(Document.authors.any(Author.affiliation.country == SolrLiteral("NOW")))


def test_conditions_in_branches_that_cannot_match_are_not_resolved() -> None:
    keys = [f"A{i:05d}" for i in range(MAX_RELATED_VALUES + 1)]
    expr = (Document.journal.title == "No Such Journal") & Document.authors.any(Author.affiliation.in_(keys))
    assert plan_of(expr).empty


# The default collection.


@pytest.mark.parametrize(
    ("expr", "q"),
    [
        (Document.authors.any(Author.name == "Casey, A"), 'author:"Casey, A" AND database:"astronomy"'),
        (CASEY_AU, f'author:"Casey, A" AND {any_of("aff_id", AU)} AND database:"astronomy"'),
        (~CASEY_AU & (Document.year == 2020), 'year:2020 AND database:"astronomy"'),
        (Document.authors[0].orcid == "0000-0002-1825-0097", 'orcid:"0000-0002-1825-0097" AND database:"astronomy"'),
    ],
)
def test_default_collection_still_applies(monkeypatch: pytest.MonkeyPatch, expr: Expr, q: str) -> None:
    monkeypatch.setattr(_config.config, "collection", "astronomy")
    search = plan_of(expr)
    assert search.q == q
    assert any(note.startswith("limited to collection 'astronomy'") for note in search.notes)


# Queries without per-author conditions are unchanged.


def test_plans_without_authors_have_no_refinement() -> None:
    search = plan_of((Document.author == "Casey, A") & (Document.affiliation.country == "Chile"))
    assert search.refinement is None and search.refinement_fl == ()


# A search that would download the whole index.

WHOLE_INDEX = [
    (lambda: ~CASEY_AU, "~Document.authors.any(...)"),
    (lambda: Document.authors.any(~(Author.name == "Casey, A")), "Document.authors.any(...)"),
    (lambda: Document.authors.any(Author.position == 0), "Document.authors.any(...)"),
    (lambda: Document.authors[0].any(EVERYTHING), "Document.authors[0].any(...)"),
    (lambda: Document.not_in(bibcodes(60)), "excluding 60 bibcode(s)"),
    (lambda: Document.not_in(bibcodes(60)) & ~CASEY_AU, "excluding 60 bibcode(s) and ~Document.authors.any(...)"),
]


@pytest.mark.parametrize(("build", "what"), WHOLE_INDEX)
def test_filtering_the_whole_index_locally_is_refused(build: Any, what: str) -> None:
    with pytest.raises(UnsupportedQueryError) as info:
        plan_of(build())
    message = str(info.value)
    assert message.startswith(f"This query would download every document in SciX: {what} is done client-side")
    assert "Add a condition ADS can search" in message and "Document.year.between(2015, 2020)" in message
    with pytest.raises(UnsupportedQueryError, match="download every document"):
        Document.select().where(build()).explain()
    with pytest.raises(UnsupportedQueryError, match="download every document"):
        to_solr(build())


@pytest.mark.parametrize(("build", "what"), WHOLE_INDEX[:2])
def test_the_default_collection_does_not_count_as_a_condition(
    monkeypatch: pytest.MonkeyPatch, build: Any, what: str
) -> None:
    monkeypatch.setattr(_config.config, "collection", "astronomy")
    with pytest.raises(UnsupportedQueryError, match="apart from the default collection"):
        plan_of(build())


@pytest.mark.parametrize(
    ("expr", "q"),
    [
        (~CASEY_AU & Document.year.between(1000, 3000), "year:[1000 TO 3000]"),
        (Document.not_in(bibcodes(60)) & (Document.author == "Casey, A"), 'author:"Casey, A"'),
        (Document.in_(bibcodes(60)) & ~CASEY_AU, "*:*"),
    ],
)
def test_any_server_side_condition_or_an_upload_is_enough(expr: Expr, q: str) -> None:
    search = plan_of(expr)
    assert search.q == q and search.filters_locally
