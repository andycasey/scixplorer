"""Tests for the document search planner (ads.search.plan and to_solr): pure, no I/O."""

from __future__ import annotations

from typing import Any

import pytest
from search_fakes import bibcodes

from ads import _config
from ads.exceptions import NotLocallyEvaluable, QueryError, UnsupportedQueryError
from ads.expr import EVERYTHING, Expr, SolrLiteral
from ads.fields import StringField
from ads.model import Model, Relation
from ads.models.affiliation import Affiliation
from ads.models.document import Document, SearchQuery
from ads.models.journal import Journal
from ads.models.library import Library
from ads.query import Query
from ads.search import MAX_RELATED_VALUES, SearchBackend, SearchPlan, plan, to_solr


def q(expr: Expr) -> str:
    return to_solr(expr)


def plan_of(expr: Expr | None = None, **kwargs: Any) -> SearchPlan:
    query = Document.select()
    if expr is not None:
        query = query.where(expr)
    return plan(query, **kwargs)


def journals(expr: Expr) -> list[str]:
    return [j.abbreviation for j in Journal.select().where(expr) if j.abbreviation is not None]


def affiliation_ids(expr: Expr) -> list[str]:
    return [a.id for a in Affiliation.select().where(expr) if a.id is not None]


def any_of(field: str, values: list[str]) -> str:
    if len(values) == 1:
        return f'{field}:"{values[0]}"'
    return f"{field}:(" + " OR ".join(f'"{v}"' for v in values) + ")"


@pytest.fixture
def library() -> Library:
    return Library(id="abc", name="Read already")


# Golden query strings.


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (Document.author == "Ness, M", 'author:"Ness, M"'),
        (Document.author[0] == "Casey, A", 'pos(author:"Casey, A", 1)'),
        (Document.author[1].exact("Casey, A"), 'pos(=author:"Casey, A", 2)'),
        (Document.author[0:3] == "Casey, A", 'pos(author:"Casey, A", 1, 3)'),
        (Document.first_author == "Casey, A", 'first_author:"Casey, A"'),
        (Document.bibcode.exact("2005ApJ...622..759G"), 'bibcode:"2005ApJ...622..759G"'),
        (Document.title.exact("star"), '=title:"star"'),
        (Document.year == 2015, "year:2015"),
        (Document.year > 2020, "year:[2021 TO *]"),
        (Document.volume == "808", 'volume:"808"'),
        (Document.id == "12312494", 'id:"12312494"'),
        (Document.full == "dark matter", 'full:"dark matter"'),
        (Document.lang == "en", 'lang:"en"'),
        (Document.title.like("JWST"), "title:JWST"),
        (Document.aff.like("Monash"), "aff:Monash"),
        (
            (Document.author == "Lastname, First")
            & (Document.year.between(2018, 2019) | (Document.year == 2015)),
            'author:"Lastname, First" AND (year:[2018 TO 2019] OR year:2015)',
        ),
        (
            Document.trending(Document.title == "exoplanets") & Document.author_count.between(1, 5),
            'trending(title:"exoplanets") AND author_count:[1 TO 5]',
        ),
        (Document.trending("exoplanets"), "trending(exoplanets)"),
        ((Document.year == 2000) & Document.title.like("JWST"), "year:2000 AND title:JWST"),
        (EVERYTHING, "*:*"),
    ],
)
def test_golden_query_strings(expr: Expr, expected: str) -> None:
    assert q(expr) == expected


def test_no_filter_matches_everything() -> None:
    assert plan(Document.select()).q == "*:*"


def test_plan_is_deterministic_and_frozen() -> None:
    query = Document.select().where(Document.journal.title.contains("astro") & (Document.year == 2020))
    first, second = plan(query), plan(query)
    assert first == second
    with pytest.raises(AttributeError):
        first.q = "x"  # type: ignore[misc]


