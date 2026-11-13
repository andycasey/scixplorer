"""Tests for per-author conditions: the Quantified node, Author, Document.authors, hints and evaluation."""

from __future__ import annotations

import copy
import pickle
import warnings
from typing import Any

import pytest
from search_fakes import FakeSearch, bibcodes

from ads import solr
from ads.evaluate import matches
from ads.exceptions import LazyLoadWarning, NotLocallyEvaluable, QueryError
from ads.expr import EVERYTHING, NOTHING, And, Match, Not, Quantified, Related, SolrLiteral, simplify, transform, walk
from ads.fields import IntegerField, NameField, StringField, TextField
from ads.model import CollectionExpr, RecordExpr
from ads.models.affiliation import Affiliation
from ads.models.author import Author, AuthorExpr
from ads.models.document import Document
from ads.models.journal import Journal
from ads.search import to_solr


def country_ids(country: str) -> list[str]:
    return [a.id for a in Affiliation.select().where(Affiliation.country == country) if a.id is not None]


AU = country_ids("Australia")
NZ = country_ids("New Zealand")
CASEY = Document._fields["author"]
UNKNOWN = "Z0000"

#: A realistic row: Casey has two affiliations (Australia and New Zealand), Ness
#: has none ("-"), Hogg's id is not in the packaged data; ORCIDs come from
#: different arrays, and orcid_other is shorter than the author list.
ROW: dict[str, Any] = {
    "bibcode": "2020ApJ...00000000A",
    "id": "1",
    "author": ["Casey, Andrew R.", "Ness, Melissa K.", "Hogg, David W."],
    "aff": ["Monash University; University of Auckland", "-", "New York University"],
    "aff_id": [f"{AU[0]}; {NZ[0]}", "-", UNKNOWN],
    "email": ["andrew@example.org", "-", "-"],
    "orcid_pub": ["0000-0003-0174-0564", "-", "-"],
    "orcid_user": ["-", "0000-0001-5082-6693", "-"],
    "orcid_other": ["-"],
}


def loaded(row: dict[str, Any] = ROW) -> Document:
    """A document as a search returning every per-author field builds it (no lazy loading)."""
    document = Document._from_row(row)
    for name in Document.authors.collection.owner_fields():
        document._data.setdefault(name, None)
    return document


def test_fixture_data() -> None:
    assert len(AU) > 1 and len(NZ) > 1
    assert Affiliation.lookup(UNKNOWN) is None


# The Quantified node.


def test_quantified_is_built_by_the_handle() -> None:
    expr = Document.authors.any(Author.name == "Casey, A")
    assert expr == Quantified(Document.authors.collection, Author.name == "Casey, A", "any", None)
    assert hash(expr) == hash(Document.authors.any(Author.name == "Casey, A"))
    assert Document.authors.all(Author.name == "Casey, A") != expr
    assert Document.authors[0:3].any(Author.name == "Casey, A") == Quantified(
        Document.authors.collection, Author.name == "Casey, A", "any", (1, 3)
    )
    shorthand = Document.authors[2].name == "Casey, A"
    assert shorthand == Quantified(Document.authors.collection, Author.name == "Casey, A", "any", (3, 3))


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"predicate": "x"}, TypeError),
        ({"quantifier": "some"}, ValueError),
        ({"positions": (0, 1)}, ValueError),
        ({"positions": (3, 2)}, ValueError),
    ],
)
def test_quantified_validates_its_parts(kwargs: dict[str, Any], error: type[Exception]) -> None:
    arguments: dict[str, Any] = {"collection": Document.authors.collection, "predicate": EVERYTHING, **kwargs}
    with pytest.raises(error):
        Quantified(**arguments)


def test_walk_and_transform_reach_the_predicate() -> None:
    inner = (Author.name == "Casey, A") & (Author.aff == "Monash")
    expr = Document.authors.any(inner) & (Document.year == 2020)
    assert inner in list(walk(expr))
    assert (Author.aff == "Monash") in list(walk(expr))

    def swap(node: Any) -> Any:
        return Author.aff == "Auckland" if node == (Author.aff == "Monash") else node

    rebuilt = transform(expr, swap)
    assert isinstance(rebuilt, And)
    quantified = rebuilt.operands[0]
    assert isinstance(quantified, Quantified) and quantified.predicate == (Author.name == "Casey, A") & (
        Author.aff == "Auckland"
    )


