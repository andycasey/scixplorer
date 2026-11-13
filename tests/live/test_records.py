"""Pinned records, the default and deferred field lists, and the types of returned values.

These are the tests that catch a field whose declared type or
multivaluedness is wrong: values are checked both as the client loads them
and as raw JSON, because the client silently coerces some mismatches (a
scalar for a multivalued field becomes a one-element list).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

import pytest

from scixplorer.models.document import Document

from .harness import PINNED, Live, Outcome, compiled
from .support import Findings, Verdict, isolate

pytestmark = pytest.mark.live

#: Records whose deferred fields are fetched (full text and long bibcode lists, so only two).
DEFERRED_RECORDS = PINNED[:2]

#: Most requests spent finding which deferred fields cannot be selected (one if all can).
DEFERRED_CAP = 4

_PYTHON_TYPES: dict[str, type] = {"int": int, "float": float, "str": str, "bool": bool, "datetime": datetime, "date": date}

#: The JSON types the API may send for each Python type without the client coercing it.
_JSON_TYPES: dict[type, tuple[type, ...]] = {
    int: (int,), float: (int, float), str: (str,), bool: (bool,), datetime: (str,), date: (str,),
}

_BY_SOLR_NAME = {field.solr_name: field for field in Document._fields.values()}


def _is_instance(value: Any, expected: type) -> bool:
    if isinstance(value, bool) and expected is not bool:
        return False
    if expected is date and isinstance(value, datetime):
        return False
    return isinstance(value, expected)


def type_problems(documents: Iterable[Document]) -> list[str]:
    """Loaded values that are neither ``None`` nor of the type ``Document.describe()`` declares."""
    problems = []
    for document in documents:
        for info in Document.describe():
            value = document._data.get(info.name)
            if value is None:
                continue
            inner = info.type[5:-1] if info.type.startswith("list[") else info.type
            expected = _PYTHON_TYPES[inner]
            if info.multivalued and not isinstance(value, list):
                problems.append(f"{document._data.get('bibcode')} {info.name}: {value!r:.60} is not a list")
                continue
            for item in value if info.multivalued else [value]:
                if not _is_instance(item, expected):
                    problems.append(f"{document._data.get('bibcode')} {info.name}: {item!r:.60} is not {inner}")
    return problems


def shape_problems(rows: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Raw JSON checks: (list-ness that contradicts ``multivalued``, values the client must coerce)."""
    problems: list[str] = []
    coercions: dict[str, str] = {}
    for row in rows:
        for key, raw in row.items():
            field = _BY_SOLR_NAME.get(key)
            if field is None:
                coercions.setdefault(key, f"{key}: returned but not a Document field")
                continue
            if field.multivalued != isinstance(raw, list):
                shape = "a list" if isinstance(raw, list) else f"a single {type(raw).__name__}"
                problems.append(
                    f"{row.get('bibcode')} {key}: declared multivalued={field.multivalued}, but ADS sent {shape}"
                )
            allowed = _JSON_TYPES[field.python_type]
            for item in raw if isinstance(raw, list) else [raw]:
                if isinstance(item, bool) is not (field.python_type is bool) or not isinstance(item, allowed):
                    coercions.setdefault(
                        key, f"{key}: ADS sends JSON {type(item).__name__} ({item!r:.30}); declared {field.type_name}"
                    )
    return problems, sorted(coercions.values())


def test_bibcode_list_returns_exactly_the_listed_records(pinned_outcome: Outcome) -> None:
    """A bibcode list, bibcode:("A" OR "B" ...), returns exactly the listed records.

    Depends on it: Document.bibcode.in_() and Document.in_(bibcodes) up to
    config.max_inline_bibcodes, lazy loading (which fetches a page by bibcode),
    and every live test whose expectations come from the pinned records.
    """
    compiled(Document.bibcode.in_(PINNED[:2]), f'bibcode:("{PINNED[0]}" OR "{PINNED[1]}")')
    pinned_outcome.require("a bibcode list was rejected")
    missing = sorted(set(PINNED) - set(pinned_outcome.bibcodes))
    unexpected = sorted(set(pinned_outcome.bibcodes) - set(PINNED))
    assert not missing and not unexpected and pinned_outcome.num_found == len(PINNED), pinned_outcome.explain(
        f"expected exactly the {len(PINNED)} pinned records; missing {missing}, unexpected {unexpected}. "
        f"A missing pinned bibcode must be replaced in tests/live/harness.py:PINNED."
    )


def test_default_field_list_is_accepted(full_records: Outcome) -> None:
    """One request with the default fl (every stored field except the deferred ones) is accepted.

    Depends on it: Document.select() with no fields (search._field_list), the
    most common query.
    """
    full_records.require("the default field list was rejected")
    assert full_records.num_found == len(PINNED), full_records.explain(f"expected {len(PINNED)} records")


