"""Document.uat_concepts and Document.planetary_features: relations to the downloaded vocabularies."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from search_fakes import FakeSearch, make_docs

import scixplorer.data as data
import scixplorer.search
from scixplorer.exceptions import DataNotAvailable, LazyLoadWarning, UnsupportedQueryError
from scixplorer.expr import Expr
from scixplorer.fields import IntegerField, StringField, TextField
from scixplorer.models.document import Document, SearchQuery
from scixplorer.models.planetary import PlanetaryFeature
from scixplorer.models.uat import UATConcept
from scixplorer.search import plan, to_solr

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty cache: the vocabularies are not downloaded."""
    directory = tmp_path / "cache"
    monkeypatch.setenv("SCIX_CACHE_DIR", str(directory))
    data.clear("uat", "planetary_features")
    yield directory
    data.clear("uat", "planetary_features")


@pytest.fixture
def vocabularies(cache: Path) -> Path:
    """The fixture thesaurus and gazetteer, as if downloaded."""
    cache.mkdir()
    shutil.copy(FIXTURES / "uat.json", cache / "uat.json")
    shutil.copy(FIXTURES / "planetary_features.csv", cache / "planetary_features.csv")
    return cache


# Query strings.

NO_DATA_NEEDED = [
    (Document.uat_concepts == 498, "uat_id:498"),
    (Document.uat_concepts == UATConcept(id=498), "uat_id:498"),
    (Document.uat_concepts.in_([498, 486, 498]), "uat_id:(498 OR 486)"),
    (Document.uat_concepts != 498, "*:* AND -uat_id:498"),
    (Document.planetary_features == 2071, "planetary_feature_id:2071"),
    (Document.planetary_features.in_([PlanetaryFeature(id=2071), 4]), "planetary_feature_id:(2071 OR 4)"),
    ((Document.year == 2020) & (Document.uat_concepts.id == 5), "year:2020 AND uat_id:5"),
    (Document.uat_id == 498, "uat_id:498"),
    (Document.uat == "Exoplanets", 'uat:"Exoplanets"'),
    (Document.planetary_feature == "Gale", 'planetary_feature:"Gale"'),
    (Document.planetary_feature.exact("Gale"), '=planetary_feature:"Gale"'),
]


@pytest.mark.parametrize(("expr", "expected"), NO_DATA_NEEDED)
def test_key_comparisons_need_no_data(cache: Path, expr: Expr, expected: str) -> None:
    assert to_solr(expr) == expected
    assert not cache.exists()


def descendants_of_1() -> Expr:
    concept = UATConcept.lookup(1)
    assert concept is not None
    return Document.uat_concepts.in_(concept.descendants)


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (lambda: Document.uat_concepts == UATConcept.lookup(2), "uat_id:2"),
        (lambda: Document.uat_concepts.name.contains("star"), "uat_id:(2 OR 4 OR 6 OR 9)"),
        (lambda: Document.uat_concepts.name == "binary stars", "uat_id:(4 OR 6)"),
        (lambda: Document.uat_concepts.alt_names == "double stars", "uat_id:4"),
        (lambda: (Document.year == 2020) & (Document.uat_concepts.deprecated == True), "year:2020 AND uat_id:9"),  # noqa: E712
        (descendants_of_1, "uat_id:(2 OR 3 OR 4 OR 5 OR 8 OR 6 OR 7)"),
        (lambda: Document.planetary_features.target == "Titan", "planetary_feature_id:6981"),
        (
            lambda: Document.planetary_features.target == "Mars",
            "planetary_feature_id:(2071 OR 16115 OR 4 OR 90001)",
        ),
        (
            lambda: (Document.planetary_features.target == "Mars")
            & (Document.planetary_features.feature_type == "Crater"),
            "planetary_feature_id:(2071 OR 16115 OR 4 OR 90001) AND planetary_feature_id:(2071 OR 1 OR 16115)",
        ),
        (lambda: ~(Document.planetary_features.name == "gale"), "*:* AND -planetary_feature_id:(2071 OR 90001)"),
        # Implicit relations: a vocabulary model's field used directly in a document query.
        (lambda: UATConcept.name.exact("Stars"), "uat_id:2"),
        (lambda: (Document.year == 2020) & (UATConcept.name == "binary stars"), "year:2020 AND uat_id:(4 OR 6)"),
        (lambda: PlanetaryFeature.target == "Titan", "planetary_feature_id:6981"),
        (lambda: Document.trending(PlanetaryFeature.target == "Titan"), "trending(planetary_feature_id:6981)"),
    ],
)
def test_conditions_resolve_to_bare_integer_ids(vocabularies: Path, build: Any, expected: str) -> None:
    assert to_solr(build()) == expected


