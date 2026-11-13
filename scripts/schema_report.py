"""Compare the ADS search index's Solr schema with ``Document.describe()``.

Usage::

    uv run python scripts/schema_report.py path/to/schema.xml

The schema is ``deploy/adsabs/server/solr/collection1/conf/schema.xml`` in the
public ``adsabs/montysolr`` repository. It is read as data only. The report
lists the fields the model lacks, the model fields the schema lacks (virtual
query-parser fields and Solr's ``score`` are expected there), and every
difference in field class, multivaluedness, storage or indexing. The expected
field class is derived from each field type the same way ``Document`` was
classified:

- ``int``/``long`` point fields: ``IntegerField``; ``float``/``double``: ``FloatField``;
  dates: ``DateTimeField``; booleans: ``BooleanField``; the ``date_string`` type: ``DateField``;
- text types whose query analyzer uses the ADS author filters: ``NameField``;
- strings, and text types that keep the whole value as one token or split it
  only on list separators (``;``, ``:``, ``/``, ``|``): ``StringField``;
- other tokenized text types: ``TextField``.

A subclass of the expected class also matches (``NumericStringField`` is a
``StringField``).
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from scixplorer import fields
from scixplorer.models.document import Document

#: Model fields that are deliberately classified differently from the rule above.
DELIBERATE = {
    "year": "Solr keeps a 4-character string; integer queries and ranges are equivalent",
}

#: Search fields the query parser provides (solrconfig.xml ``virtual-fields`` and ``fieldMap``).
QUERY_PARSER = frozenset({"abs", "full", "orcid", "affil", "inst", "arxiv", "collection"})

#: Schema field name patterns that users never query (facets, copies, internals).
INTERNAL = re.compile(
    r"(_facet|_facet_hier.*|_nosyn|_wildcard|_ctime|_mtime|_boost|^_version_$|^tmp_|"
    r"^a_\d+|^author_(notrans|fuzzy|short_name_rage|collector)|^unfielded_search|^query_normalizer$)"
)

_PROPERTY = re.compile(r"^\$\{[^:}]+:(.*)\}$")
_LIST_SEPARATORS = re.compile(r"^\[?[\\;:/|]+\]?$")


@dataclass(frozen=True)
class SchemaField:
    name: str
    type: str
    expected: str
    multivalued: bool
    stored: bool
    indexed: bool


def _flag(value: str | None, default: bool) -> bool:
    """A schema boolean, resolving ``${property:default}`` to its default."""
    if value is None:
        return default
    match = _PROPERTY.match(value)
    return (match.group(1) if match else value).strip().lower() == "true"


def _expected_class(field_type: ET.Element) -> str:
    solr_class = field_type.get("class", "")
    name = field_type.get("name", "")
    for marker, result in (
        ("Int", "IntegerField"),
        ("Long", "IntegerField"),
        ("Float", "FloatField"),
        ("Double", "FloatField"),
        ("Date", "DateTimeField"),
        ("Bool", "BooleanField"),
        ("StrField", "StringField"),
    ):
        if marker in solr_class.rsplit(".", 1)[-1]:
            return result
    if name == "date_string":
        return "DateField"
    analyzers = field_type.findall("analyzer")
    query = next((a for a in analyzers if a.get("type") == "query"), analyzers[0] if analyzers else None)
    if query is None:
        return "StringField"
    filters = [f.get("class", "") for f in query.findall("filter")]
    if any("Author" in f for f in filters):
        return "NameField"
    tokenizer = query.find("tokenizer")
    tokenizer_class = "" if tokenizer is None else tokenizer.get("class", "")
    if "KeywordTokenizer" in tokenizer_class:
        return "StringField"
    pattern = "" if tokenizer is None else tokenizer.get("pattern", "")
    if "PatternTokenizer" in tokenizer_class and _LIST_SEPARATORS.match(pattern):
        return "StringField"
    return "TextField"


def read_schema(path: str) -> tuple[dict[str, SchemaField], list[tuple[str, str]]]:
    """The schema's fields by name, and its copyField (source, dest) pairs."""
    root = ET.parse(path).getroot()
    types = {t.get("name", ""): t for t in root.iter("fieldType")}
    fields = {}
    for element in root.iter("field"):
        type_name = element.get("type", "")
        field_type = types.get(type_name, ET.Element("fieldType"))

        def attr(key: str, default: bool, element: ET.Element = element, field_type: ET.Element = field_type) -> bool:
            return _flag(element.get(key, field_type.get(key)), default)

        name = element.get("name", "")
        fields[name] = SchemaField(
            name=name,
            type=type_name,
            expected=_expected_class(field_type),
            multivalued=attr("multiValued", False),
            stored=attr("stored", True),
            indexed=attr("indexed", True),
        )
    copies = [(c.get("source", ""), c.get("dest", "")) for c in root.iter("copyField")]
    return fields, copies


def report(path: str) -> list[str]:
    """The report lines comparing the schema at ``path`` with ``Document``."""
    schema, copies = read_schema(path)
    model = {info.solr_name: (info, type(Document._fields[info.name]).__name__) for info in Document.describe()}
    lines = ["# Fields in the schema but not in Document"]
    for name, field in schema.items():
        if name not in model:
            kind = "internal" if INTERNAL.search(name) else "MISSING"
            lines.append(
                f"{kind:9} {name:36} {field.type:38} multivalued={field.multivalued} "
                f"stored={field.stored} indexed={field.indexed}"
            )
    lines.append("")
    lines.append("# Fields in Document but not in the schema")
    for name, (info, class_name) in model.items():
        if name not in schema:
            kind = "virtual" if name in QUERY_PARSER else "pseudo" if name == "score" else "UNKNOWN"
            lines.append(f"{kind:9} {name:36} {class_name:14} indexed={info.indexed}")
    lines.append("")
    lines.append("# Differences (schema -> Document)")
    for name, (info, class_name) in model.items():
        known = schema.get(name)
        if known is None:
            continue
        problems = []
        field = Document._fields[info.name]
        if class_name != "VirtualField" and not isinstance(field, getattr(fields, known.expected)):
            problems.append(f"class {known.expected} -> {class_name}")
        if known.multivalued != info.multivalued and class_name != "VirtualField":
            problems.append(f"multivalued {known.multivalued} -> {info.multivalued}")
        if known.stored != info.stored:
            problems.append(f"stored {known.stored} -> {info.stored}")
        if known.indexed != info.indexed:
            problems.append(f"indexed {known.indexed} -> {info.indexed}")
        if problems:
            note = f"  ({DELIBERATE[name]})" if name in DELIBERATE else ""
            lines.append(f"{name:36} {known.type:38} {'; '.join(problems)}{note}")
    lines.append("")
    lines.append("# copyField")
    lines.extend(f"{source} -> {dest}" for source, dest in copies)
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/schema_report.py path/to/schema.xml", file=sys.stderr)
        return 2
    print("\n".join(report(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