def test_simplify_simplifies_the_predicate() -> None:
    name = Author.name == "Casey, A"
    expr = Document.authors[0:2].all(And((name, And((name, EVERYTHING)))))
    assert simplify(expr) == Document.authors[0:2].all(name)


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (lambda h: h.any(NOTHING), NOTHING),
        (lambda h: h.all(NOTHING), NOTHING),
        (lambda h: h.any(EVERYTHING), "any(EVERYTHING)"),
        (lambda h: h.all(EVERYTHING), "any(EVERYTHING)"),
        (lambda h: h.any((Author.name == "x") & NOTHING), NOTHING),
        (lambda h: h.all((Author.name == "x") | EVERYTHING), "any(EVERYTHING)"),
    ],
)
def test_simplify_constant_predicates(build: Any, expected: Any) -> None:
    for handle in (Document.authors, Document.authors[1:4]):
        result = simplify(build(handle))
        if expected == "any(EVERYTHING)":
            # "Has at least one author (at those positions)": not EVERYTHING, since a document may have none.
            assert result == handle.any(EVERYTHING)
        else:
            assert result == expected


def test_compiling_an_unresolved_quantified_node_is_an_error() -> None:
    with pytest.raises(QueryError, match=r"unresolved per-record condition \(Document\.authors\[0\]\.any"):
        solr.compile(Document.authors[0].name == "Casey, A")


# The Author model.


def test_author_fields() -> None:
    kinds = {
        "position": IntegerField,
        "name": NameField,
        "aff": TextField,
        "aff_id": StringField,
        "email": type(Document._fields["email"]),
        "orcid_pub": type(Document._fields["orcid_pub"]),
        "orcid_user": type(Document._fields["orcid_user"]),
        "orcid_other": type(Document._fields["orcid_other"]),
        "orcid": StringField,
    }
    assert {name: type(field) for name, field in Author._fields.items()} == kinds
    assert [name for name, field in Author._fields.items() if field.multivalued] == ["aff_id"]
    assert all(field.__doc__ for field in Author._fields.values())
    assert list(Author._relations) == ["affiliation"]
    relation = Author._relations["affiliation"]
    assert (relation.related_model, relation.key, relation.related_key) == (Affiliation, "aff_id", "id")


def test_author_has_no_backend() -> None:
    with pytest.raises(TypeError, match="Author has no backend, so it cannot be queried"):
        Author.select().where(Author.name == "Casey, A").all()


def test_collection_mapping_is_declared() -> None:
    collection = Document.authors.collection
    assert collection.record_model is Author and collection.length == "author" and collection.index == "position"
    assert {name: (s.fields, s.search) for name, s in collection.sources.items()} == {
        "name": (("author",), "author"),
        "aff": (("aff",), "aff"),
        "aff_id": (("aff_id",), "aff_id"),
        "email": (("email",), "email"),
        "orcid_pub": (("orcid_pub",), "orcid_pub"),
        "orcid_user": (("orcid_user",), "orcid_user"),
        "orcid_other": (("orcid_other",), "orcid_other"),
        "orcid": (("orcid_pub", "orcid_user", "orcid_other"), "orcid"),
    }
    assert Document._collections == {"authors": collection}
    assert collection.owner_fields() == ("author", "aff", "aff_id", "email", "orcid_pub", "orcid_user", "orcid_other")
    assert collection.owner_fields(["orcid"]) == ("author", "orcid_pub", "orcid_user", "orcid_other")


# Records.


def test_authors_of_a_realistic_row() -> None:
    casey, ness, hogg = loaded().authors
    assert isinstance(casey, Author)
    assert casey.to_dict() == {
        "position": 0,
        "name": "Casey, Andrew R.",
        "aff": "Monash University; University of Auckland",
        "aff_id": [AU[0], NZ[0]],
        "email": "andrew@example.org",
        "orcid_pub": "0000-0003-0174-0564",
        "orcid_user": None,
        "orcid_other": None,
        "orcid": "0000-0003-0174-0564",
    }
    assert casey.affiliation == (Affiliation.lookup(AU[0]), Affiliation.lookup(NZ[0]))
    assert ness.to_dict() == {
        "position": 1,
        "name": "Ness, Melissa K.",
        "aff": None,
        "aff_id": [],
        "email": None,
        "orcid_pub": None,
        "orcid_user": "0000-0001-5082-6693",
        "orcid_other": None,
        "orcid": "0000-0001-5082-6693",
    }
    assert ness.affiliation == ()
    assert (hogg.position, hogg.aff, hogg.aff_id, hogg.affiliation, hogg.orcid) == (
        2,
        "New York University",
        [UNKNOWN],
        (),
        None,
    )
    assert repr(casey) == "<Author 0: 'Casey, Andrew R.'>"


