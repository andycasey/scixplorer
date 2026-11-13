"""Exact matching with the = prefix (SPEC section 5) and positional search with pos() (items 7 and 8)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from ads.expr import Match, Or
from ads.models.document import Document
from ads.solr import EXPANDED_MATCH_KINDS

from . import schema
from .harness import Live, Outcome, compiled, summarise
from .probes import ProbeResults
from .support import Findings, Verdict, peel

pytestmark = pytest.mark.live

#: Most requests spent on the =field table: one if every field accepts =.
EXACT_CAP = 7

#: Of those, at most this many go to the fields that have a _nosyn copy in the schema.
EXACT_CAP_WITH_COPY = 2


def exact_fields() -> list[str]:
    """The Document fields the compiler sends with = for an exact match (match_kind "text" or "name")."""
    return [info.name for info in Document.describe() if info.match_kind in EXPANDED_MATCH_KINDS]


def nosyn_copies(request: pytest.FixtureRequest) -> set[str] | None:
    """Solr fields that have a ``<name>_nosyn`` copy in the published schema, or ``None`` if it is unavailable."""
    try:
        path = request.getfixturevalue("schema_path")
        fields, _ = schema.report_module().read_schema(str(path))
    except Exception:
        return None
    return {name[: -len("_nosyn")] for name in fields if name.endswith("_nosyn")}


@pytest.fixture(scope="module")
def exact_table(live: Live, request: pytest.FixtureRequest) -> tuple[dict[str, Verdict[Outcome]], set[str] | None]:
    """Whether ADS accepts =field:"a" for each field, and the fields with a _nosyn copy."""
    calls = 0

    def check(group: Sequence[str]) -> tuple[bool, Outcome]:
        nonlocal calls
        calls += 1
        clauses = tuple(Match(Document._fields[name], "a", exact=True) for name in group)
        outcome = live.count(clauses[0] if len(clauses) == 1 else Or(clauses))
        return outcome.ok, outcome

    def message(outcome: Outcome) -> str | None:
        return outcome.error

    names = exact_fields()
    copies = nosyn_copies(request)
    if copies is None:
        return peel(names, check, EXACT_CAP, message=message), None
    with_copy = [name for name in names if Document._fields[name].solr_name in copies]
    verdicts = peel(with_copy, check, EXACT_CAP_WITH_COPY, message=message)
    others = [name for name in names if name not in with_copy]
    verdicts.update(peel(others, check, EXACT_CAP - calls, message=message))
    return verdicts, copies


def test_exact_prefix_on_every_text_and_name_field(findings: Findings, exact_table: Any) -> None:
    """=field:"a" is accepted on every field the client sends = for (TextField, VirtualField, NameField).

    One request ORs =field:"a" for all of them (the fields with a _nosyn copy
    in schema.xml, expected to work, in a group of their own). If ADS rejects
    a group and its error names one field, that field is marked rejected and
    the rest are sent again; otherwise the group is split in halves. At most
    EXACT_CAP (7) requests; fields not settled within them are
    "undetermined". Depends on it: Field.match_kind of each field
    (solr.EXPANDED_MATCH_KINDS); a field that rejects = needs a match_kind
    that does not send it.
    """
    verdicts, copies = exact_table
    rows = [f"{'field':20} {'_nosyn copy':12} verdict"]
    for name in exact_fields():
        copy = "?" if copies is None else "yes" if Document._fields[name].solr_name in copies else "no"
        rows.append(f"{name:20} {copy:12} {summarise(verdicts[name])}")
    table = "\n".join(rows)
    findings.add("=field:\"a\" accepted?", table)
    rejected = [name for name, verdict in verdicts.items() if verdict.status != "pass"]
    assert not rejected, f"= is rejected (or undetermined) on {len(rejected)} field(s):\n{table}"


def test_exact_author_matches_its_record(probes: ProbeResults) -> None:
    """=author:"Last, First M." (no name variants) still matches the record listing that exact name.

    Depends on it: NameField.match_kind = "name" sending =author: for .exact().
    """
    probes.check("exact_author")


@pytest.fixture(scope="module")
def author_counts(live: Live, lead: dict[str, Any]) -> tuple[Outcome, Outcome]:
    """numFound for =author:"<lead's first author>" and for author:"<the same>"."""
    name = str(lead["author"][0])
    exact = live.count(compiled(Document.author.exact(name), f'=author:"{name}"'))
    expanded = live.count(compiled(Document.author == name, f'author:"{name}"'))
    return exact, expanded


def test_exact_author_is_no_broader(author_counts: tuple[Outcome, Outcome]) -> None:
    """=author:"…" matches no more records than author:"…" (it only turns name-variant expansion off).

    Depends on it: the meaning of Document.author.exact() documented in fields.NameField.
    """
    exact, expanded = (outcome.require("rejected") for outcome in author_counts)
    assert exact.num_found is not None and expanded.num_found is not None
    assert exact.num_found <= expanded.num_found, (
        f"=author found more than author:\n{exact.describe()}\n{expanded.describe()}"
    )


def test_exact_author_with_full_first_name_is_narrower(author_counts: tuple[Outcome, Outcome]) -> None:
    """For a prolific author with a spelled-out first name, =author:"…" finds strictly fewer records than author:"…".

    Depends on it: the documented ADS behaviour that =author: excludes the
    initial-only variants that author: includes (SPEC section 5 table).
    """
    exact, expanded = (outcome.require("rejected") for outcome in author_counts)
    assert exact.num_found is not None and expanded.num_found is not None
    assert exact.num_found < expanded.num_found, (
        f"=author did not narrow the search:\n{exact.describe()}\n{expanded.describe()}"
    )


def test_exact_title_matches_its_record(probes: ProbeResults) -> None:
    """=title:"<the whole title>" (no synonyms) matches the record with that title.

    Depends on it: TextField.match_kind = "text" sending =title: for .exact().
    """
    probes.check("exact_title")


def test_exact_prefix_on_bibcode_matches_nothing(live: Live, findings: Findings, lead: dict[str, Any]) -> None:
    """=bibcode:"X" does not match record X (it finds nothing or is rejected), while bibcode:"X" finds exactly X.

    This is why keyword fields never get = (solr.EXPANDED_MATCH_KINDS excludes
    "keyword", so Document.bibcode.exact(X) is sent as bibcode:"X").
    """
    bibcode = str(lead["bibcode"])
    plain = live.count(compiled(Document.bibcode.exact(bibcode), f'bibcode:"{bibcode}"')).require("rejected")
    exact = live.count(Document.raw(f'=bibcode:"{bibcode}"'))
    findings.add("=bibcode", f"{exact.q}: " + (exact.error or f"numFound {exact.num_found}"))
    assert plain.num_found == 1, plain.explain("expected exactly one record")
    assert not exact.ok or exact.num_found == 0, exact.explain(
        "=bibcode matched the record, so the claim in SPEC section 5 is wrong "
        "(harmless: the client never sends = on keyword fields)"
    )


def test_exact_inside_position(probes: ProbeResults) -> None:
    """= inside pos(), pos(=author:"…", 2), is accepted and matches the second author.

    Depends on it: solr._compile_func compiling an exact Match as pos()'s first argument.
    """
    probes.check("exact_position")


def test_position_one_is_the_first_author(probes: ProbeResults) -> None:
    """pos(author:"<first author>", 1) (Document.author[0] == …) matches the record.

    Depends on it: TextExpr.__getitem__ positions being 1-indexed in pos().
    """
    probes.check("position_first")


def test_position_tests_the_position(probes: ProbeResults) -> None:
    """pos(author:"<second author>", 1) does not match: pos() tests the position, not mere presence.

    Depends on it: the meaning of Document.author[i].
    """
    probes.check("position_first_not_second")


def test_position_two_argument_form(probes: ProbeResults) -> None:
    """The two-argument pos(author:"…", 2) (Document.author[1] == …) matches the second author.

    Depends on it: solr._compile_func dropping the end when start == end.
    """
    probes.check("position_second")


def test_position_three_argument_form(probes: ProbeResults) -> None:
    """The three-argument pos(author:"…", 1, 3) (Document.author[0:3] == …) matches the third author.

    Depends on it: TextExpr.__getitem__ slices compiling to an inclusive 1-indexed range.
    """
    probes.check("position_range")


def test_first_author_field(probes: ProbeResults) -> None:
    """first_author:"…" matches the record's first author.

    Depends on it: Document.first_author as a searchable NameField.
    """
    probes.check("first_author")
