"""The Solr syntax the compiler emits: phrases, ranges, dates, negation, value lists and wildcards (items 1-6).

Counts are compared within a fixed closed set S (``closed_set()``: AJ papers
of 2014-2017), so the identities hold however the index grows. Each test
first checks that the client still compiles its expression to the form
under test.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import pytest

from scixplorer import solr
from scixplorer.expr import NOTHING, Expr, days_ago
from scixplorer.models.affiliation import Affiliation
from scixplorer.models.document import Document
from scixplorer.search import MAX_RELATED_VALUES, QUERY_PATH, plan

from .harness import Live, Outcome, closed_set, compiled, month_of
from .probes import ProbeResults
from .support import Findings

pytestmark = pytest.mark.live

S = 'bibstem:"AJ" AND year:[2014 TO 2017]'

#: Sizes of aff_id lists tried, largest first, until one is accepted.
AFF_ID_SIZES = (MAX_RELATED_VALUES, 100)


def partition(live: Live, whole: Expr, *parts: Expr) -> None:
    """Assert numFound(whole) == the sum of numFound(part), every part non-empty."""
    outcomes = [live.count(expr) for expr in (whole, *parts)]
    counts = [outcome.require("a query of this partition was rejected").num_found for outcome in outcomes]
    listing = "\n".join(f"  {count:>9}  {outcome.q}" for count, outcome in zip(counts, outcomes))
    assert counts[0] == sum(counts[1:]) and all(counts[1:]), (
        f"expected the first count to be the sum of the others, each non-zero:\n{listing}"
    )


def titles(doc: dict[str, Any]) -> list[str]:
    """The record's titles and alternate titles."""
    return [str(t) for key in ("title", "alternate_title") for t in (doc.get(key) or [])]


def test_fielded_phrase(probes: ProbeResults) -> None:
    """A plain fielded phrase, title:"w1 w2 ...", matches a record whose title starts with those words.

    Depends on it: solr.QUOTED_FIELD_TYPES quoting TextField values as phrases.
    """
    probes.check("phrase")


def test_integer_equality(probes: ProbeResults) -> None:
    """An integer equality, year:YYYY, matches a record of that year (year is a string in Solr).

    Depends on it: IntegerField values being sent bare (not in solr.QUOTED_FIELD_TYPES).
    """
    probes.check("integer_equality")


def test_integer_range_partition_on_year(live: Live) -> None:
    """Integer ranges [a TO b] are inclusive, * leaves a side open, and year > x is sent as [x+1 TO *].

    numFound(S AND year <= 2015) + numFound(S AND year > 2015) == numFound(S).
    Depends on it: IntegerField.step and solr._compile_range.
    """
    partition(
        live,
        compiled(closed_set(), S),
        compiled(closed_set() & (Document.year <= 2015), f"{S} AND year:[* TO 2015]"),
        compiled(closed_set() & (Document.year > 2015), f"{S} AND year:[2016 TO *]"),
    )


def test_integer_range_partition_on_citation_count(live: Live) -> None:
    """Open-ended integer ranges on citation_count partition a set, with an exclusive bound sent as +1.

    numFound(S AND cc >= 0) == numFound(S AND cc <= 10) + numFound(S AND cc > 10).
    Depends on it: IntegerField.step and solr._compile_range.
    """
    cc = Document.citation_count
    partition(
        live,
        compiled(closed_set() & (cc >= 0), f"{S} AND citation_count:[0 TO *]"),
        compiled(closed_set() & (cc <= 10), f"{S} AND citation_count:[* TO 10]"),
        compiled(closed_set() & (cc > 10), f"{S} AND citation_count:[11 TO *]"),
    )


def test_open_ended_range_on_author_count(live: Live) -> None:
    """author_count:[101 TO *] (author_count > 100) returns only records with more than 100 authors.

    Depends on it: IntegerField ranges on author_count.
    """
    expr = compiled(Document.author_count > 100, "author_count:[101 TO *]")
    outcome = live.search(expr, fields=("author_count",), rows=3).require("rejected")
    counts = [doc.get("author_count") for doc in outcome.docs]
    assert outcome.num_found and all(isinstance(n, int) and n > 100 for n in counts), outcome.explain(
        f"expected records with more than 100 authors; author_count of the rows: {counts}"
    )


