"""Search aliases and query-parser fields, fields outside the schema, and fields that are not indexed (item 13)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from scixplorer.expr import Match, Or
from scixplorer.models.document import Document

from .harness import Live, Outcome, summarise
from .probes import ALIASES, ProbeResults
from .support import Findings, Verdict, isolate

pytestmark = pytest.mark.live

#: VirtualFields the old client offered that the published schema lacks, with a value to search for.
UNSCHEMED = {"grant_agencies": "NASA", "grant_id": "NNX"}

#: Stored fields the schema marks as not indexed (scripts/schema_report.py lists them).
NOT_INDEXED = ("links_data", "page_range")


@pytest.mark.parametrize("alias", ALIASES)
def test_search_alias(probes: ProbeResults, alias: str) -> None:
    """The search field (arxiv, collection, affil, inst, orcid, full, abs) is accepted and matches a pinned record.

    The value searched for is taken from the record's own fields (identifier,
    database, aff, institution, orcid_*, title). Depends on it: the
    VirtualField of the same name in models/document.py.
    """
    probes.check(f"alias_{alias}")


@pytest.fixture(scope="module")
def unschemed(live: Live, findings: Findings) -> dict[str, Verdict[Outcome]]:
    """Whether ADS accepts each of :data:`UNSCHEMED` (one request if it accepts both)."""

    def check(group: Sequence[str]) -> tuple[bool, Outcome]:
        clauses = tuple(Match(Document._fields[name], UNSCHEMED[name]) for name in group)
        outcome = live.count(clauses[0] if len(clauses) == 1 else Or(clauses))
        return outcome.ok, outcome

    verdicts = isolate(list(UNSCHEMED), check, cap=3)
    findings.add(
        "fields missing from schema.xml",
        "\n".join(f"{name:15} {summarise(verdict)}" for name, verdict in verdicts.items()),
    )
    return verdicts


@pytest.mark.parametrize("name", sorted(UNSCHEMED))
def test_field_missing_from_schema_is_searchable(unschemed: dict[str, Verdict[Outcome]], name: str) -> None:
    """grant_agencies / grant_id (offered by the old client, absent from schema.xml) are accepted in a query.

    Depends on it: the VirtualFields Document.grant_agencies and
    Document.grant_id; if ADS rejects them they should be removed.
    """
    verdict = unschemed[name]
    if verdict.status == "undetermined":
        pytest.skip("not isolated within the request cap")
    assert verdict.status == "pass", (
        verdict.evidence.explain(f"ADS rejects {name}:") if verdict.evidence else f"ADS rejects {name}"
    )


def test_not_indexed_fields_are_returned(full_records: Outcome) -> None:
    """links_data and page_range (stored but not indexed) are returned by the default fl for some pinned record.

    Depends on it: these fields being stored Document fields that are fetched
    (their docstrings say "Returned, but not searchable").
    """
    full_records.require("the default field list was rejected")
    absent = [name for name in NOT_INDEXED if not any(doc.get(name) for doc in full_records.docs)]
    assert not absent, full_records.explain(f"never returned for the pinned records: {absent}")