def test_resolution_notes(vocabularies: Path) -> None:
    search = plan(Document.select().where(Document.uat_concepts.name.contains("star")))
    assert any(note.startswith("Document.uat_concepts: 4 UATConcept row(s) matched") for note in search.notes)
    empty = plan(Document.select().where(Document.planetary_features.target == "Pluto"))
    assert empty.empty
    assert any("no PlanetaryFeature matched" in note for note in empty.notes)


def test_too_many_related_ids(vocabularies: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scixplorer.search, "MAX_RELATED_VALUES", 3)
    message = "4 PlanetaryFeature rows matched .* at most 3 planetary_feature_id"
    with pytest.raises(UnsupportedQueryError, match=message):
        to_solr(Document.planetary_features.target == "Mars")
    assert to_solr(Document.planetary_features.target == "Titan") == "planetary_feature_id:6981"


# Without the data.


def expected_message(model: Any) -> str:
    """The message the model itself raises when its data is not downloaded."""
    with pytest.raises(DataNotAvailable) as info:
        model.select().all()
    return str(info.value)


@pytest.mark.parametrize(
    ("build", "model", "name"),
    [
        (lambda: Document.uat_concepts.name == "Stars", UATConcept, "uat"),
        (lambda: UATConcept.name == "Stars", UATConcept, "uat"),
        (lambda: Document.planetary_features.target == "Mars", PlanetaryFeature, "planetary_features"),
    ],
)
def test_conditions_needing_data_raise_data_not_available(cache: Path, build: Any, model: Any, name: str) -> None:
    message = expected_message(model)
    assert f'data.refresh("{name}")' in message
    fake = FakeSearch(make_docs(1))
    query = Document.select().where(build()).using(fake.client())
    for run in (lambda: to_solr(query), query.all, query.count, query.explain, lambda: plan(query)):
        with pytest.raises(DataNotAvailable) as info:
            run()
        assert str(info.value) == message
    assert fake.requests == []


def test_instance_access_needs_the_data_only_when_there_are_ids(cache: Path) -> None:
    assert Document(bibcode="X", uat_id=[], planetary_feature_id=None).uat_concepts == []
    assert Document(bibcode="X", planetary_feature_id=None).planetary_features == []
    with pytest.raises(DataNotAvailable, match=r'data\.refresh\("uat"\)'):
        Document(bibcode="X", uat_id=[2]).uat_concepts
    with pytest.raises(DataNotAvailable, match=r'data\.refresh\("planetary_features"\)'):
        Document(bibcode="X", planetary_feature_id=[2071]).planetary_features


# Instances.


def test_instance_resolution(vocabularies: Path) -> None:
    document = Document(bibcode="X", uat_id=[4, 999, 2], planetary_feature_id=[2071, 5, 6981])
    assert document.uat_concepts == [UATConcept.lookup(4), UATConcept.lookup(2)]
    assert [c.name for c in document.uat_concepts] == ["Binary stars", "Stars"]
    assert [f.name for f in document.planetary_features] == ["Gale", "Aaru"]
    assert document.uat_concepts is document.uat_concepts
    document.uat_id = [6]
    assert [c.name for c in document.uat_concepts] == ["Eclipsing binary stars"]