def test_unequal_and_missing_arrays() -> None:
    row = {
        "bibcode": "X",
        "author": ["A, A", "B, B", "C, C"],
        "aff": ["Only first"],
        "aff_id": [f"{AU[0]} ;{AU[1]};-; ", "-", "-", "-", f"{NZ[0]}"],
        "orcid_user": ["-", "", "0000-0001-0000-0000"],
    }
    authors = loaded(row).authors
    assert [a.aff for a in authors] == ["Only first", None, None]
    assert [a.aff_id for a in authors] == [[AU[0], AU[1]], [], []]
    assert [a.email for a in authors] == [None, None, None]
    assert [a.orcid for a in authors] == [None, None, "0000-0001-0000-0000"]


def test_document_without_authors() -> None:
    assert loaded({"bibcode": "X", "aff": ["x"]}).authors == []
    assert loaded({"bibcode": "X", "author": []}).authors == []


def test_authors_are_cached_until_a_field_changes() -> None:
    document = loaded()
    authors = document.authors
    assert document.authors is authors
    document.author = ["Someone, S"]
    assert [a.name for a in document.authors] == ["Someone, S"]
    assert document.authors is not authors
    document.aff_id = [NZ[1]]
    assert document.authors[0].affiliation == (Affiliation.lookup(NZ[1]),)


def test_authors_cannot_be_set() -> None:
    with pytest.raises(AttributeError, match="cannot be set"):
        loaded().authors = []  # type: ignore[misc]


def test_authors_survive_pickling() -> None:
    copy_of = pickle.loads(pickle.dumps(loaded()))
    assert [a.name for a in copy_of.authors] == ROW["author"]


# Lazy loading.


def narrow_page(fake: FakeSearch) -> list[Document]:
    return Document.select(Document.title).using(fake.client()).all()


def rows(n: int) -> list[dict[str, Any]]:
    return [{**ROW, "bibcode": b, "id": str(i), "title": [f"Paper {i}"]} for i, b in enumerate(bibcodes(n))]