def test_float_exclusive_bound(live: Live) -> None:
    """A float exclusive bound, x > v, is sent as (x:[v TO *] AND -x:v) and excludes exactly v.

    numFound(S AND x >= 0.5) == numFound(S AND x > 0.5) + numFound(S AND x == 0.5)
    for x = citation_count_norm. Depends on it: solr._compile_range for fields
    without Field.step (FloatField, DateTimeField).
    """
    x = Document.citation_count_norm
    outcomes = [
        live.count(compiled(closed_set() & (x >= 0.5), f"{S} AND citation_count_norm:[0.5 TO *]")),
        live.count(compiled(
            closed_set() & (x > 0.5),
            f"{S} AND (citation_count_norm:[0.5 TO *] AND -citation_count_norm:0.5)",
        )),
        live.count(compiled(closed_set() & (x == 0.5), f"{S} AND citation_count_norm:0.5")),
    ]
    counts = [outcome.require("rejected").num_found for outcome in outcomes]
    listing = "\n".join(f"  {count:>9}  {outcome.q}" for count, outcome in zip(counts, outcomes))
    assert counts[0] == counts[1] + counts[2] and counts[1], (
        f"expected numFound(>=) == numFound(>) + numFound(==), with numFound(>) non-zero:\n{listing}"
    )


def test_datetime_range_with_quoted_timestamps(live: Live) -> None:
    """Datetime ranges with double-quoted timestamps, entry_date:["2016-01-01T00:00:00Z" TO *], are accepted and exact.

    numFound(S AND entry_date >= 1990) == numFound(S AND entry_date < T) +
    numFound(S AND entry_date >= T); the exclusive side is sent as
    (entry_date:[* TO "T"] AND -entry_date:"T"). Depends on it: DateTimeField
    in solr.QUOTED_FIELD_TYPES, and solr._compile_range.
    """
    when = datetime(2016, 1, 1, tzinfo=timezone.utc)
    stamp = '"2016-01-01T00:00:00Z"'
    entry = Document.entry_date
    partition(
        live,
        compiled(closed_set() & (entry >= datetime(1990, 1, 1)), f'{S} AND entry_date:["1990-01-01T00:00:00Z" TO *]'),
        compiled(closed_set() & (entry < when), f"{S} AND (entry_date:[* TO {stamp}] AND -entry_date:{stamp})"),
        compiled(closed_set() & (entry >= when), f"{S} AND entry_date:[{stamp} TO *]"),
    )


def test_relative_datetime_literal(live: Live) -> None:
    """entry_date:[NOW-7DAYS TO *] (entry_date >= days_ago(7)) finds records created in the last week.

    Depends on it: SolrLiteral values being sent verbatim (expr.days_ago, solr.format_value).
    """
    expr = compiled(Document.entry_date >= days_ago(7), "entry_date:[NOW-7DAYS TO *]")
    outcome = live.search(expr, fields=("entry_date",), rows=3).require("rejected")
    since = datetime.now(timezone.utc) - timedelta(days=8)
    dates = [str(doc.get("entry_date")) for doc in outcome.docs]
    recent = [d for d in dates if datetime.fromisoformat(d.replace("Z", "+00:00")) >= since]
    assert outcome.num_found and len(recent) == len(dates), outcome.explain(
        f"expected records created within the last week; their entry_date: {dates}"
    )


def test_month_precision_date_range(live: Live) -> None:
    """pubdate:[2020-01 TO 2020-06] covers January to June 2020 inclusive, June included.

    Checked on ApJ, sorted by date descending: every pubdate is in range and
    the latest is in June. Depends on it: DateField.to_solr (YYYY-MM, unquoted)
    and DateField.step.
    """
    expr = compiled(
        (Document.bibstem == "ApJ") & Document.pubdate.between(date(2020, 1, 1), date(2020, 6, 1)),
        'bibstem:"ApJ" AND pubdate:[2020-01 TO 2020-06]',
    )
    outcome = live.search(expr, fields=("pubdate", "date"), order=(Document.date.desc(),), rows=3)
    outcome.require("rejected")
    months = [month_of(str(doc.get("pubdate"))) for doc in outcome.docs]
    inside = all(date(2020, 1, 1) <= month <= date(2020, 6, 1) for month in months)
    assert months and inside and months[0] == date(2020, 6, 1), outcome.explain(
        f"expected pubdates from 2020-01 to 2020-06, the latest in June; got {[m.isoformat()[:7] for m in months]}"
    )