def test_search_results_fill_the_text_fields_and_relations(vocabularies: Path) -> None:
    fake = FakeSearch(
        make_docs(
            1,
            uat=["Stars", "Binary stars"],
            uat_id=[2, 4],
            planetary_feature=["Mars/Crater/Gale"],
            planetary_feature_id=[2071],
        )
    )
    [document] = Document.select().using(fake.client()).all()
    fl = fake.params()["fl"].split(",")
    for name in ("uat", "uat_id", "planetary_feature", "planetary_feature_id", "gpn", "gpn_id"):
        assert fl.count(name) == 1, name
    assert "uat_concepts" not in fl and "planetary_features" not in fl
    assert document.uat == ["Stars", "Binary stars"]
    assert document.planetary_feature == ["Mars/Crater/Gale"]
    assert [c.id for c in document.uat_concepts] == [2, 4]
    assert [f.id for f in document.planetary_features] == [2071]
    assert len(fake.searches) == 1


def test_text_fields_and_relation_keys_load_lazily(vocabularies: Path) -> None:
    fake = FakeSearch(make_docs(2, uat=["Stars"], uat_id=[2]))
    documents = Document.select(Document.year).using(fake.client()).all()
    with pytest.warns(LazyLoadWarning, match="Document.uat"):
        assert documents[0].uat == ["Stars"]
    assert fake.params()["fl"] == "bibcode,id,scix_id,uat"
    with pytest.warns(LazyLoadWarning, match="Document.uat_id"):
        assert [c.name for c in documents[1].uat_concepts] == ["Stars"]
    assert fake.params()["fl"] == "bibcode,id,scix_id,uat_id"
    assert [c.name for c in documents[0].uat_concepts] == ["Stars"]
    assert len(fake.searches) == 3


def test_from_row_reads_the_text_fields() -> None:
    document = Document._from_row({"bibcode": "X", "uat": ["Stars"], "planetary_feature": ["Gale"], "uat_id": [2]})
    assert document.to_dict() == {
        "bibcode": "X",
        "planetary_feature": ["Gale"],
        "uat_id": [2],
        "uat": ["Stars"],
    }


def test_describe_shows_the_text_fields_under_their_solr_names() -> None:
    info = {i.name: i for i in Document.describe()}
    assert "uat_concepts" not in info and "planetary_features" not in info
    uat = info["uat"]
    assert (uat.solr_name, uat.type, uat.match_kind) == ("uat", "list[str]", "keyword")
    planetary = info["planetary_feature"]
    assert (planetary.solr_name, planetary.type, planetary.match_kind) == ("planetary_feature", "list[str]", "text")
    assert (info["uat_id"].solr_name, info["uat_id"].type) == ("uat_id", "list[int]")
    assert [i.solr_name for i in Document.describe()].count("uat") == 1
    assert type(Document._fields["uat"]) is StringField
    assert type(Document._fields["planetary_feature"]) is TextField
    assert type(Document._fields["gpn_id"]) is IntegerField and Document._fields["gpn"].solr_name == "gpn"


def test_relations_and_names() -> None:
    assert Document._relations["uat_concepts"].related_model is UATConcept
    assert Document._relations["planetary_features"].related_model is PlanetaryFeature
    assert Document._relations["uat_concepts"].related_field is UATConcept._fields["id"]
    assert {"uat", "planetary_feature", "uat_concepts", "planetary_features"} <= set(dir(Document))
    assert plan(Document.select("uat", Document.planetary_feature)).fl == (
        "bibcode",
        "id",
        "planetary_feature",
        "scix_id",
        "uat",
    )
    with pytest.raises(AttributeError, match="no field 'uat_concepts'"):
        Document.select("uat_concepts")
    with pytest.raises(TypeError, match="unexpected field 'uat_concepts'"):
        Document(uat_concepts=["Stars"])


def test_search_query_takes_solr_names() -> None:
    query = SearchQuery("star", fl="bibcode,uat,planetary_feature", sort="year desc")
    assert plan(query).fl == ("bibcode", "id", "planetary_feature", "scix_id", "uat")
    assert {field.name for field in query.fields} == {"bibcode", "uat", "planetary_feature"}
