"""Schema drift: Document compared with the published schema.xml of the index (item 14).

Needs network access (GitHub) but no ADS token, so it runs under --live even
without one. The comparison is scripts/schema_report.py's own ``report()``.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from . import schema
from .support import Findings

pytestmark = pytest.mark.live

#: Modelled fields known to be absent from schema.xml, and why they stay; tests/live/test_fields.py
#: checks that ADS still accepts them.
KNOWN_UNSCHEMED = {
    "grant_agencies": "offered by the previous client; see test_field_missing_from_schema_is_searchable",
    "grant_id": "offered by the previous client; see test_field_missing_from_schema_is_searchable",
}

#: The kinds of difference that mean Document declares a field wrongly.
MISMATCHES = ("class ", "multivalued ", "stored ")


class SchemaDriftWarning(UserWarning):
    """The schema has fields Document does not model, or other differences that are not errors."""


def test_document_matches_the_published_schema(schema_path: Path, findings: Findings) -> None:
    """Every modelled field exists in schema.xml with the same field class, multivaluedness and storage.

    Fails on a modelled field the schema lacks (other than query-parser
    virtual fields, score and KNOWN_UNSCHEMED) and on a class, multivalued
    or stored mismatch (other than schema_report.DELIBERATE). Schema fields
    the model does not cover, and stored fields that are not indexed, are
    reported as a SchemaDriftWarning. Depends on it: the field definitions in
    models/document.py.
    """
    module = schema.report_module()
    lines = module.report(str(schema_path))
    groups = schema.sections(lines)
    uncovered = [line for line in groups["Fields in the schema but not in Document"] if line.startswith("MISSING")]
    unknown = [
        line for line in groups["Fields in Document but not in the schema"]
        if line.startswith("UNKNOWN") and line.split()[1] not in KNOWN_UNSCHEMED
    ]
    known = [line for line in groups["Fields in Document but not in the schema"] if line.split()[1] in KNOWN_UNSCHEMED]
    differences = groups["Differences (schema -> Document)"]
    wrong = [
        line for line in differences
        if line.split()[0] not in module.DELIBERATE and any(kind in line for kind in MISMATCHES)
    ]
    notes = [line for line in differences if line not in wrong] + known + uncovered
    if notes:
        findings.add("schema drift (not failures)", "\n".join(notes))
        warnings.warn(
            "schema.xml differs from Document in ways that are not errors:\n  " + "\n  ".join(notes),
            SchemaDriftWarning,
            stacklevel=1,
        )
    table = "\n".join(line for line in lines[: lines.index("# copyField")] if not line.startswith("internal"))
    assert not unknown and not wrong, (
        f"Document disagrees with {schema.SCHEMA_URL}:\n  " + "\n  ".join(unknown + wrong) + f"\n\nFull report:\n{table}"
    )