def test_plan_needs_no_client_or_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "token", None)
    monkeypatch.delenv("ADS_API_TOKEN", raising=False)
    monkeypatch.delenv("ADS_DEV_KEY", raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")
    assert plan_of(Document.affiliation.country == "Australia").q.startswith("aff_id:(")


# Journals.


def test_journal_forms_compile_to_bibstem() -> None:
    apj = Journal.get(abbreviation="ApJ")
    forms = [
        Document.journal == apj,
        Document.journal.abbreviation == "ApJ",
        Document.journal == "ApJ",
        Document.journal.title == "The Astrophysical Journal",
        Journal.abbreviation == "ApJ",
    ]
    assert journals(Journal.title == "The Astrophysical Journal") == ["ApJ"]
    for form in forms:
        assert q(form) == 'bibstem:"ApJ"', form


def test_journal_title_contains() -> None:
    expected = any_of("bibstem", journals(Journal.title.contains("gravitation")))
    assert expected.startswith("bibstem:(")
    assert q(Document.journal.title.contains("gravitation")) == expected
    assert q(Journal.title.contains("gravitation")) == expected


def test_journal_combinations() -> None:
    apj = Journal.get(abbreviation="ApJ")
    expr = ((Document.journal.abbreviation == "MNRAS") & (Document.year == 2019)) | (
        (Document.journal == apj) & (Document.year == 2018)
    )
    assert q(expr) == '(bibstem:"MNRAS" AND year:2019) OR (bibstem:"ApJ" AND year:2018)'


def test_journal_key_given_directly_is_not_looked_up() -> None:
    assert Journal.lookup("NoSuchJ") is None
    assert q(Document.journal == "NoSuchJ") == 'bibstem:"NoSuchJ"'
    assert q(Document.journal.in_(["ApJ", "NoSuchJ", "ApJ"])) == 'bibstem:("ApJ" OR "NoSuchJ")'


def test_negated_journal() -> None:
    assert q(Document.journal != "ApJ") == '*:* AND -bibstem:"ApJ"'
    assert q((Document.year == 2020) & ~(Document.journal.title == "The Astrophysical Journal")) == (
        'year:2020 AND -bibstem:"ApJ"'
    )


def test_unknown_journal_title_gives_an_empty_plan() -> None:
    search = plan_of(Document.journal.title == "The Journal Nobody Publishes In")
    assert search.empty
    assert any("matches nothing" in note for note in search.notes)
    assert to_solr(Document.journal.title == "The Journal Nobody Publishes In") == "*:* AND -*:*"


def test_unknown_journal_under_or_is_dropped() -> None:
    expr = (Document.journal.title == "No Such Journal") | (Document.year == 2020)
    assert q(expr) == "year:2020"


def test_implicit_relation_inside_an_operator() -> None:
    assert q(Document.trending(Journal.abbreviation == "ApJ")) == 'trending(bibstem:"ApJ")'


def test_resolution_notes_name_the_relation_and_counts() -> None:
    search = plan_of(Document.journal.title.contains("gravitation"))
    n = len(journals(Journal.title.contains("gravitation")))
    assert any(note.startswith("Document.journal:") and f"{n} value(s)" in note for note in search.notes)


# Affiliations.


def test_affiliation_country() -> None:
    ids = affiliation_ids(Affiliation.country == "Australia")
    assert 1 < len(ids) <= MAX_RELATED_VALUES
    assert q(Document.affiliation.country == "Australia") == any_of("aff_id", ids)
    assert q(Affiliation.country == "Australia") == any_of("aff_id", ids)


def test_affiliation_abbreviation_and_object() -> None:
    mit = Affiliation.get(abbreviation="MIT")
    assert q(Document.affiliation.abbreviation == "MIT") == any_of("aff_id", affiliation_ids(Affiliation.abbreviation == "MIT"))
    assert q(Document.affiliation == mit) == f'aff_id:"{mit.id}"'


def test_too_many_related_keys() -> None:
    n = len(affiliation_ids(Affiliation.country == "USA"))
    assert n > MAX_RELATED_VALUES
    with pytest.raises(UnsupportedQueryError, match=f"{n} Affiliation rows matched") as info:
        plan_of(Document.affiliation.country == "USA")
    assert "Narrow" in str(info.value)


def test_related_key_limit_is_inclusive() -> None:
    ids = [f"A{i:05d}" for i in range(MAX_RELATED_VALUES + 1)]
    assert plan_of(Document.affiliation.in_(ids[:-1])).q.startswith("aff_id:(")
    with pytest.raises(UnsupportedQueryError):
        plan_of(Document.affiliation.in_(ids))


def test_related_predicate_that_cannot_be_evaluated_locally() -> None:
    with pytest.raises(NotLocallyEvaluable, match="Document.journal"):
        plan_of(Document.journal.title == SolrLiteral("NOW"))


def test_foreign_field_without_relation() -> None:
    with pytest.raises(QueryError, match="no relation to Library"):
        plan_of(Library.name == "x")


class _Twice(Model):
    _key = "bibcode"
    _backend = SearchBackend()
    id = StringField("id")
    bibcode = StringField("bibcode")
    bibstem = StringField("bibstem", multivalued=True)
    other = StringField("other", multivalued=True)
    journal = Relation(Journal, key="bibstem", related_key="abbreviation", resolve=lambda _: None)
    other_journal = Relation(Journal, key="other", related_key="abbreviation", resolve=lambda _: None)


def test_foreign_field_with_two_relations_is_ambiguous() -> None:
    with pytest.raises(QueryError, match="ambiguous"):
        plan(Query(_Twice).where(Journal.title == "x"))
    assert plan(Query(_Twice).where(_Twice.other_journal == "ApJ")).q == 'other:"ApJ"'


class _Curated(Model):
    _key = "bibcode"
    _backend = SearchBackend()
    id = StringField("id")
    bibcode = StringField("bibcode")
    library_ids = StringField("library ids", multivalued=True)
    library = Relation(Library, key="library_ids", related_key="id", resolve=lambda _: None)


def test_related_model_needing_requests_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ads.client._default_client", None)
    with pytest.raises(UnsupportedQueryError, match="without requests to ADS"):
        plan(Query(_Curated).where(_Curated.library.name == "Gaia"))
    assert plan(Query(_Curated).where(_Curated.library == "abc")).q == 'library_ids:"abc"'


# Libraries.


def test_library_docs_strategy(library: Library) -> None:
    assert q(Document.in_(library) & (Document.year == 2020)) == "docs(library/abc) AND year:2020"
    assert q(~Document.in_(library)) == "*:* AND -docs(library/abc)"
    assert q(Document.not_in(library)) == "*:* AND -docs(library/abc)"
    assert any("docs(library/<id>)" in note for note in plan_of(Document.in_(library)).notes)


def test_library_membership_never_reads_bibcodes(monkeypatch: pytest.MonkeyPatch, library: Library) -> None:
    monkeypatch.setattr(Library, "bibcodes", property(lambda self: pytest.fail("bibcodes read")))
    assert q(Document.in_(library)) == "docs(library/abc)"


def test_library_without_id() -> None:
    with pytest.raises(QueryError, match="has no id"):
        Document.in_(Library(name="local only"))


def test_limitations_doc_expression_is_one_query() -> None:
    lib_1, lib_2, lib_3 = (Library(id=i) for i in ("L1", "L2", "L3"))
    expr = (
        (Document.in_(lib_1) & (Document.year == 2020))
        | ((Document.year == 2021) & Document.in_(lib_2) & Document.not_in(lib_3))
        | (Document.year == 2019)
    )
    search = plan_of(expr)
    assert search.bigquery_bibcodes is None and search.exclude_bibcodes is None
    assert search.q == (
        "(docs(library/L1) AND year:2020) OR (year:2021 AND docs(library/L2) AND -docs(library/L3)) OR year:2019"
    )


def test_library_bibcodes_strategy(monkeypatch: pytest.MonkeyPatch, library: Library) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    small = bibcodes(3)
    search = plan_of(Document.in_(library) & (Document.year == 2020), library_bibcodes={"abc": small})
    assert search.q == any_of("bibcode", small) + " AND year:2020"
    assert any("library abc: 3 bibcode(s)" in note for note in search.notes)

    large = bibcodes(80)
    search = plan_of(Document.in_(library) & (Document.year == 2020), library_bibcodes={"abc": large})
    assert search.q == "year:2020" and search.bigquery_bibcodes == tuple(large)

    search = plan_of(~Document.in_(library), library_bibcodes={"abc": large})
    assert search.q == "*:*" and search.exclude_bibcodes == frozenset(large)

    with pytest.raises(ValueError, match="needs the bibcodes of library 'abc'"):
        plan_of(Document.in_(library))
    with pytest.raises(UnsupportedQueryError, match="library_membership"):
        plan_of(Document.in_(library) | (Document.year == 2020), library_bibcodes={"abc": large})
    with pytest.raises(UnsupportedQueryError, match="needs the bibcodes"):
        to_solr(Document.in_(library))


def test_bibcodes_strategy_skips_libraries_that_cannot_matter(monkeypatch: pytest.MonkeyPatch, library: Library) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    assert plan_of((Document.journal.title == "No Such Journal") & Document.in_(library)).empty
    expr = ((Document.journal.title == "No Such Journal") & Document.in_(library)) | (Document.year == 2020)
    assert plan_of(expr).q == "year:2020"


def test_bibcodes_strategy_notes_each_library_once(monkeypatch: pytest.MonkeyPatch, library: Library) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    expr = (Document.in_(library) & (Document.year == 2020)) | (Document.in_(library) & (Document.year == 2021))
    notes = plan_of(expr, library_bibcodes={"abc": bibcodes(2)}).notes
    assert sum(note.startswith("library abc:") for note in notes) == 1


def test_empty_library_with_bibcodes_strategy(monkeypatch: pytest.MonkeyPatch, library: Library) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "bibcodes")
    assert plan_of(Document.in_(library), library_bibcodes={"abc": []}).empty