def test_reading_authors_loads_every_needed_field_with_one_request() -> None:
    fake = FakeSearch(rows(3))
    documents = narrow_page(fake)
    assert len(fake.searches) == 1
    with pytest.warns(LazyLoadWarning) as caught:
        authors = documents[0].authors
    assert len(caught) == 1 and caught[0].filename == __file__
    message = str(caught[0].message)
    assert message.startswith(
        "Document.aff, Document.aff_id, Document.author, Document.email, Document.orcid_other, "
        "Document.orcid_pub, Document.orcid_user were not fetched; loading them for 3 document(s)"
    )
    assert len(fake.searches) == 2
    assert fake.params()["fl"] == "aff,aff_id,author,bibcode,email,id,orcid_other,orcid_pub,orcid_user,scix_id"
    assert [a.name for a in authors] == ROW["author"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert documents[0].authors is authors
        assert [a.orcid for a in documents[2].authors] == ["0000-0003-0174-0564", "0000-0001-5082-6693", None]
        assert documents[1].email == ROW["email"]
    assert len(fake.searches) == 2


def test_only_missing_fields_are_loaded() -> None:
    fake = FakeSearch(rows(2))
    documents = Document.select(Document.author, Document.aff_id, Document.aff, Document.email).using(fake.client()).all()
    with pytest.warns(LazyLoadWarning, match=r"^Document\.orcid_other, Document\.orcid_pub, Document\.orcid_user were"):
        documents[1].authors
    assert fake.params()["fl"] == "bibcode,id,orcid_other,orcid_pub,orcid_user,scix_id"


def test_hand_built_document_without_identifier_loads_nothing() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert [a.name for a in Document(author=["A, B"]).authors] == ["A, B"]


# Construction errors.


@pytest.mark.parametrize(
    ("build", "error", "match"),
    [
        (lambda: Document.authors.any(Document.year == 2020), QueryError, "Document.year is a field of Document, not of Author"),
        (
            lambda: Document.authors.any((Author.name == "x") & (Document.author == "y")),
            QueryError,
            "everything inside Document.authors.any",
        ),
        (lambda: Document.authors.any(Affiliation.country == "Chile"), QueryError, "through the relation Author.affiliation"),
        (lambda: Document.authors.any(Journal.title == "x"), QueryError, "Journal.title is not a field of Author"),
        (lambda: Document.authors.any(Document.affiliation.country == "Chile"), QueryError, "not a relation of Author"),
        (lambda: Document.authors.any("Casey, A"), TypeError, "needs an expression over Author fields"),
        (lambda: Document.authors.any(Author.name), TypeError, "needs an expression"),
        (lambda: Document.authors.all(True), TypeError, "got the bool True.*and/or/not"),
        (lambda: Document.authors[0].any((Author.name == "x") and (Author.aff == "y")), TypeError, "truth value"),
        (lambda: Document.authors.any(Document.raw("aff:x")), QueryError, "cannot contain raw Solr text"),
        (lambda: Document.authors.any(Document.citations(Author.name == "x")), QueryError, "cannot contain an operator"),
        (lambda: Document.authors.any(Author.aff_id[0] == "x"), QueryError, "cannot restrict positions"),
        (lambda: Document.authors.any(Document.authors.any(Author.name == "x")), QueryError, "nested"),
        (lambda: Document.authors.name, AttributeError, r"Document\.authors\.name: say which authors"),
        (lambda: Document.authors[0:2].orcid, AttributeError, r"Document\.authors\[0:2\]\.any\(Author\.orcid"),
        (lambda: Document.authors.nothing, AttributeError, "has no attribute 'nothing'"),
        (lambda: Document.authors[0].nothing, AttributeError, "Author has no field 'nothing'; fields are: position"),
        (lambda: Document.authors[-1], QueryError, "from the end"),
        (lambda: Document.authors[2:], QueryError, "from the end"),
        (lambda: Document.authors[-3:-1], QueryError, "from the end"),
        (lambda: Document.authors[0:4:2], QueryError, "step"),
        (lambda: Document.authors[3:3], QueryError, "Empty"),
        (lambda: Document.authors[True], TypeError, "ints or slices"),
        (lambda: Document.authors["x"], TypeError, "ints or slices"),
        (lambda: Document.authors[0:2][0], TypeError, "already restricted"),
        (lambda: Document.authors[0][0], TypeError, "already restricted"),
        (lambda: Document.authors == "Casey, A", TypeError, "cannot be compared"),
        (lambda: Document.authors[0] != "Casey, A", TypeError, "cannot be compared"),
        (lambda: bool(Document.authors), TypeError, "truth value"),
        (lambda: iter(Document.authors[0]), TypeError, "not iterable"),
        (lambda: Document.authors[0].name.desc(), QueryError, "Cannot sort"),
        (lambda: Document.select(Document.authors[0].name), TypeError, "not a plain field"),
    ],
)
def test_construction_errors(build: Any, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        build()


def test_handles() -> None:
    assert isinstance(Document.authors, CollectionExpr) and not isinstance(Document.authors, RecordExpr)
    assert isinstance(Document.authors[0], AuthorExpr)
    assert type(Document.authors[0:1]) is CollectionExpr
    assert Document.authors[1:3].positions == (2, 3) and Document.authors.positions is None
    assert [repr(h) for h in (Document.authors, Document.authors[0], Document.authors[1:3])] == [
        "<Document.authors>",
        "<Document.authors[0]>",
        "<Document.authors[1:3]>",
    ]
    assert repr(Document.authors[1].name) == "<Document.authors[1].name>"
    assert repr(Document.authors[1].affiliation) == "<Document.authors[1].affiliation>"
    assert repr(Document.authors[1].affiliation.country) == "<Document.authors[1].affiliation.country>"
    assert {"any", "all"} <= set(dir(Document.authors))
    assert {"any", "all", "name", "orcid", "affiliation"} <= set(dir(Document.authors[0]))
    assert "authors" in dir(Document)


@pytest.mark.parametrize(
    ("shorthand", "explicit"),
    [
        (lambda: Document.authors[0].orcid == "X", lambda: Document.authors[0].any(Author.orcid == "X")),
        (lambda: Document.authors[0].name.exact("A, B"), lambda: Document.authors[0].any(Author.name.exact("A, B"))),
        (lambda: Document.authors[0].email.like("*@x.org"), lambda: Document.authors[0].any(Author.email.like("*@x.org"))),
        (lambda: Document.authors[0].aff_id.in_(["A", "B"]), lambda: Document.authors[0].any(Author.aff_id.in_(["A", "B"]))),
        (lambda: Document.authors[0].position >= 0, lambda: Document.authors[0].any(Author.position >= 0)),
        (
            lambda: Document.authors[0].affiliation.country == "Chile",
            lambda: Document.authors[0].any(Author.affiliation.country == "Chile"),
        ),
        (lambda: Document.authors[0].affiliation == "A1", lambda: Document.authors[0].any(Author.affiliation == "A1")),
        (lambda: Document.authors[0].name != "A, B", lambda: ~Document.authors[0].any(Author.name == "A, B")),
    ],
)
def test_attribute_shorthand(shorthand: Any, explicit: Any) -> None:
    assert shorthand() == explicit()


def test_values_are_checked_as_for_any_field() -> None:
    with pytest.raises(QueryError, match="needs a str"):
        Document.authors[0].name == 3  # noqa: B015
    with pytest.raises(QueryError, match="None"):
        Document.authors.any(Author.orcid == None)  # noqa: E711
