"""RelationExpr.any(predicate): several conditions about the same related object."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import ads.data as data
from ads.evaluate import matches
from ads.exceptions import QueryError
from ads.expr import Expr, Match, Related
from ads.models.affiliation import Affiliation
from ads.models.author import Author
from ads.models.document import Document
from ads.models.journal import Journal
from ads.models.planetary import PlanetaryFeature
from ads.models.uat import UATConcept
from ads.search import plan, to_solr

FIXTURES = Path(__file__).parent / "fixtures"

MARS_CRATER = (PlanetaryFeature.target == "Mars") & (PlanetaryFeature.feature_type == "Crater")
AU_OBSERVATORY = (Affiliation.country == "Australia") & Affiliation.canonical_name.contains("Observatory")


@pytest.fixture(autouse=True)
def vocabularies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The fixture thesaurus and gazetteer, as if downloaded."""
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path))
    data.clear("uat", "planetary_features")
    shutil.copy(FIXTURES / "uat.json", tmp_path / "uat.json")
    shutil.copy(FIXTURES / "planetary_features.csv", tmp_path / "planetary_features.csv")
    yield
    data.clear("uat", "planetary_features")


def ids(expr: Expr) -> list[str]:
    return [a.id for a in Affiliation.select().where(expr) if a.id is not None]


def any_of(field: str, values: list[Any]) -> str:
    quoted = [f'"{v}"' if isinstance(v, str) else str(v) for v in values]
    return f"{field}:{quoted[0]}" if len(quoted) == 1 else f"{field}:(" + " OR ".join(quoted) + ")"


# Query strings.


def test_conditions_inside_any_are_about_one_related_object() -> None:
    together = Document.planetary_feature.any(MARS_CRATER)
    apart = (Document.planetary_feature.target == "Mars") & (Document.planetary_feature.feature_type == "Crater")
    assert to_solr(together) == "planetary_feature_id:(2071 OR 16115)"
    assert to_solr(apart) == (
        "planetary_feature_id:(2071 OR 16115 OR 4 OR 90001) AND planetary_feature_id:(2071 OR 1 OR 16115)"
    )
    # A document mentioning a crater on Mathilde (1) and a collis on Mars (4) mentions some
    # Mars feature and some crater, but no Mars crater.
    document = Document(bibcode="X", planetary_feature_id=[1, 4])
    assert matches(apart, document) and not matches(together, document)
    assert matches(together, Document(bibcode="Y", planetary_feature_id=[7097, 16115]))


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (lambda: Document.planetary_feature.any(PlanetaryFeature.target == "Titan"), "planetary_feature_id:6981"),
        (
            lambda: Document.uat.any(UATConcept.name.contains("star") & (UATConcept.deprecated == False)),  # noqa: E712
            "uat_id:(2 OR 4 OR 6)",
        ),
        (lambda: Document.uat.any(UATConcept.name.contains("star") & UATConcept.name.contains("binary")), "uat_id:(4 OR 6)"),
        (
            lambda: Document.journal.any(Journal.title.contains("Astrophysical") & Journal.abbreviation.startswith("ApJ")),
            'bibstem:("ApJ" OR "ApJS")',
        ),
        (lambda: Document.affiliation.any(AU_OBSERVATORY), lambda: any_of("aff_id", ids(AU_OBSERVATORY))),
        (lambda: Document.authors.any(Author.affiliation.any(AU_OBSERVATORY)), lambda: any_of("aff_id", ids(AU_OBSERVATORY))),
        (
            lambda: Document.authors[0].affiliation.any(AU_OBSERVATORY),
            lambda: f"pos({any_of('aff_id', ids(AU_OBSERVATORY))}, 1)",
        ),
        (
            lambda: (Document.year == 2020) & ~Document.planetary_feature.any(MARS_CRATER),
            "year:2020 AND -planetary_feature_id:(2071 OR 16115)",
        ),
        (lambda: Document.planetary_feature.any(MARS_CRATER & (PlanetaryFeature.name == "Mars")), "*:* AND -*:*"),
    ],
)
def test_golden_query_strings(build: Any, expected: Any) -> None:
    assert to_solr(build()) == (expected() if callable(expected) else expected)


def test_affiliation_any_through_an_author() -> None:
    keys = ids(AU_OBSERVATORY)
    assert 1 < len(keys) < len(ids(Affiliation.country == "Australia"))
    together = Document.authors.any((Author.name == "Casey, A") & Author.affiliation.any(AU_OBSERVATORY))
    search = plan(Document.select().where(together & (Document.year == 2020)))
    assert search.q == f'author:"Casey, A" AND {any_of("aff_id", keys)} AND year:2020'
    assert search.refinement == together


def test_resolution_notes() -> None:
    notes = plan(Document.select().where(Document.planetary_feature.any(MARS_CRATER))).notes
    assert any(note.startswith("Document.planetary_feature: 2 PlanetaryFeature row(s) matched") for note in notes)