def test_unknown_library_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "library_membership", "magic")
    with pytest.raises(ValueError, match="library_membership"):
        plan_of(Document.year == 2020)


# Large bibcode sets.


def test_small_bibcode_set_stays_inline() -> None:
    codes = bibcodes(50)
    search = plan_of(Document.bibcode.in_(codes) & (Document.year == 2020))
    assert search.bigquery_bibcodes is None
    assert search.q == any_of("bibcode", codes) + " AND year:2020"


def test_large_bibcode_set_uses_bigquery() -> None:
    codes = bibcodes(60)
    search = plan_of(Document.bibcode.in_(codes) & (Document.year == 2020))
    assert search.q == "year:2020"
    assert search.bigquery_bibcodes == tuple(codes)
    assert search.path == "/search/bigquery"
    assert plan_of(Document.in_(codes)).q == "*:*"


def test_max_inline_bibcodes_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.config, "max_inline_bibcodes", 2)
    assert plan_of(Document.bibcode.in_(bibcodes(3))).bigquery_bibcodes is not None


def test_two_large_sets_are_intersected_in_the_first_sets_order() -> None:
    first = bibcodes(80)
    second = list(reversed(first[20:])) + bibcodes(10, prefix="2021MNRAS")
    search = plan_of(Document.in_(first) & (Document.year == 2020) & Document.bibcode.in_(second))
    assert search.bigquery_bibcodes == tuple(first[20:])
    assert search.q == "year:2020"
    assert any("intersection of 2 bibcode sets" in note for note in search.notes)