def test_default_fields_load_without_data_warnings(live: Live, full_records: Outcome) -> None:
    """Every value returned for the default fl converts to its declared type without a DataWarning.

    Depends on it: Field.to_python for every Document field; a warning means
    the field's class (IntegerField, DateField, ...) in models/document.py is wrong.
    """
    full_records.require("the default field list was rejected")
    _, warned = live.documents(full_records)
    assert not warned, "DataWarning while loading the default fields:\n  " + "\n  ".join(warned)


def test_default_fields_have_declared_types(live: Live, full_records: Outcome) -> None:
    """Every loaded value is None or an instance of the type Document.describe() declares (lists of it if multivalued).

    Depends on it: the typing contract of SPEC section 5 (doc.year is int | None, ...).
    """
    full_records.require("the default field list was rejected")
    documents, _ = live.documents(full_records)
    problems = type_problems(documents)
    assert not problems, "values of the wrong type:\n  " + "\n  ".join(problems)


def test_default_fields_are_lists_exactly_when_multivalued(findings: Findings, full_records: Outcome) -> None:
    """ADS sends a JSON list for a field exactly when the field is declared multivalued.

    Depends on it: the multivalued flag of each field in models/document.py.
    The client silently wraps a scalar in a list (and unwraps a one-element
    list), so only the raw JSON shows a wrong flag. Values the client coerces
    (a JSON string for an integer field, ...) are listed in the findings.
    """
    full_records.require("the default field list was rejected")
    problems, coercions = shape_problems(full_records.docs)
    if coercions:
        findings.add("default fields: values the client coerces", "\n".join(coercions))
    assert not problems, "raw JSON contradicts the multivalued flag:\n  " + "\n  ".join(problems)


def test_score_is_returned_by_search_query(live: Live, full_records: Outcome) -> None:
    """/search/query returns score (a number) when fl includes it, as the default fl does.

    Depends on it: Document.score being a stored field in the default fl.
    """
    full_records.require("the default field list was rejected")
    documents, _ = live.documents(full_records)
    scores = {doc._data.get("bibcode"): doc._data.get("score") for doc in documents}
    assert scores and all(isinstance(score, float) for score in scores.values()), f"scores: {scores}"


@pytest.fixture(scope="module")
def deferred(live: Live) -> dict[str, Verdict[Outcome]]:
    """Which deferred fields can be selected (one request if all of them can)."""

    def check(group: Sequence[str]) -> tuple[bool, Outcome]:
        outcome = live.search(Document.bibcode.in_(DEFERRED_RECORDS), fields=tuple(group), rows=len(DEFERRED_RECORDS))
        return outcome.ok, outcome

    return isolate(sorted(Document._deferred), check, DEFERRED_CAP)


def test_deferred_fields_can_be_selected(findings: Findings, deferred: dict[str, Verdict[Outcome]]) -> None:
    """Selecting a deferred field (ack, body, caption, citation, credit, mention, reference) is accepted.

    Depends on it: Document.select(Document.body, ...) and lazy loading of
    deferred fields (Document._load_missing). A field that is accepted but
    never returned is restricted (or empty for these records): that is
    recorded in the findings, not failed.
    """
    rows = []
    for name, verdict in sorted(deferred.items()):
        evidence = verdict.evidence
        if verdict.status == "pass" and evidence is not None:
            returned = sum(1 for doc in evidence.docs if doc.get(name) not in (None, []))
            rows.append(f"{name:10} accepted  returned for {returned} of {len(DEFERRED_RECORDS)} records")
        else:
            error = evidence.error if evidence is not None else "not isolated within the request cap"
            rows.append(f"{name:10} {verdict.status:9} {error}")
    findings.add(f"deferred fields on {', '.join(DEFERRED_RECORDS)}", "\n".join(rows))
    failed = [name for name, verdict in deferred.items() if verdict.status != "pass"]
    assert not failed, "deferred fields that cannot be selected:\n  " + "\n  ".join(rows)


def test_deferred_fields_have_declared_types(live: Live, deferred: dict[str, Verdict[Outcome]]) -> None:
    """Deferred fields load without a DataWarning, with their declared types and multivaluedness.

    Depends on it: the field classes of ack, body, caption, citation, credit,
    mention and reference in models/document.py.
    """
    outcomes = {id(v.evidence): v.evidence for v in deferred.values() if v.status == "pass" and v.evidence}
    if not outcomes:
        pytest.skip("no deferred field could be selected (see test_deferred_fields_can_be_selected)")
    problems: list[str] = []
    for outcome in outcomes.values():
        documents, warned = live.documents(outcome)
        problems += [f"DataWarning: {text}" for text in warned]
        problems += type_problems(documents)
        problems += shape_problems(outcome.docs)[0]
    assert not problems, "deferred field values that contradict their declaration:\n  " + "\n  ".join(problems)
