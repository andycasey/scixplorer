"""Probes: assumptions phrased as "pinned record R matches (or does not match) clause C".

Each probe is checked as ``bibcode:"R" AND C`` (``AND -C`` for a negative
probe), which must find exactly one document. Probes are conjunctions, so a
group of them can be checked with one request; :func:`~live.support.isolate`
splits a failing group to find the probes that fail, within a request cap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from scixplorer import search
from scixplorer.expr import And, Expr, Not, Or, escape_pattern
from scixplorer.models.document import Document

from .harness import Live, Outcome, alias_value, choose_lead, leading_words, name_parts
from .support import Verdict, isolate

#: The search aliases and query-parser fields checked against a pinned record.
ALIASES = ("arxiv", "collection", "affil", "inst", "orcid", "full", "abs")

#: Most requests spent isolating failing probes (one request if all pass).
PROBE_CAP = 10

_ACCEPTANCE_ONLY = (
    "no pinned record has a value for this field, so only acceptance is checked "
    "(field:\"zzzzzz\" OR bibcode:\"<anchor>\")"
)


@dataclass(frozen=True)
class Probe:
    """Record ``anchor`` must match ``clause`` (must not, if ``negate``)."""

    name: str
    anchor: str
    clause: Expr
    assumption: str
    negate: bool = False
    note: str = ""

    def condition(self) -> Expr:
        """The clause as it is AND-ed with the anchor."""
        return Not(self.clause) if self.negate else self.clause

    def text(self) -> str:
        """The probe in words, with the Solr clause."""
        verb = "must not match" if self.negate else "must match"
        return f"record {self.anchor} {verb} {search.to_solr(self.clause)}"


def group_query(probes: Sequence[Probe]) -> tuple[Expr, int]:
    """One query checking all ``probes``, and the number of documents it must find (one per anchor)."""
    anchors: dict[str, list[Expr]] = {}
    for probe in probes:
        anchors.setdefault(probe.anchor, []).append(probe.condition())
    parts = [And((Document.bibcode == anchor, *conditions)) for anchor, conditions in anchors.items()]
    return (parts[0] if len(parts) == 1 else Or(tuple(parts))), len(parts)


def build(records: dict[str, dict[str, Any]]) -> list[Probe]:
    """The probes, derived from the pinned records (empty if no record suits them)."""
    lead = choose_lead(records)
    if lead is None:
        return []
    anchor = str(lead["bibcode"])
    first, second, third = (str(name) for name in lead["author"][:3])
    title = str(lead["title"][0])
    phrase = leading_words(title)
    last, given = name_parts(first)
    probes = [
        Probe("phrase", anchor, Document.title == phrase,
              "A fielded phrase, title:\"w1 w2 ...\", matches a record whose title starts with those words."),
        Probe("integer_equality", anchor, Document.year == int(str(lead["year"])),
              "An integer equality, year:YYYY, matches a record of that year (year is a string in Solr)."),
        Probe("wildcard_phrase", anchor, Document.author.like(f"{escape_pattern(last)}, {given[0]}*"),
              "A wildcard inside a quoted phrase, author:\"Last, I*\", matches that author."),
        Probe("exact_author", anchor, Document.author.exact(first),
              "=author:\"Last, First M.\" (no name variants) still matches the record listing that exact name."),
        Probe("exact_title", anchor, Document.title.exact(title),
              "=title:\"<the whole title>\" (no synonyms) matches the record with that title."),
        Probe("exact_position", anchor, Document.author[1].exact(second),
              "= inside pos(), pos(=author:\"…\", 2), is accepted and matches the second author."),
        Probe("position_first", anchor, Document.author[0] == first,
              "pos(author:\"…\", 1) matches the record's first author."),
        Probe("position_first_not_second", anchor, Document.author[0] == second,
              "pos(author:\"<second author>\", 1) does not match: pos() tests the position, not just presence.",
              negate=True),
        Probe("position_second", anchor, Document.author[1] == second,
              "Two-argument pos(author:\"…\", 2) matches the second author."),
        Probe("position_range", anchor, Document.author[0:3] == third,
              "Three-argument pos(author:\"…\", 1, 3) matches an author in positions 1 to 3."),
        Probe("first_author", anchor, Document.first_author == first,
              "first_author:\"…\" matches the record's first author."),
    ]
    for alias in ALIASES:
        probes.append(_alias_probe(alias, anchor, records))
    return probes


def _alias_probe(alias: str, lead: str, records: dict[str, dict[str, Any]]) -> Probe:
    handle = getattr(Document, alias)
    assumption = f"The search field {alias}: is accepted and matches the record whose fields it covers."
    for bibcode, record in [(lead, records[lead]), *records.items()]:
        value = alias_value(alias, record)
        if value is not None:
            return Probe(f"alias_{alias}", bibcode, handle == value, assumption)
    return Probe(
        f"alias_{alias}", lead, (handle == "zzzzzz") | (Document.bibcode == lead),
        f"The search field {alias}: is accepted.", note=_ACCEPTANCE_ONLY,
    )


class ProbeResults:
    """The verdict of every probe, found with at most :data:`PROBE_CAP` requests."""

    def __init__(self, live: Live, probes: Sequence[Probe]) -> None:
        self.probes = {probe.name: probe for probe in probes}
        self.verdicts: dict[str, Verdict[Outcome]] = isolate(
            list(self.probes), lambda names: self._check(live, names), PROBE_CAP
        )

    def _check(self, live: Live, names: Sequence[str]) -> tuple[bool, Outcome]:
        expr, expected = group_query([self.probes[name] for name in names])
        outcome = live.count(expr)
        return outcome.ok and outcome.num_found == expected, outcome

    def check(self, name: str) -> None:
        """Pass if the probe passed; fail with its evidence; skip if it was not isolated."""
        if name not in self.probes:
            pytest.skip("no pinned record has the fields this probe needs (see test_records.py)")
        probe = self.probes[name]
        verdict = self.verdicts[name]
        if verdict.status == "pass":
            return
        lines = [probe.assumption, f"  probe:    {probe.text()}"]
        if probe.note:
            lines.append(f"  note:     {probe.note}")
        if verdict.status == "undetermined":
            pytest.skip(
                f"not isolated: a group holding this probe failed and the cap of {PROBE_CAP} requests "
                f"was reached; the probes that were isolated report their own failures. " + " ".join(lines)
            )
        evidence = verdict.evidence
        if evidence is not None:
            lines.append(evidence.describe())
            if not verdict.alone:
                lines.append("  (found failing by elimination: this is the request for its group)")
            elif evidence.ok:
                lines.append("  (expected numFound 1)")
        raise AssertionError("\n".join(lines))