def test_any_builds_a_related_node() -> None:
    relation = Document._relations["planetary_feature"]
    assert Document.planetary_feature.any(MARS_CRATER) == Related(relation, MARS_CRATER)
    assert Document.authors[1].affiliation.any(AU_OBSERVATORY) == Document.authors[1].any(
        Author.affiliation.any(AU_OBSERVATORY)
    )


def test_the_implicit_form_is_unchanged() -> None:
    relation = Document._relations["planetary_feature"]
    target = PlanetaryFeature._fields["target"]
    assert (Document.planetary_feature.target == "Mars") == Related(relation, Match(target, "Mars"))
    assert to_solr(PlanetaryFeature.target == "Mars") == to_solr(Document.planetary_feature.target == "Mars")
    assert to_solr(Document.planetary_feature.any(PlanetaryFeature.target == "Mars")) == to_solr(
        Document.planetary_feature.target == "Mars"
    )


# Construction errors.


@pytest.mark.parametrize(
    ("build", "error", "match"),
    [
        (
            lambda: Document.planetary_feature.any(Document.year == 2020),
            QueryError,
            r"Document\.year is a field of Document, not of PlanetaryFeature: everything inside "
            r"Document\.planetary_feature\.any\(\.\.\.\) is about one planetary feature",
        ),
        (lambda: Document.uat.any(PlanetaryFeature.target == "Mars"), QueryError, "not a field of UATConcept; inside"),
        (lambda: Document.journal.any("ApJ"), TypeError, "needs an expression over Journal fields"),
        (lambda: Document.journal.any(Journal.title), TypeError, "needs an expression over Journal fields"),
        (lambda: Document.affiliation.any(True), TypeError, "got the bool True.*and/or/not"),
        (
            lambda: Document.planetary_feature.any((PlanetaryFeature.target == "Mars") and (PlanetaryFeature.name == "x")),
            TypeError,
            "truth value",
        ),
        (lambda: Document.uat.any(Document.raw("x")), QueryError, "cannot contain raw Solr text"),
        (lambda: Document.uat.any(Document.citations(UATConcept.id == 1)), QueryError, "cannot contain an operator"),
        (lambda: Document.uat.any(UATConcept.alt_names[0] == "x"), QueryError, "cannot restrict positions"),
        (lambda: Document.affiliation.any(Document.authors.any(Author.name == "x")), QueryError, "nested"),
        (lambda: Author.affiliation.any(Author.name == "x"), QueryError, "Author.name is a field of Author"),
        (lambda: Document.planetary_feature.all, AttributeError, r'"every related planetary feature" cannot be searched'),
    ],
)
def test_construction_errors(build: Any, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        build()


def test_all_on_a_relation_points_to_any() -> None:
    for build, relation in [
        (lambda: Document.planetary_feature.all, "Document.planetary_feature"),
        (lambda: Document.journal.all, "Document.journal"),
        (lambda: Document.authors[0].affiliation.all, "Author.affiliation"),
    ]:
        with pytest.raises(AttributeError) as info:
            build()
        message = str(info.value)
        assert message.startswith(f"{relation} has no .all(): a condition on \"every related")
        assert f"Use {relation}.any(...)" in message


def test_field_through_a_relation_points_to_any() -> None:
    with pytest.raises(AttributeError) as info:
        Document.planetary_feature.target.any  # type: ignore[attr-defined]  # noqa: B018
    assert str(info.value) == (
        'Document.planetary_feature.target has no .any(): a comparison through a relation already means "any '
        'related planetary feature" (Document.planetary_feature.target == x matches when any of them does). '
        "To say several things about the same planetary feature, use Document.planetary_feature.any(...) "
        "with conditions on PlanetaryFeature fields."
    )


# Evaluation.


def test_evaluation_on_a_loaded_document() -> None:
    au, nz = ids(AU_OBSERVATORY)[0], ids(Affiliation.country == "New Zealand")[0]
    other_au = next(i for i in ids(Affiliation.country == "Australia") if i not in ids(AU_OBSERVATORY))
    document = Document(
        bibcode="X",
        author=["Casey, A.", "Ness, M."],
        aff_id=[f"{nz}; {other_au}", au],
        planetary_feature_id=[2071],
        uat_id=[9],
        bibstem=["ApJ"],
    )
    for name in Document.authors.collection.owner_fields():
        document._data.setdefault(name, None)
    assert matches(Document.affiliation.any(AU_OBSERVATORY), document)
    assert matches(Document.authors.any(Author.affiliation.any(AU_OBSERVATORY)), document)
    assert not matches(Document.authors[0].affiliation.any(AU_OBSERVATORY), document)
    assert matches(Document.authors[1].affiliation.any(AU_OBSERVATORY), document)
    assert not matches(Document.authors.any((Author.name == "Casey, A") & Author.affiliation.any(AU_OBSERVATORY)), document)
    assert matches(Document.planetary_feature.any(MARS_CRATER), document)
    assert not matches(Document.uat.any(UATConcept.name.contains("star") & (UATConcept.deprecated == False)), document)  # noqa: E712
    assert matches(Document.journal.any(Journal.abbreviation.startswith("ApJ")), document)
