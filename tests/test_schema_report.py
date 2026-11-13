"""Tests for scripts/schema_report.py, on a small synthetic schema."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCHEMA = """<?xml version="1.0" ?>
<schema name="test" version="1.5">
  <types>
    <fieldType name="string" class="solr.StrField"/>
    <fieldType name="int" class="solr.IntPointField"/>
    <fieldType name="ads_text" class="solr.TextField">
      <analyzer type="query"><tokenizer class="solr.WhitespaceTokenizerFactory"/></analyzer>
    </fieldType>
    <fieldType name="keyword" class="solr.TextField">
      <analyzer><tokenizer class="solr.KeywordTokenizerFactory"/></analyzer>
    </fieldType>
    <fieldType name="ids" class="solr.TextField">
      <analyzer type="query"><tokenizer class="solr.PatternTokenizerFactory" pattern="[\\;]"/></analyzer>
    </fieldType>
    <fieldType name="author" class="solr.TextField">
      <analyzer type="query">
        <tokenizer class="solr.KeywordTokenizerFactory"/>
        <filter class="solr.AuthorNormalizeFilterFactory"/>
      </analyzer>
    </fieldType>
  </types>
  <field name="id" type="string"/>
  <field name="bibcode" type="keyword"/>
  <field name="title" type="ads_text" multiValued="true"/>
  <field name="author" type="author" multiValued="true"/>
  <field name="aff_id" type="ids" multiValued="true"/>
  <field name="volume" type="int"/>
  <field name="lang" type="keyword" stored="${storeAll:true}"/>
  <field name="page_range" type="string"/>
  <field name="links_data" type="string" multiValued="true" indexed="false"/>
  <field name="doi" type="string" multiValued="true" indexed="${indexAll:false}"/>
  <field name="secret_new_field" type="string"/>
  <field name="title_facet" type="string" stored="false"/>
  <copyField source="id" dest="recid"/>
</schema>
"""


def load() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "schema_report.py"
    spec = importlib.util.spec_from_file_location("schema_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it executes
    spec.loader.exec_module(module)
    return module


def test_report(tmp_path: Path) -> None:
    schema = tmp_path / "schema.xml"
    schema.write_text(SCHEMA)
    lines = load().report(str(schema))
    text = "\n".join(lines)
    assert any(line.startswith("MISSING") and "secret_new_field" in line for line in lines)
    assert any(line.startswith("internal") and "title_facet" in line for line in lines)
    assert any(line.startswith("pseudo") and "score" in line for line in lines)
    assert "volume" in text and "class IntegerField -> StringField" in text
    assert "lang" in text and "stored True -> False" in text
    differences = lines[lines.index("# Differences (schema -> Document)") + 1 : lines.index("# copyField")]
    for name in ("id ", "bibcode ", "title ", "author ", "aff_id ", "links_data "):
        assert not any(line.startswith(name) for line in differences), name
    assert "id -> recid" in text


def test_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert load().main(["schema_report.py"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_report_compares_indexing(tmp_path: Path) -> None:
    schema = tmp_path / "schema.xml"
    schema.write_text(SCHEMA)
    lines = load().report(str(schema))
    differences = lines[lines.index("# Differences (schema -> Document)") + 1 : lines.index("# copyField")]
    by_name = {line.split()[0]: line for line in differences if line}
    assert by_name["page_range"].endswith("indexed True -> False")
    assert by_name["doi"].endswith("indexed False -> True")
    assert "links_data" not in by_name
    assert any(line.startswith("virtual") and " full " in line and "indexed=True" in line for line in lines)


def test_fields_are_matched_by_solr_name(tmp_path: Path) -> None:
    # Document.uat_name and Document.planetary_feature_name are the Solr fields uat and planetary_feature.
    schema = tmp_path / "schema.xml"
    schema.write_text(
        SCHEMA.replace(
            '<field name="secret_new_field" type="string"/>',
            '<field name="uat" type="ids" multiValued="true"/>'
            '<field name="uat_id" type="int" multiValued="true"/>'
            '<field name="planetary_feature" type="ads_text" multiValued="true"/>'
            '<field name="planetary_feature_id" type="int" multiValued="true"/>',
        )
    )
    lines = load().report(str(schema))
    for name in ("uat", "uat_id", "planetary_feature", "planetary_feature_id", "uat_name", "planetary_feature_name"):
        assert not any(line.split()[1:2] == [name] for line in lines if line.startswith(("MISSING", "UNKNOWN"))), name
    differences = lines[lines.index("# Differences (schema -> Document)") + 1 : lines.index("# copyField")]
    assert not any(line.split()[:1] in (["uat"], ["planetary_feature"]) for line in differences)