def test_negation_alone(live: Live) -> None:
    """A negation on its own is sent as *:* AND -clause and returns records not matching the clause.

    Depends on it: solr._compile_and anchoring a purely negative group with *:*.
    """
    expr = compiled(~(Document.bibstem == "ApJ"), '*:* AND -bibstem:"ApJ"')
    outcome = live.search(expr, fields=("bibstem",), rows=3).require("rejected")
    bad = [doc.get("bibcode") for doc in outcome.docs if "ApJ" in (doc.get("bibstem") or [])]
    assert outcome.num_found and not bad, outcome.explain(f"expected records without bibstem ApJ; these have it: {bad}")


def test_and_not(live: Live) -> None:
    """a AND -b excludes b: numFound(S AND -year:[* TO 2015]) + numFound(S AND year:[* TO 2015]) == numFound(S).

    Depends on it: solr._compile_and writing a Not beside a positive operand as -clause.
    """
    partition(
        live,
        compiled(closed_set(), S),
        compiled(closed_set() & ~(Document.year <= 2015), f"{S} AND -year:[* TO 2015]"),
        compiled(closed_set() & (Document.year <= 2015), f"{S} AND year:[* TO 2015]"),
    )


def test_or_not(live: Live) -> None:
    """a OR (*:* AND -b) is a valid nested negation: (b OR (*:* AND -b)) within S finds all of S.

    Depends on it: solr._compile_and anchoring a negation nested in an OR with *:*.
    """
    b = Document.citation_count <= 10
    expr = compiled(
        closed_set() & (b | ~b),
        f"{S} AND (citation_count:[* TO 10] OR (*:* AND -citation_count:[* TO 10]))",
    )
    whole = live.total(closed_set(), "rejected")
    outcome = live.count(expr).require("rejected")
    assert outcome.num_found == whole, outcome.explain(f"expected all {whole} records of S")


def test_not_of_group(live: Live) -> None:
    """*:* AND -(a AND b) negates a whole group: ((a AND b) OR (*:* AND -(a AND b))) within S finds all of S.

    Depends on it: solr._operand parenthesising a negated group.
    """
    ab = (Document.year == 2015) & (Document.citation_count <= 10)
    group = "year:2015 AND citation_count:[* TO 10]"
    expr = compiled(closed_set() & (ab | ~ab), f"{S} AND (({group}) OR (*:* AND -({group})))")
    whole = live.total(closed_set(), "rejected")
    outcome = live.count(expr).require("rejected")
    assert outcome.num_found == whole, outcome.explain(f"expected all {whole} records of S")


def test_nothing_matches_nothing(live: Live) -> None:
    """*:* AND -*:* (the compiled form of a condition that can never match) returns no records.

    Depends on it: solr.compile(NOTHING), sent by to_solr() and inside raw queries.
    """
    assert solr.compile(NOTHING) == "*:* AND -*:*"
    outcome = live.count(Document.raw(solr.compile(NOTHING))).require("rejected")
    assert outcome.num_found == 0, outcome.explain("expected no records")


def test_integer_value_list(live: Live) -> None:
    """year:(2014 OR 2015) matches either year: within S it equals numFound(S AND year:[* TO 2015]).

    Depends on it: solr._compile_in for unquoted (integer) values.
    """
    listed = live.count(compiled(closed_set() & Document.year.in_([2014, 2015]), f"{S} AND year:(2014 OR 2015)"))
    ranged = live.count(closed_set() & (Document.year <= 2015))
    assert listed.require("rejected").num_found == ranged.require("rejected").num_found, (
        listed.explain(f"expected the same count as {ranged.q} ({ranged.num_found})")
    )


