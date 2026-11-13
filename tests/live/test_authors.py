"""The per-author arrays behind Document.authors, and pos() on them (search.POSITIONAL_FIELDS).

The arrays come from the shared ``full_records`` fixture (the pinned records
with the default fields). The checks cost one request if everything holds,
and at most ``authors.BUDGET`` (10) otherwise.
"""

from __future__ import annotations

from typing import Any

import pytest

from scixplorer import search
from scixplorer.models.document import Document

from . import authors
from .harness import Live, Outcome, compiled
from .support import Findings, Verdict

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def arrays(full_records: Outcome) -> list[dict[str, Any]]:
    """The pinned records' author lists and per-author arrays (raw JSON; no extra request)."""
    full_records.require("the default field list was rejected")
    return authors.records_of(full_records)


@pytest.fixture(scope="module")
def checks(arrays: list[dict[str, Any]]) -> dict[str, authors.Item]:
    """The checks derived from the arrays, by name."""
    return {item.name: item for item in authors.items(arrays)}


@pytest.fixture(scope="module")
def verdicts(live: Live, checks: dict[str, authors.Item]) -> dict[str, Verdict[Outcome]]:
    """The verdict of every check: one request if all hold, else one more per check."""
    return authors.check(live, list(checks.values()))


@pytest.fixture(scope="module")
def positions(
    findings: Findings, checks: dict[str, authors.Item], verdicts: dict[str, Verdict[Outcome]]
) -> dict[str, tuple[str, str]]:
    """Whether pos() works on each per-author field; recorded as a table in the findings."""
    found = authors.position_verdicts(list(checks.values()), verdicts)
    rows = [f"{'field':12} {'listed':7} {'status':9} detail"]
    for field, (status, detail) in found.items():
        listed = "yes" if field in search.POSITIONAL_FIELDS else "no"
        rows.append(f"{field:12} {listed:7} {status:9} {detail}")
    add = [f for f, (status, _) in found.items() if status == "works" and f not in search.POSITIONAL_FIELDS]
    remove = [f for f, (status, _) in found.items() if status == "fails" and f in search.POSITIONAL_FIELDS]
    rows.append(f"could be added to search.POSITIONAL_FIELDS: {', '.join(add) or 'none'}")
    rows.append(f"must be removed from search.POSITIONAL_FIELDS: {', '.join(remove) or 'none'}")
    findings.add("pos() on the per-author fields", "\n".join(rows))
    return found


def require(name: str, checks: dict[str, authors.Item], verdicts: dict[str, Verdict[Outcome]]) -> None:
    """Pass if the check held; fail with its assumptions, probes and the request that showed it."""
    verdict = verdicts[name]
    if verdict.status != "pass":
        raise AssertionError(authors.explain(checks[name], verdict))


def test_per_author_arrays_have_one_entry_per_author(findings: Findings, arrays: list[dict[str, Any]]) -> None:
    """Each per-author array (aff, aff_id, email, orcid_pub, orcid_user, orcid_other) has one entry per author.

    Checked on every pinned record (arrays ADS leaves out are skipped; the
    findings show the counts). Depends on it: Document.authors
    (scixplorer.model.Collection.records), which reads entry i of each array as
    author i's value, and Document.affiliation (one entry per author).
    """
    findings.add("per-author arrays of the pinned records (entries)", authors.presence(arrays))
    problems = authors.alignment_problems(arrays)
    assert not problems, "arrays not aligned with author:\n  " + "\n  ".join(problems)


def test_missing_per_author_values_are_a_dash(findings: Findings, arrays: list[dict[str, Any]]) -> None:
    """An author without a value has "-" in a per-author array: never null or an empty string.

    Depends on it: the placeholder "-" of Document.authors
    (scixplorer.models.author.document_authors), read as "no value". How several
    affiliations of one author are written in aff and aff_id is recorded in
    the findings.
    """
    problems, dashes = authors.placeholder_problems(arrays)
    findings.add(
        'per-author arrays: entries that are "-"',
        "\n".join(f"{name}: {count}" for name, count in dashes.items()) + "\n" + authors.delimiters(arrays),
    )
    assert not problems, "entries that are neither a value nor '-':\n  " + "\n  ".join(problems)
    if not any(dashes.values()):
        pytest.skip('no pinned author lacks one of these values, so the placeholder "-" was not seen')


@pytest.mark.parametrize("field", authors.POSITION_FIELDS)
def test_position_on_per_author_field(field: str, positions: dict[str, tuple[str, str]]) -> None:
    """pos(<field>:"v", n) is accepted, matches the record whose author n has v, and does not match at another position.

    Depends on it: search.POSITIONAL_FIELDS. A listed field must pass, or
    Document.authors[i].<field> conditions find the wrong documents. For an
    unlisted field the client never sends pos() (it checks the position
    client-side), so a failure is expected (xfail); a pass means the field
    could be added. The findings table lists every field.
    """
    status, detail = positions[field]
    if status == "works":
        return
    if status == "untested":
        pytest.skip(f"{field}: {detail}")
    if field in search.POSITIONAL_FIELDS:
        raise AssertionError(
            f"{field} is in search.POSITIONAL_FIELDS, but {detail}; remove it from search.POSITIONAL_FIELDS"
        )
    pytest.xfail(f"{field}: {detail} (not in search.POSITIONAL_FIELDS, so the client does not send pos() on it)")


def test_set_inside_position(
    arrays: list[dict[str, Any]], checks: dict[str, authors.Item], verdicts: dict[str, Verdict[Outcome]]
) -> None:
    """pos(aff_id:("a" OR "b"), n), a set of ids inside pos(), matches when one of them is author n's id.

    Depends on it: Document.authors[i].affiliation conditions, which the
    planner sends as pos() around the whole set of matching ids (search._position).
    """
    case = authors.position_case(arrays, "aff_id")
    if case is None:
        pytest.skip("no pinned author has an aff_id")
    if "aff_id" in search.POSITIONAL_FIELDS:
        expected = f'pos(aff_id:("{case.value}" OR "{authors.NO_SUCH_ID}"), {case.position + 1})'
        compiled(Document.authors[case.position].aff_id.in_([case.value, authors.NO_SUCH_ID]), expected)
    require("aff_id_set", checks, verdicts)


def test_aff_id_matches_each_id_of_an_entry_with_several(
    checks: dict[str, authors.Item], verdicts: dict[str, Verdict[Outcome]]
) -> None:
    """aff_id:"<id>" matches a record whose aff_id entry for one author is "<id>; <other>" (either id).

    Depends on it: Document.affiliation and Author.affiliation conditions,
    which send single ids, matching authors with several affiliations.
    """
    if "aff_id_several" not in checks:
        pytest.skip("no pinned author has an aff_id entry with several ids")
    require("aff_id_several", checks, verdicts)
