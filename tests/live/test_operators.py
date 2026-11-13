"""Second-order operators with a fielded sub-query or a raw string argument (item 9)."""

from __future__ import annotations

from typing import Any

import pytest

from scixplorer.models.document import Document

from .harness import Live, closed_set, compiled
from .support import Findings

pytestmark = pytest.mark.live

#: One-argument operators checked for acceptance only (their results depend on reads and wording).
ACCEPTANCE_ONLY = ("trending", "useful", "reviews", "similar")


def test_citations_of_a_record(live: Live, lead: dict[str, Any]) -> None:
    """citations(bibcode:"X") returns records whose reference lists contain X.

    Depends on it: Document.citations() wrapping a compiled sub-query in citations(...).
    """
    bibcode = str(lead["bibcode"])
    expr = compiled(Document.citations(Document.bibcode == bibcode), f'citations(bibcode:"{bibcode}")')
    outcome = live.search(expr, fields=("reference",), rows=2).require("rejected")
    bad = [doc.get("bibcode") for doc in outcome.docs if bibcode not in (doc.get("reference") or [])]
    assert outcome.num_found and outcome.docs and not bad, outcome.explain(f"these do not cite {bibcode}: {bad}")


def test_references_of_a_record(live: Live, lead: dict[str, Any]) -> None:
    """references(bibcode:"X") returns records that are in X's reference list.

    Depends on it: Document.references() wrapping a compiled sub-query in references(...).
    """
    bibcode = str(lead["bibcode"])
    cited = set(lead.get("reference") or [])
    expr = compiled(Document.references(Document.bibcode == bibcode), f'references(bibcode:"{bibcode}")')
    outcome = live.search(expr, rows=3).require("rejected")
    bad = [b for b in outcome.bibcodes if b not in cited]
    assert outcome.num_found and not bad and outcome.num_found <= len(cited), outcome.explain(
        f"expected records from the {len(cited)} references of {bibcode}; not among them: {bad}"
    )


@pytest.mark.parametrize("operator", ACCEPTANCE_ONLY)
def test_operator_accepts_fielded_query(live: Live, findings: Findings, lead: dict[str, Any], operator: str) -> None:
    """trending/useful/reviews/similar(bibcode:"X") is accepted with a fielded sub-query.

    Depends on it: Document.trending(), useful(), reviews() and similar(). The
    number found is recorded in the findings, not asserted.
    """
    bibcode = str(lead["bibcode"])
    expr = compiled(getattr(Document, operator)(Document.bibcode == bibcode), f'{operator}(bibcode:"{bibcode}")')
    outcome = live.count(expr)
    findings.add(f"{operator}()", f"{outcome.q}: " + (outcome.error or f"numFound {outcome.num_found}"))
    outcome.require(f"{operator}() rejected a fielded sub-query")


def test_operator_accepts_raw_string(live: Live) -> None:
    """An operator given a raw string, trending(exoplanets) (Document.trending("exoplanets")), is accepted.

    Depends on it: the str argument of the Document operators being sent verbatim (Raw).
    """
    live.count(compiled(Document.trending("exoplanets"), "trending(exoplanets)")).require("rejected")


def test_topn_takes_the_top_of_a_sorted_query(live: Live) -> None:
    """topn(5, <query with AND>, citation_count desc) returns the five most cited records of that query.

    Compared with the same query sorted by citation_count desc, rows=5 (the
    citation counts must be the same multiset). Depends on it: Document.top_n
    and solr._func_arg leaving an AND sub-query unparenthesised inside topn().
    """
    expr = compiled(
        Document.top_n(5, closed_set(), Document.citation_count.desc()),
        'topn(5, bibstem:"AJ" AND year:[2014 TO 2017], citation_count desc)',
    )
    top = live.search(expr, fields=("citation_count",), rows=5).require("rejected")
    best = live.search(closed_set(), fields=("citation_count",), order=(Document.citation_count.desc(),), rows=5)
    best.require("rejected")
    counts = sorted((doc.get("citation_count") for doc in top.docs), reverse=True)
    expected = [doc.get("citation_count") for doc in best.docs]
    assert top.num_found == 5 and counts == expected, (
        f"topn() citation counts {counts} differ from the top five {expected}\n{top.describe()}\n{best.describe()}"
    )
