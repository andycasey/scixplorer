"""Tests for evaluating expressions in Python and for in-memory sorting."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers import INSTITUTES, JOURNALS, Institute, Journal, Paper

from scixplorer.evaluate import matches, predicate, sort_rows
from scixplorer.exceptions import NotLocallyEvaluable
from scixplorer.expr import (
    EVERYTHING,
    NOTHING,
    And,
    Func,
    In,
    InLibrary,
    Like,
    Match,
    Not,
    Or,
    Range,
    Raw,
    SolrLiteral,
    days_ago,
    now,
)

P = Paper
F = Paper._fields
UTC = timezone.utc


# --- logic and missing values -----------------------------------------------


def test_constants_and_logic():
    paper = Paper(year=2020, bibcode="X")
    assert matches(EVERYTHING, paper)
    assert not matches(NOTHING, paper)
    assert matches((P.year == 2020) & (P.bibcode == "X"), paper)
    assert not matches((P.year == 2020) & (P.bibcode == "Y"), paper)
    assert matches((P.year == 1) | (P.bibcode == "X"), paper)
    assert not matches(Or(()), paper)
    assert matches(And(()), paper)
    assert matches(P.year != 1999, paper)
    assert not matches(~(P.year == 2020), paper)


def test_none_never_matches_a_positive_predicate():
    paper = Paper()
    for expr in [
        P.year == 2020,
        P.year > 0,
        P.year.in_([2020]),
        P.title == "x",
        P.title.like("*"),
        P.bibcode.exact("X"),
    ]:
        assert not matches(expr, paper), expr
        assert matches(~expr, paper), expr


def test_multivalued_any_semantics():
    paper = Paper(author=["Casey, A", "Ness, M"], bibstem=["ApJ", "ApJL"])
    assert matches(P.author == "Ness", paper)
    assert matches(P.bibstem == "ApJL", paper)
    assert not matches(P.bibstem == "MNRAS", paper)
    assert matches(P.bibstem != "MNRAS", paper)
    assert not matches(P.bibstem != "ApJ", paper)


def test_nested_lists_tuples_and_none_elements():
    obj = SimpleNamespace(bibstem=[None, ("A", ["B", None]), [], ("C",)])
    assert matches(P.bibstem == "B", obj)
    assert matches(P.bibstem == "C", obj)
    assert not matches(P.bibstem == "D", obj)
    assert not matches(P.bibstem == "D", SimpleNamespace(bibstem=[None, []]))
    assert matches(P.bibstem.not_in(["D"]), SimpleNamespace(bibstem=[None]))


def test_value_read_with_getattr_on_any_object():
    assert matches(P.year == 2020, SimpleNamespace(year=2020))


# --- Match ------------------------------------------------------------------


def test_string_field_equality_is_case_sensitive_and_whole_value():
    paper = Paper(bibcode="2020ApJ...1..1C")
    assert matches(P.bibcode == "2020ApJ...1..1C", paper)
    assert not matches(P.bibcode == "2020apj...1..1c", paper)
    assert not matches(P.bibcode == "2020ApJ", paper)
    assert matches(P.bibcode.exact("2020ApJ...1..1C"), paper)
    assert not matches(P.bibcode.exact("2020APJ...1..1C"), paper)


def test_text_field_is_case_insensitive_phrase_containment():
    paper = Paper(title=["The Big-Bang theory, revisited"])
    assert matches(P.title == "big bang", paper)
    assert matches(P.title == "BIG-BANG THEORY", paper)
    assert matches(P.title == "theory revisited", paper)
    assert matches(P.title == "the", paper)
    assert not matches(P.title == "bang big", paper)
    assert not matches(P.title == "big theory", paper)
    assert not matches(P.title == "bi", paper)
    assert not matches(P.title == "...", paper)


def test_text_field_exact_is_whole_value_ignoring_case_and_spacing():
    paper = Paper(author=["Casey,  Andrew R.", "Ness, M"])
    assert matches(P.author.exact("casey, andrew r."), paper)
    assert matches(P.author.exact(" Ness,\tM "), paper)
    assert not matches(P.author.exact("Casey"), paper)
    assert matches(P.author == "Casey", paper)


def test_numeric_and_boolean_match():
    paper = Paper(year=2020, citation_count_norm=0.5, public=False)
    assert matches(P.year == 2020, paper)
    assert matches(P.citation_count_norm == 0.5, paper)
    assert matches(P.public == False, paper)  # noqa: E712
    assert not matches(P.public == True, paper)  # noqa: E712


def test_datetime_match_coerces_dates_and_time_zones():
    paper = Paper(entry_date=datetime(2021, 12, 1, tzinfo=UTC))
    assert matches(P.entry_date == datetime(2021, 12, 1), paper)
    assert matches(P.entry_date == date(2021, 12, 1), paper)
    assert matches(P.entry_date == datetime(2021, 12, 1, 10, tzinfo=timezone(timedelta(hours=10))), paper)
    assert not matches(P.entry_date == datetime(2021, 12, 1, 1), paper)
    assert matches(P.entry_date == datetime(2021, 12, 1), Paper(entry_date=date(2021, 12, 1)))
    assert matches(P.entry_date == date(2021, 12, 1), Paper(entry_date=datetime(2021, 12, 1)))


def test_date_field_match_has_month_precision():
    paper = Paper(pubdate=date(2020, 6, 15))
    assert matches(P.pubdate == date(2020, 6, 1), paper)
    assert matches(P.pubdate == "2020-06", paper)
    assert matches(P.pubdate == datetime(2020, 6, 30, 12), paper)
    assert not matches(P.pubdate == date(2020, 7, 15), paper)
    assert matches(P.pubdate == "2020-06", SimpleNamespace(pubdate="2020-06-00"))


def test_in_uses_match_semantics():
    paper = Paper(title=["Stellar streams"], year=2020)
    assert matches(P.title.in_(["galaxies", "STREAMS"]), paper)
    assert not matches(P.title.in_(["galaxies"]), paper)
    assert matches(P.year.in_([2019, 2020]), paper)
    assert not matches(P.year.in_([]), paper)


# --- Like -------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern, value, expected",
    [
        ("JWST*", "jwst observations", True),
        ("JWST*", "The JWST", False),
        ("*jwst*", "The JWST era", True),
        ("J?ST", "JWST", True),
        ("J?ST", "JWWST", False),
        ("a*b", "a\nb", True),
        (r"100\*", "100*", True),
        (r"100\*", "1000", False),
        (r"what\?", "what?", True),
        (r"what\?", "whats", False),
        (r"back\\slash*", "back\\slash!", True),
        ("50%", "50%", True),
        ("a.c", "abc", False),
        ("(x)+", "(x)+", True),
    ],
)
def test_like(pattern, value, expected):
    assert matches(Like(F["bibcode"], pattern), Paper(bibcode=value)) is expected


def test_like_percent_alias_and_helpers():
    paper = Paper(title=["Gaia DR3: what? 50% done"])
    assert matches(P.title.like("gaia%"), paper)
    assert matches(P.title.contains("WHAT?"), paper)
    assert not matches(P.title.contains("what?!"), paper)
    assert matches(P.title.startswith("Gaia DR3"), paper)
    assert not matches(P.title.startswith("DR3"), paper)


def test_like_on_non_string_values_uses_str():
    assert matches(Like(F["year"], "20*"), Paper(year=2020))
    assert not matches(Like(F["year"], "19*"), Paper(year=2020))


# --- Range ------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, values",
    [
        (P.year.between(2018, 2020), {2017: False, 2018: True, 2020: True, 2021: False}),
        (P.year > 2018, {2018: False, 2019: True}),
        (P.year >= 2018, {2017: False, 2018: True}),
        (P.year < 2018, {2017: True, 2018: False}),
        (P.year <= 2018, {2018: True, 2019: False}),
        (Range(F["year"], 2018, 2020, False, False), {2018: False, 2019: True, 2020: False}),
    ],
)
def test_integer_ranges(expr, values):
    for value, expected in values.items():
        assert matches(expr, Paper(year=value)) is expected, value


@pytest.mark.parametrize(
    "expr, values",
    [
        (P.citation_count_norm > 0.5, {0.5: False, 0.50001: True, 0.4: False}),
        (P.citation_count_norm >= 0.5, {0.5: True, 0.49: False}),
        (P.citation_count_norm < 0.5, {0.5: False, 0.49: True}),
        (P.citation_count_norm.between(0.1, 0.2), {0.1: True, 0.2: True, 0.21: False}),
    ],
)
def test_float_ranges(expr, values):
    for value, expected in values.items():
        assert matches(expr, Paper(citation_count_norm=value)) is expected, value


def test_datetime_ranges():
    expr = P.entry_date > datetime(2021, 12, 1)
    assert not matches(expr, Paper(entry_date=datetime(2021, 12, 1, tzinfo=UTC)))
    assert matches(expr, Paper(entry_date=datetime(2021, 12, 1, 0, 0, 1, tzinfo=UTC)))
    expr = P.entry_date >= date(2021, 12, 1)
    assert matches(expr, Paper(entry_date=datetime(2021, 12, 1, tzinfo=UTC)))
    assert not matches(expr, Paper(entry_date=datetime(2021, 11, 30, 23, tzinfo=UTC)))
    plus_ten = timezone(timedelta(hours=10))
    assert not matches(expr, Paper(entry_date=datetime(2021, 12, 1, 9, tzinfo=plus_ten)))


def test_date_ranges_have_month_precision():
    expr = P.pubdate.between(date(2020, 1, 1), date(2020, 6, 1))
    assert matches(expr, Paper(pubdate=date(2020, 6, 30)))
    assert matches(expr, Paper(pubdate=date(2020, 1, 1)))
    assert not matches(expr, Paper(pubdate=date(2019, 12, 31)))
    assert not matches(expr, Paper(pubdate=date(2020, 7, 1)))
    expr = P.pubdate > date(2020, 6, 1)
    assert not matches(expr, Paper(pubdate=date(2020, 6, 30)))
    assert matches(expr, Paper(pubdate=date(2020, 7, 1)))


# --- Related ----------------------------------------------------------------


def test_related_single_object():
    apj = Paper(bibcode="2020ApJ...900..1C")
    nature = Paper(bibcode="2020Natur.900..1C")
    assert matches(P.journal == "ApJ", apj)
    assert matches(P.journal == JOURNALS["ApJ"], apj)
    assert matches(P.journal.title.contains("astrophysical"), apj)
    assert not matches(P.journal.title.contains("royal"), apj)
    assert not matches(P.journal == "ApJ", nature)
    assert matches(P.journal != "ApJ", nature)
    assert matches(P.journal.in_(["MNRAS", "ApJ"]), apj)


def test_related_list_with_tuples_and_none():
    paper = Paper(aff_id=["-", "A3", "A2; A1"])
    assert paper.institute == [None, INSTITUTES["A3"], (INSTITUTES["A2"], INSTITUTES["A1"])]
    assert matches(P.institute.country == "Australia", paper)
    assert matches(P.institute.country == "Germany", paper)
    assert not matches(P.institute.country == "France", paper)
    assert matches(P.institute == "A1", paper)
    assert not matches(P.institute.country == "Australia", Paper(aff_id=["-", "A3"]))
    assert matches(P.institute.country != "Australia", Paper(aff_id=["-", "A3"]))


def test_related_none_and_empty():
    assert not matches(P.institute.country == "Australia", Paper())
    assert not matches(P.institute == "A1", Paper(aff_id=[]))
    assert matches(~(P.institute == "A1"), Paper())


def test_related_plain_list_and_tuple_values():
    owner = SimpleNamespace(journal=[JOURNALS["MNRAS"], (None, JOURNALS["ApJ"])])
    assert matches(P.journal == "ApJ", owner)
    assert not matches(P.journal == "AJ", owner)


# --- not locally evaluable --------------------------------------------------


@pytest.mark.parametrize(
    "expr, match",
    [
        (Func("citations", (P.year == 2020,)), "citations"),
        (P.author[0] == "Casey, A", "pos"),
        (Raw("year:2020"), "Raw"),
        (InLibrary("abc"), "InLibrary"),
        (P.full == "dark matter", "search-only"),
        (P.full.like("dark*"), "search-only"),
        (P.doctype == "article", "search-only"),
        (P.doctype.in_(["article"]), "search-only"),
        (P.entry_date >= days_ago(7), "NOW-7DAYS"),
        (P.entry_date < now(), "NOW"),
        (P.entry_date == now(), "NOW"),
        (P.entry_date.in_([date(2020, 1, 1), now()]), "NOW"),
        (P.journal.title == SolrLiteral("X"), "X"),
        (Journal.title.like("x") & Raw("y"), "Raw"),
    ],
)
def test_not_locally_evaluable(expr, match):
    with pytest.raises(NotLocallyEvaluable, match=match):
        matches(expr, Paper(year=2020))


def test_not_locally_evaluable_is_raised_even_if_short_circuited():
    with pytest.raises(NotLocallyEvaluable):
        matches(NOTHING & Raw("x"), Paper())
    with pytest.raises(NotLocallyEvaluable):
        matches(EVERYTHING | Not(InLibrary("abc")), Paper())
    with pytest.raises(NotLocallyEvaluable):
        predicate(P.journal.title == now())


def test_predicate_is_reusable():
    test = predicate((Institute.country == "Australia") | (Institute.id == "A2"))
    assert [i.id for i in INSTITUTES.values() if test(i)] == ["A1", "A2"]


def test_unknown_node_is_type_error():
    with pytest.raises(TypeError):
        predicate(Match)  # type: ignore[arg-type]


# --- sorting ----------------------------------------------------------------


def rows(*pairs):
    return [SimpleNamespace(id=i, year=year, bibcode=bibcode) for i, (year, bibcode) in enumerate(pairs)]


def ids(result):
    return [row.id for row in result]


def test_sort_single_key_both_directions_with_none_last():
    data = rows((2020, "b"), (None, "a"), (2018, "c"), (2020, "d"))
    assert ids(sort_rows(data, [P.year.asc()])) == [2, 0, 3, 1]
    assert ids(sort_rows(data, [P.year.desc()])) == [0, 3, 2, 1]


def test_sort_multiple_keys_is_stable():
    data = rows((2020, "b"), (2018, "z"), (2020, "a"), (None, "a"), (2020, "b"), (2018, None))
    assert ids(sort_rows(data, [P.year.desc(), P.bibcode.asc()])) == [2, 0, 4, 1, 5, 3]
    assert ids(sort_rows(data, [P.bibcode.desc(), P.year.asc()])) == [1, 0, 4, 2, 3, 5]
    assert ids(sort_rows(data, [])) == [0, 1, 2, 3, 4, 5]


def test_sort_does_not_mutate_input_and_accepts_iterables():
    data = rows((2020, "a"), (2019, "b"))
    result = sort_rows(iter(data), iter([P.year.asc()]))
    assert ids(result) == [1, 0]
    assert ids(data) == [0, 1]


def test_sort_normalises_dates():
    data = [
        SimpleNamespace(id=0, entry_date=datetime(2021, 1, 2, tzinfo=UTC)),
        SimpleNamespace(id=1, entry_date=date(2021, 1, 1)),
        SimpleNamespace(id=2, entry_date=datetime(2021, 1, 1, 12)),
    ]
    assert ids(sort_rows(data, [P.entry_date.asc()])) == [1, 2, 0]


def test_sort_by_search_only_field_is_not_locally_evaluable():
    with pytest.raises(NotLocallyEvaluable):
        sort_rows([Paper()], [P.full.asc()])


# --- matching by kind: names, text, keywords ---------------------------------

RECORDS = [
    "Murray, Stephen S.",
    "Murray, Stephen",
    "Murray, S. S.",
    "Murray, S.",
    "Murray, Stephen D.",
    "Murray, S. D.",
    "Murray, Sarah S.",
    "Murray, Scott",
]

# The examples on the ADS search-syntax help page: query -> records it matches.
ADS_NAME_TABLE = {
    ("murray, s", False): set(RECORDS),
    ("murray, stephen", False): set(RECORDS[:6]),
    ("murray, stephen s", False): set(RECORDS[:4]),
    ("murray, s", True): {"Murray, S."},
    ("murray, stephen", True): {"Murray, Stephen"},
    ("murray, stephen s", True): {"Murray, Stephen S."},
}


@pytest.mark.parametrize(
    "query, exact, record, expected",
    [
        (query, exact, record, record in matched)
        for (query, exact), matched in ADS_NAME_TABLE.items()
        for record in RECORDS
    ],
)
def test_name_matching_reproduces_the_ads_table(query, exact, record, expected):
    expr = P.author.exact(query) if exact else P.author == query
    assert matches(expr, Paper(author=[record])) is expected


@pytest.mark.parametrize(
    "query, record, expected",
    [
        ("Murray", "Murray, Stephen S.", True),
        ("murray", "Murray", True),
        ("Murray, S", "Murray", True),
        ("Murray, S", "Murrays, S.", False),
        ("Murray, S", "Stephen, Murray", False),
        ("van der Berg, A", "Van  der Berg, Anna", True),
        ("Murray, S S", "Murray, S.S.", True),
        ("Murray, Stephen Scott", "Murray, S. Sam", False),
        ("Murray, S. D.", "Murray, Stephen", True),
    ],
)
def test_name_matching_edge_cases(query, record, expected):
    assert matches(P.author == query, Paper(author=[record])) is expected


def test_exact_name_ignores_case_periods_and_spacing_only():
    assert matches(P.author.exact("MURRAY,  s."), Paper(author=["Murray, S."]))
    assert matches(P.author.exact("Murray, S S"), Paper(author=["Murray, S.S."]))
    assert not matches(P.author.exact("Murray, S"), Paper(author=["Murray, S. S."]))
    assert not matches(P.author.exact("Murray"), Paper(author=["Murray, S."]))


def test_name_matching_is_any_over_multivalued_and_positions_are_not_local():
    paper = Paper(author=["Casey, Andrew R.", "Ness, Melissa"])
    assert matches(P.author == "Ness, M", paper)
    assert not matches(P.author.exact("Ness, M"), paper)
    with pytest.raises(NotLocallyEvaluable):
        matches(P.author[1].exact("Ness, M"), paper)


def test_in_honours_exact():
    paper = Paper(author=["Murray, Stephen S."])
    assert matches(P.author.in_(["Ness, M", "Murray, S"]), paper)
    assert not matches(P.author.in_(["Ness, M", "Murray, S"], exact=True), paper)
    assert matches(P.author.in_(["Ness, M", "murray, stephen s."], exact=True), paper)
    assert matches(P.author.not_in(["Murray, S"], exact=True), paper)


def test_text_exact_is_whole_value_without_phrase_containment():
    paper = Paper(title=["The  Big Bang"])
    assert matches(P.title == "big bang", paper)
    assert not matches(P.title.exact("big bang"), paper)
    assert matches(P.title.exact("the big BANG"), paper)
    assert matches(P.title.in_(["nothing", "the big bang"], exact=True), paper)


def test_keyword_exact_is_plain_equality():
    paper = Paper(bibcode="2005ApJ...622..759G", bibstem=["ApJ"])
    assert matches(P.bibcode.exact("2005ApJ...622..759G"), paper)
    assert not matches(P.bibcode.exact("2005apj...622..759g"), paper)
    assert matches(P.bibstem.in_(["ApJ", "AJ"], exact=True), paper)
    assert matches(In(F["year"], (2019, 2020), exact=True), Paper(year=2020))