def test_disjoint_large_sets_give_an_empty_plan() -> None:
    search = plan_of(Document.in_(bibcodes(60)) & Document.in_(bibcodes(60, prefix="2021MNRAS")))
    assert search.empty


def test_large_negated_set_is_excluded_client_side() -> None:
    codes = bibcodes(60)
    search = plan_of(~Document.bibcode.in_(codes) & (Document.year == 2020))
    assert search.q == "year:2020"
    assert search.path == "/search/query"
    assert search.exclude_bibcodes == frozenset(codes)
    assert plan_of(Document.not_in(codes)).q == "*:*"


def test_large_positive_and_negated_sets() -> None:
    keep, drop = bibcodes(80), bibcodes(70)[10:]
    search = plan_of(Document.in_(keep) & Document.not_in(drop))
    assert search.bigquery_bibcodes == tuple(keep[:10]) + tuple(keep[70:])
    assert search.exclude_bibcodes is None
    assert search.q == "*:*"


@pytest.mark.parametrize(
    "build",
    [
        lambda codes: Document.in_(codes) | (Document.year == 2020),
        lambda codes: ~(Document.in_(codes) & (Document.year == 2020)),
        lambda codes: Document.citations(Document.in_(codes)),
        lambda codes: (Document.year == 2020) & (Document.in_(codes) | (Document.year == 2021)),
    ],
)
def test_large_set_elsewhere_is_unsupported(build: Any) -> None:
    with pytest.raises(UnsupportedQueryError) as info:
        plan_of(build(bibcodes(60)))
    message = str(info.value)
    assert "top-level & condition" in message and "Document.in_(library)" in message