def test_inline_bibcode_list_at_the_limit(live: Live, sample: Outcome) -> None:
    """A list of config.max_inline_bibcodes (50) bibcodes is sent inline in a GET and finds all 50.

    Depends on it: config.max_inline_bibcodes, above which the planner switches to /search/bigquery.
    """
    bibcodes = sample.bibcodes[:50]
    expr = Document.bibcode.in_(bibcodes)
    assert plan(Document.select().where(expr)).path == QUERY_PATH
    outcome = live.count(expr).require("an inline list of 50 bibcodes was rejected")
    assert outcome.num_found == len(bibcodes), outcome.explain(f"expected {len(bibcodes)} records")


def test_long_affiliation_id_list(live: Live, findings: Findings) -> None:
    """An aff_id:(...) list of search.MAX_RELATED_VALUES (1000) ids fits in one GET request.

    Built as Document.affiliation.id.in_(<1000 Affiliation ids>), sent with
    rows=0. If it is rejected, a list of 100 is tried, to tell a length
    limit from a rejection of the field. Depends on it: search.MAX_RELATED_VALUES.
    """
    ids = sorted(affiliation.id for affiliation in Affiliation.select() if affiliation.id)
    tried: list[tuple[int, Outcome]] = []
    for size in AFF_ID_SIZES:
        outcome = live.count(Document.affiliation.id.in_(ids[:size]))
        tried.append((size, outcome))
        if outcome.ok:
            break
    lines = [
        f"{size:5} ids, query string of {len(urlencode(outcome.request.params or {})) if outcome.request else 0:6} "
        f"characters: {'accepted' if outcome.ok else outcome.error}"
        for size, outcome in tried
    ]
    findings.add("long aff_id lists", "\n".join(lines))
    size, outcome = tried[0]
    assert outcome.ok, (
        f"a list of {size} aff_id values was rejected, so search.MAX_RELATED_VALUES is too high:\n  "
        + "\n  ".join(lines)
    )


def test_single_token_wildcard(live: Live) -> None:
    """title:exoplan* (title.like("exoplan*")) returns records with a title word starting with exoplan.

    Depends on it: solr._compile_pattern for a single-token pattern.
    """
    outcome = live.search(compiled(Document.title.like("exoplan*"), "title:exoplan*"),
                          fields=("title", "alternate_title"), rows=3).require("rejected")
    bad = [doc.get("bibcode") for doc in outcome.docs if not any(re.search(r"\bexoplan", t, re.I) for t in titles(doc))]
    assert outcome.num_found and not bad, outcome.explain(f"no title word starting with exoplan in {bad}")


def test_escaped_specials_in_wildcard(live: Live, findings: Findings) -> None:
    """A single-token pattern with a Lucene special character is sent backslash-escaped (title:star\\-form*) and accepted.

    Depends on it: solr._LUCENE_SPECIALS escaping in solr._compile_pattern.
    """
    outcome = live.count(compiled(Document.title.like("star-form*"), r"title:star\-form*"))
    findings.add("escaped wildcard", f"{outcome.q}: " + (outcome.error or f"numFound {outcome.num_found}"))
    outcome.require("the escaped pattern was rejected")


def test_wildcard_inside_phrase(probes: ProbeResults) -> None:
    """A wildcard inside a quoted phrase, author:"Last, I*", matches that author.

    Depends on it: solr._compile_pattern quoting a pattern that contains whitespace.
    """
    probes.check("wildcard_phrase")


def test_leading_wildcard(live: Live, findings: Findings) -> None:
    """A leading wildcard from .contains(), title:*astro*, is answered (not rejected) with matching records.

    Depends on it: TextExpr.contains() being usable on Document fields.
    """
    outcome = live.search(compiled(Document.title.contains("astro"), "title:*astro*"),
                          fields=("title", "alternate_title"), rows=3)
    findings.add("leading wildcard", f"{outcome.q}: " + (outcome.error or f"answered, numFound {outcome.num_found}"))
    outcome.require("ADS rejects a leading wildcard, so TextExpr.contains() cannot be used on Document")
    bad = [doc.get("bibcode") for doc in outcome.docs if not any("astro" in t.lower() for t in titles(doc))]
    assert outcome.num_found and not bad, outcome.explain(f"no title containing 'astro' in {bad}")
