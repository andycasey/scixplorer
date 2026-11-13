"""Tests for the Solr compiler: the golden strings of SPEC section 6, then the other rules."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from helpers import Journal, Paper

from scixplorer.exceptions import QueryError
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
    Order,
    Range,
    Raw,
    SolrLiteral,
    days_ago,
    now,
)
from scixplorer.fields import DateTimeField, StringField
from scixplorer.model import Model
from scixplorer.solr import QUOTED_FIELD_TYPES, compile, compile_order, format_value, quote


class Dated(Model):
    date = DateTimeField()


P = Paper
F = Paper._fields
A = P.year == 2020
B = P.bibstem == "ApJ"
C = P.author == "Ness, M"

GOLDEN = [
    (P.year == 2015, "year:2015"),
    (P.author == "Ness, M", 'author:"Ness, M"'),
    (P.author.exact("Ness, M"), '=author:"Ness, M"'),
    (P.title == 'the "big" one\\', r'title:"the \"big\" one\\"'),
    (P.title.like("JWST*"), "title:JWST*"),
    (P.title.like("star-form*"), r"title:star\-form*"),
    (P.author.like("Casey, A*"), 'author:"Casey, A*"'),
    (P.title.contains("astro"), "title:*astro*"),
    (P.year.between(2018, 2019), "year:[2018 TO 2019]"),
    (P.year >= 2020, "year:[2020 TO *]"),
    (P.year > 2020, "year:[2021 TO *]"),
    (P.year < 2020, "year:[* TO 2019]"),
    (P.citation_count_norm > 0.5, "citation_count_norm:[0.5 TO *] AND -citation_count_norm:0.5"),
    (P.entry_date >= datetime(2021, 12, 1), 'entry_date:["2021-12-01T00:00:00Z" TO *]'),
    (P.entry_date >= days_ago(7), "entry_date:[NOW-7DAYS TO *]"),
    (P.pubdate.between(date(2020, 1, 1), date(2020, 6, 1)), "pubdate:[2020-01 TO 2020-06]"),
    (Dated.date == datetime(2015, 7, 1), 'date:"2015-07-01T00:00:00Z"'),
    (P.public == True, "public:true"),  # noqa: E712
    (P.bibcode.in_(["A", "B"]), 'bibcode:("A" OR "B")'),
    (P.year.in_([2019, 2021]), "year:(2019 OR 2021)"),
    (P.bibcode.in_(["A"]), 'bibcode:"A"'),
    (P.year.in_([2019]), "year:2019"),
    ((P.year == 2020) & (P.bibstem == "ApJ"), 'year:2020 AND bibstem:"ApJ"'),
    ((A & B) | C, '(year:2020 AND bibstem:"ApJ") OR author:"Ness, M"'),
    ((P.year == 2020) & ~(P.bibstem == "ApJ"), 'year:2020 AND -bibstem:"ApJ"'),
    (~(P.year == 2020), "*:* AND -year:2020"),
    (P.year != 2020, "*:* AND -year:2020"),
    (~A & ~B, '*:* AND -year:2020 AND -bibstem:"ApJ"'),
    (A | ~B, 'year:2020 OR (*:* AND -bibstem:"ApJ")'),
    (~(A & B), '*:* AND -(year:2020 AND bibstem:"ApJ")'),
    (Func("citations", (P.bibcode == "X",)), 'citations(bibcode:"X")'),
    (
        Func("topn", (10, P.year == 2020, Order(F["citation_count"], True))),
        "topn(10, year:2020, citation_count desc)",
    ),
    (Func("trending", (Raw("exoplanets"),)), "trending(exoplanets)"),
    (P.author[0] == "Casey, A", 'pos(author:"Casey, A", 1)'),
    (P.author[1:3] == "Casey, A", 'pos(author:"Casey, A", 2, 3)'),
    (InLibrary("abc"), "docs(library/abc)"),
    (A & Raw("foo bar"), "year:2020 AND (foo bar)"),
    (Raw("foo bar"), "foo bar"),
    (Func("citations", (Raw("foo bar"),)), "citations(foo bar)"),
    (EVERYTHING, "*:*"),
    (NOTHING, "*:* AND -*:*"),
    (A | NOTHING, "year:2020 OR (*:* AND -*:*)"),
]


@pytest.mark.parametrize("expr, expected", GOLDEN, ids=[g[1] for g in GOLDEN])
def test_golden(expr, expected):
    assert compile(expr) == expected


# --- quoting and escaping ---------------------------------------------------


def test_escape_backslash_before_quote():
    assert quote('a\\"b') == '"a\\\\\\"b"'
    assert compile(P.title == "two \\\\ backslashes") == r'title:"two \\\\ backslashes"'


def test_quoting_lives_in_one_table():
    assert {t.__name__ for t in QUOTED_FIELD_TYPES} == {
        "TextField",
        "StringField",
        "NameField",
        "VirtualField",
        "DateTimeField",
    }
    assert format_value(F["year"], 3) == "3"
    assert format_value(F["bibcode"], "3") == '"3"'
    assert format_value(F["bibcode"], SolrLiteral("x y")) == "x y"


@pytest.mark.parametrize(
    "expr, expected",
    [
        (P.citation_count_norm == 0.25, "citation_count_norm:0.25"),
        (P.citation_count_norm == 2, "citation_count_norm:2"),
        (P.public == False, "public:false"),  # noqa: E712
        (P.full == "dark matter", 'full:"dark matter"'),
        (P.entry_date == now(), "entry_date:NOW"),
        (P.pubdate == "2020-06", "pubdate:2020-06"),
        (P.entry_date.in_([date(2020, 1, 1), days_ago(1)]), 'entry_date:("2020-01-01T00:00:00Z" OR NOW-1DAYS)'),
        (Journal.title == "x", 'title:"x"'),
    ],
)
def test_value_formatting(expr, expected):
    assert compile(expr) == expected


def test_solr_name_is_used():
    class Renamed(Model):
        norm = StringField(solr_name="citation_count_norm")

    assert compile(Renamed.norm == "x") == 'citation_count_norm:"x"'
    assert compile_order([Renamed.norm.desc()]) == "citation_count_norm desc"


def test_invalid_values_are_rejected_at_compile_time_too():
    with pytest.raises(QueryError):
        compile(Match(F["year"], "abc"))


# --- wildcard patterns ------------------------------------------------------


@pytest.mark.parametrize(
    "pattern, expected",
    [
        (r'a+b-c&d|e!f(g)h{i}j[k]l^m"n~o:p\\q/r*', r"a\+b\-c\&d\|e\!f\(g\)h\{i\}j\[k\]l\^m\"n\~o\:p\\q\/r*"),
        (r"lit\*eral\?s", r"lit\*eral\?s"),
        ("?ab*", "?ab*"),
        ("*", "*"),
        ('Ca\\\\sey "A\\*?', r'"Ca\\sey \"A\*?"'),
        ("a\tb", '"a\tb"'),
    ],
)
def test_like_patterns(pattern, expected):
    assert compile(Like(F["title"], pattern)) == f"title:{expected}"


def test_startswith_and_contains_escape_their_text():
    assert compile(P.title.startswith("what?")) == r"title:what\?*"
    assert compile(P.title.contains("a-b")) == r"title:*a\-b*"
    assert compile(P.author.startswith("Casey, A")) == 'author:"Casey, A*"'


def test_percent_alias():
    assert compile(P.title.like("JWST%")) == "title:JWST*"


# --- ranges -----------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, expected",
    [
        (P.year <= 2020, "year:[* TO 2020]"),
        (Range(F["year"], 2018, 2020, False, False), "year:[2019 TO 2019]"),
        (P.citation_count_norm < 0.5, "citation_count_norm:[* TO 0.5] AND -citation_count_norm:0.5"),
        (
            Range(F["citation_count_norm"], 0.1, 0.5, False, False),
            "citation_count_norm:[0.1 TO 0.5] AND -citation_count_norm:0.1 AND -citation_count_norm:0.5",
        ),
        (
            P.entry_date > datetime(2021, 12, 1),
            'entry_date:["2021-12-01T00:00:00Z" TO *] AND -entry_date:"2021-12-01T00:00:00Z"',
        ),
        (P.pubdate < date(2020, 6, 1), "pubdate:[* TO 2020-05]"),
        (P.pubdate > date(2020, 12, 1), "pubdate:[2021-01 TO *]"),
        (P.year > SolrLiteral("X"), "year:[X TO *] AND -year:X"),
        (Range(F["year"], 2018, None, include_hi=False), "year:[2018 TO *]"),
    ],
)
def test_ranges(expr, expected):
    assert compile(expr) == expected


def test_lowered_range_is_parenthesised_when_nested():
    expr = A & (P.citation_count_norm > 0.5)
    assert compile(expr) == "year:2020 AND (citation_count_norm:[0.5 TO *] AND -citation_count_norm:0.5)"
    expr = ~(P.citation_count_norm > 0.5)
    assert compile(expr) == "*:* AND -(citation_count_norm:[0.5 TO *] AND -citation_count_norm:0.5)"


# --- boolean structure -------------------------------------------------------


@pytest.mark.parametrize(
    "expr, expected",
    [
        (And(()), "*:*"),
        (Or(()), "*:* AND -*:*"),
        (And((A,)), "year:2020"),
        (Or((A,)), "year:2020"),
        (Or((And((A,)), B)), 'year:2020 OR bibstem:"ApJ"'),
        (And((Not(A),)), "*:* AND -year:2020"),
        (And((A, And((B, C)))), 'year:2020 AND (bibstem:"ApJ" AND author:"Ness, M")'),
        (Or((A, Or((B, C)))), 'year:2020 OR (bibstem:"ApJ" OR author:"Ness, M")'),
        (~A & B, '-year:2020 AND bibstem:"ApJ"'),
        (Not(Not(A)), "*:* AND -(*:* AND -year:2020)"),
        (~(A | B), '*:* AND -(year:2020 OR bibstem:"ApJ")'),
        (~Raw("foo bar"), "*:* AND -(foo bar)"),
        (A & ~InLibrary("abc"), "year:2020 AND -docs(library/abc)"),
        (A & ~(P.author[0] == "X"), 'year:2020 AND -pos(author:"X", 1)'),
        (In(F["year"], ()), "*:* AND -*:*"),
        (A & EVERYTHING, "year:2020 AND *:*"),
    ],
)
def test_boolean_structure(expr, expected):
    assert compile(expr) == expected


def test_operand_order_is_preserved():
    assert compile(C | B | A) == 'author:"Ness, M" OR bibstem:"ApJ" OR year:2020'


# --- functions ---------------------------------------------------------------


def test_position_with_other_builders():
    assert compile(P.author[0].like("Casey*")) == "pos(author:Casey*, 1)"
    assert compile(P.author[0].in_(["A", "B"])) == 'pos(author:("A" OR "B"), 1)'
    assert compile(P.author[0].exact("Casey, A")) == 'pos(=author:"Casey, A", 1)'
    assert compile(P.author[0] != "X") == '*:* AND -pos(author:"X", 1)'
    assert compile(Func("pos", (A, 2, 5))) == "pos(year:2020, 2, 5)"


def test_function_arguments():
    assert compile(Func("citations", (A & B,))) == 'citations(year:2020 AND bibstem:"ApJ")'
    assert compile(Func("citations", (~A,))) == "citations(*:* AND -year:2020)"
    assert compile(Func("topn", (5, "verbatim", Order(F["year"])))) == "topn(5, verbatim, year asc)"
    assert compile(A & Func("citations", (B,))) == 'year:2020 AND citations(bibstem:"ApJ")'


def test_unresolved_relation_is_an_error():
    with pytest.raises(QueryError, match="unresolved relation"):
        compile(P.journal == "ApJ")
    with pytest.raises(QueryError, match="unresolved relation"):
        compile(A & Func("citations", (P.journal.title == "x",)))


# --- sort -------------------------------------------------------------------


def test_compile_order():
    orders = (Order(F["citation_count"], True), Order(F["bibcode"]))
    assert compile_order(orders) == "citation_count desc,bibcode asc"
    assert compile_order([P.year.asc()]) == "year asc"
    assert compile_order(()) is None
    assert compile_order(iter([])) is None


# --- exact matching per match kind ---------------------------------------------

EXACT_GOLDEN = [
    (P.author == "Casey, A", 'author:"Casey, A"'),
    (P.author.exact("Casey, A"), '=author:"Casey, A"'),
    (P.author[1].exact("Casey, A"), 'pos(=author:"Casey, A", 2)'),
    (P.author[0] == "Casey, A", 'pos(author:"Casey, A", 1)'),
    (P.title.exact("star"), '=title:"star"'),
    (P.bibcode.exact("2005ApJ...622..759G"), 'bibcode:"2005ApJ...622..759G"'),
    (P.author.in_(["Casey, A", "Ness, M"], exact=True), '=author:"Casey, A" OR =author:"Ness, M"'),
    (A & P.author.in_(["Casey, A", "Ness, M"], exact=True), 'year:2020 AND (=author:"Casey, A" OR =author:"Ness, M")'),
    (P.bibcode.in_(["A", "B"], exact=True), 'bibcode:("A" OR "B")'),
    (~P.author.exact("Casey, A"), '*:* AND -=author:"Casey, A"'),
]


@pytest.mark.parametrize("expr, expected", EXACT_GOLDEN, ids=[g[1] for g in EXACT_GOLDEN])
def test_exact_golden(expr, expected):
    assert compile(expr) == expected


@pytest.mark.parametrize(
    "expr, expected",
    [
        (P.author.in_(["Casey, A", "Ness, M"]), 'author:("Casey, A" OR "Ness, M")'),
        (P.author.in_(["Casey, A"], exact=True), '=author:"Casey, A"'),
        (P.author.not_in(["Casey, A", "Ness, M"], exact=True), '*:* AND -(=author:"Casey, A" OR =author:"Ness, M")'),
        (P.author[0].in_(["A", "B"], exact=True), "pos(=author:\"A\" OR =author:\"B\", 1)"),
        (P.full.exact("dark matter"), '=full:"dark matter"'),
        (P.bibstem.exact("ApJ"), 'bibstem:"ApJ"'),
        (Match(F["year"], 2020, exact=True), "year:2020"),
        (In(F["year"], (2019, 2020), exact=True), "year:(2019 OR 2020)"),
        (In(F["author"], (), exact=True), "*:* AND -*:*"),
        (P.bibcode.in_(["A"], exact=True), 'bibcode:"A"'),
    ],
)
def test_exact_by_match_kind(expr, expected):
    assert compile(expr) == expected


def test_only_expanded_kinds_get_the_equals_prefix():
    samples = {str: "x", int: 1, float: 0.5, bool: True}
    for field in Paper._fields.values():
        if field.python_type not in samples:
            continue
        value = samples[field.python_type]
        compiled = compile(Match(field, value, exact=True))
        assert compiled.startswith("=") is (field.match_kind in ("text", "name")), field