# Sorting and fields.


def test_sort_tiebreakers() -> None:
    query = Document.select()
    assert plan(query).sort == "score desc,id asc"
    assert plan(query.order_by(Document.citation_count.desc())).sort == "citation_count desc,id asc"
    assert plan(query.order_by(Document.bibcode)).sort == "bibcode asc"
    assert plan(query.order_by(Document.year.desc(), Document.id.desc())).sort == "year desc,id desc"
    assert any("so pages are stable" in note for note in plan(query).notes)


@pytest.mark.parametrize("field", [Document.title, Document.full])
def test_sort_by_unsortable_field(field: Any) -> None:
    with pytest.raises(QueryError, match="Cannot sort"):
        plan(Document.select().order_by(field))


def test_default_fields() -> None:
    fl = plan(Document.select()).fl
    names = list(Document._fields)
    assert list(fl) == sorted(fl, key=names.index)
    assert {"bibcode", "id", "title", "score", "aff_id", "bibstem"} <= set(fl)
    assert not set(fl) & Document._deferred
    assert "lang" not in fl and "full" not in fl and "abs" not in fl


def test_selected_fields_always_include_bibcode_and_id() -> None:
    assert plan(Document.select(Document.title, "citation_count")).fl == ("bibcode", "citation_count", "id", "title")
    assert plan(Document.select(Document.citation)).fl == ("bibcode", "citation", "id")


@pytest.mark.parametrize("field", ["lang", "full"])
def test_selecting_a_search_only_field(field: str) -> None:
    with pytest.raises(QueryError, match="search-only"):
        plan(Document.select(field))


# to_solr and SearchQuery.


def test_to_solr_accepts_queries() -> None:
    assert to_solr(Document.select().where(Document.year == 2020).limit(3)) == "year:2020"


def test_search_query() -> None:
    query = SearchQuery(
        q="author:'Ness, M' AND year:2018", fl=["bibcode", "title"], sort="citation_count desc", rows=5
    )
    search = plan(query)
    assert search.q == "author:'Ness, M' AND year:2018"
    assert search.fl == ("bibcode", "id", "title")
    assert search.sort == "citation_count desc,id asc"
    assert (query.limit_, query.offset_) == (5, 0)


def test_search_query_filters_and_sort() -> None:
    query = SearchQuery("star", fl="bibcode, year", sort="year asc, bibcode desc", start=10, fq=["year:2020", "doctype:article"])
    search = plan(query)
    assert search.q == "(star) AND (year:2020) AND (doctype:article)"
    assert search.sort == "year asc,bibcode desc"
    assert query.offset_ == 10 and query.limit_ is None
    assert plan(SearchQuery("star", fq="year:2020")).q == "(star) AND (year:2020)"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"sort": "nonsense desc"}, "unknown field"),
        ({"sort": "year sideways"}, "Cannot parse"),
        ({"sort": "year"}, "Cannot parse"),
        ({"fl": ["no_such_field"]}, "no field"),
    ],
)
def test_search_query_errors(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(QueryError, match=match):
        SearchQuery("star", **kwargs)
