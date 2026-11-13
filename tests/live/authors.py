"""The per-author arrays (author, aff, aff_id, email, orcid_pub, orcid_user, orcid_other) and pos() on them.

``Document.authors`` (an :class:`scixplorer.model.Collection`) assumes that these
arrays are index-aligned with ``author``, that ``"-"`` means "no value", and
that ADS searches the fields in ``search.POSITIONAL_FIELDS`` by position with
``pos()``. The functions here derive, from the pinned records' default fields
(the ``full_records`` fixture, so no extra request), the checks of those
assumptions; ``tests/live/test_authors.py`` sends them, and
``tests/test_live_authors.py`` exercises them offline against a fake index.

Each check is an *item*: one or more probes on pinned records (see
:mod:`live.probes`). All items are sent together in one request; only if that
fails is each item sent alone, so every item gets its own verdict. At most
``1 + len(items)`` requests (:data:`BUDGET`), one when everything holds.
Group testing (:func:`live.support.isolate`) is not used here: failures are
likely to come several at a time (a whole class of fields ignoring
positions), where it needs more requests than checking each item.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from scixplorer.expr import Expr
from scixplorer.models.document import Document

from .harness import PINNED, Live, Outcome
from .probes import Probe, group_query
from .support import Verdict

#: The per-author arrays besides author itself.
ARRAYS = ("aff", "aff_id", "email", "orcid_pub", "orcid_user", "orcid_other")

#: The ORCID arrays that the virtual orcid field searches together.
ORCIDS = ("orcid_pub", "orcid_user", "orcid_other")

#: The fields pos() is tried on: the arrays, and the virtual orcid field.
POSITION_FIELDS = ("aff_id", "aff", "email", "orcid", "orcid_pub", "orcid_user", "orcid_other")

#: The array entry meaning "no value" (scixplorer.models.author.document_authors).
PLACEHOLDER = "-"

#: A value no record has, for checking only that a clause is accepted.
NO_SUCH_VALUE = "zzzzzz"

#: An affiliation id no record has.
NO_SUCH_ID = "Z99999"

#: The most requests these checks send: one for all items, then one per item.
BUDGET = 1 + len(POSITION_FIELDS) + 2


def records_of(outcome: Outcome) -> list[dict[str, Any]]:
    """The returned records in :data:`PINNED` order (raw JSON)."""
    by_bibcode = {str(doc.get("bibcode")): doc for doc in outcome.docs}
    return [by_bibcode[bibcode] for bibcode in PINNED if bibcode in by_bibcode]


def real(value: Any) -> str | None:
    """``value`` stripped, or ``None`` for the placeholder, a blank or a non-string."""
    if not isinstance(value, str) or not value.strip() or value.strip() == PLACEHOLDER:
        return None
    return value.strip()


def entry(record: dict[str, Any], field: str, i: int) -> str | None:
    """Author ``i``'s value of ``field`` (for ``orcid``, the first of the ORCID arrays that has one)."""
    if field == "orcid":
        return next((value for name in ORCIDS if (value := entry(record, name, i)) is not None), None)
    array = record.get(field)
    return real(array[i]) if isinstance(array, list) and i < len(array) else None


def alignment_problems(records: Sequence[dict[str, Any]]) -> list[str]:
    """Arrays whose number of entries differs from the number of authors (or that are not lists)."""
    problems = []
    for record in records:
        authors = record.get("author")
        if not isinstance(authors, list):
            continue
        for name in ARRAYS:
            array = record.get(name)
            if array is None:
                continue
            if not isinstance(array, list):
                problems.append(f"{record.get('bibcode')} {name}: not a list ({array!r:.40})")
            elif len(array) != len(authors):
                problems.append(f"{record.get('bibcode')} {name}: {len(array)} entries for {len(authors)} authors")
    return problems


def presence(records: Sequence[dict[str, Any]]) -> str:
    """Per record: the number of authors and of entries of each array (``-`` if ADS left it out)."""
    rows = [f"{'record':20} {'author':>6} " + " ".join(f"{name:>11}" for name in ARRAYS)]
    for record in records:
        authors = record.get("author")
        counts = [len(a) if isinstance(a := record.get(name), list) else "-" for name in ARRAYS]
        rows.append(
            f"{record.get('bibcode')!s:20} {len(authors) if isinstance(authors, list) else '-':>6} "
            + " ".join(f"{count:>11}" for count in counts)
        )
    return "\n".join(rows)


def placeholder_problems(records: Sequence[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    """Entries that are not a non-blank string (null, "" or another type), and the number of "-" per array."""
    problems: list[str] = []
    dashes = dict.fromkeys(ARRAYS, 0)
    for record in records:
        for name in ARRAYS:
            array = record.get(name)
            if not isinstance(array, list):
                continue
            for i, value in enumerate(array):
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"{record.get('bibcode')} {name}[{i}] is {value!r} (expected a value or '-')")
                elif value.strip() == PLACEHOLDER:
                    dashes[name] += 1
    return problems, dashes


def delimiters(records: Sequence[dict[str, Any]]) -> str:
    """How several affiliations of one author are written in aff and aff_id: entries containing ';', with an example."""
    lines = []
    for name in ("aff", "aff_id"):
        several = [v for record in records for v in record.get(name) or [] if isinstance(v, str) and ";" in v]
        example = f"; e.g. {several[0]!r:.80}" if several else ""
        lines.append(f"{name}: {len(several)} entries contain ';'{example}")
    return "\n".join(lines)


@dataclass(frozen=True)
class PositionCase:
    """Author ``position`` (0-based) of record ``anchor`` has ``value`` in ``field``; author ``other`` does not."""

    field: str
    anchor: str
    value: str
    position: int
    other: int


def position_case(records: Sequence[dict[str, Any]], field: str) -> PositionCase | None:
    """A pinned author with a value of ``field`` (preferring one after the first) and another without it.

    For ``aff_id`` the value is the entry's first id.
    """
    for record in records:
        authors = record.get("author") or []
        with_value = [i for i in range(len(authors)) if entry(record, field, i) is not None]
        for i in [i for i in with_value if i > 0] + [i for i in with_value if i == 0]:
            value = entry(record, field, i)
            assert value is not None
            if field == "aff_id":
                value = value.split(";")[0].strip()
            others = [
                k for k in range(len(authors))
                if k != i and value.casefold() not in (entry(record, field, k) or "").casefold()
            ]
            if others:
                return PositionCase(field, str(record.get("bibcode")), value, i, others[0])
    return None


def several_ids(records: Sequence[dict[str, Any]]) -> tuple[str, str, list[str]] | None:
    """A pinned record, one of its aff_id entries holding several ids, and those ids; ``None`` if there is none."""
    for record in records:
        for value in record.get("aff_id") or []:
            ids = [part.strip() for part in value.split(";") if real(part)] if isinstance(value, str) else []
            if len(ids) > 1:
                return str(record.get("bibcode")), value, ids
    return None


def at(field: str, value: str, position: int) -> Expr:
    """``pos(<field>:"<value>", position + 1)``: the clause for author ``position``'s value."""
    return Document.pos(getattr(Document, field) == value, position + 1)


def set_at(affiliation_id: str, position: int) -> Expr:
    """``pos(aff_id:("<id>" OR "Z99999"), position + 1)``: a set of ids inside pos()."""
    return Document.pos(Document.aff_id.in_([affiliation_id, NO_SUCH_ID]), position + 1)


@dataclass(frozen=True)
class Item:
    """One check: every probe must hold. ``tested`` is false for a field checked for acceptance only."""

    name: str
    probes: tuple[Probe, ...]
    tested: bool = True


def items(records: Sequence[dict[str, Any]]) -> list[Item]:
    """The checks: pos() on each of :data:`POSITION_FIELDS`, a set inside pos(), and each id of an entry with several."""
    found: list[Item] = []
    anchor = str(records[0].get("bibcode")) if records else PINNED[0]
    for field in POSITION_FIELDS:
        case = position_case(records, field)
        if case is None:
            clause = at(field, NO_SUCH_VALUE, 0) | (Document.bibcode == anchor)
            probe = Probe(field, anchor, clause, f"pos({field}:\"…\", 1) is accepted.")
            found.append(Item(field, (probe,), tested=False))
            continue
        found.append(Item(field, (
            Probe(f"{field}: at its position", case.anchor, at(field, case.value, case.position),
                  f"pos({field}:\"…\", n) matches the record whose author n has that value."),
            Probe(f"{field}: not at another", case.anchor, at(field, case.value, case.other),
                  f"pos({field}:\"…\", m) does not match when author m does not have that value.", negate=True),
        )))
    case = position_case(records, "aff_id")
    if case is not None:
        found.append(Item("aff_id_set", (Probe(
            "aff_id_set", case.anchor, set_at(case.value, case.position),
            "pos(aff_id:(\"a\" OR \"b\"), n), a set inside pos(), matches when one of them is author n's id.",
        ),)))
    several = several_ids(records)
    if several is not None:
        record, value, ids = several
        found.append(Item("aff_id_several", tuple(
            Probe(f"aff_id_several: {one}", record, Document.aff_id == one,
                  f"aff_id:\"<id>\" matches a record whose aff_id entry is {value!r}.")
            for one in ids[:2]
        )))
    return found


def check(live: Live, checks: Sequence[Item]) -> dict[str, Verdict[Outcome]]:
    """The verdict of each item: one request for all of them, then one per item if that fails."""

    def run(group: Sequence[Item]) -> tuple[bool, Outcome]:
        expr, expected = group_query([probe for item in group for probe in item.probes])
        outcome = live.count(expr)
        return outcome.ok and outcome.num_found == expected, outcome

    if not checks:
        return {}
    passed, outcome = run(checks)
    if passed:
        return {item.name: Verdict("pass", outcome, alone=len(checks) == 1) for item in checks}
    verdicts = {}
    for item in checks:
        passed, outcome = run([item])
        verdicts[item.name] = Verdict("pass" if passed else "fail", outcome, alone=True)
    return verdicts


def position_verdicts(checks: Sequence[Item], verdicts: dict[str, Verdict[Outcome]]) -> dict[str, tuple[str, str]]:
    """For each of :data:`POSITION_FIELDS`: ``(status, detail)``, status one of works, fails, untested."""
    by_name = {item.name: item for item in checks}
    found: dict[str, tuple[str, str]] = {}
    for field in POSITION_FIELDS:
        item, verdict = by_name[field], verdicts[field]
        evidence = verdict.evidence
        if evidence is not None and not evidence.ok:
            found[field] = ("fails", f"pos() rejected: {evidence.error}")
        elif verdict.status == "fail":
            found[field] = ("fails", "pos() accepted, but it does not test the position "
                            "(it must match at the author's position and not at another)")
        elif not item.tested:
            found[field] = ("untested", "pos() accepted; no pinned author has a value to test the position with")
        else:
            found[field] = ("works", "pos() accepted and tests the position")
    return found


def explain(item: Item, verdict: Verdict[Outcome]) -> str:
    """A failed item: its assumptions, its probes and the request that showed it."""
    lines = [probe.assumption for probe in item.probes]
    lines += [f"  probe:    {probe.text()}" for probe in item.probes]
    if verdict.evidence is not None:
        lines.append(verdict.evidence.describe())
    return "\n".join(lines)
