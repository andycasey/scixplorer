"""Static type expectations for Document.authors and Author.

This module is never run, only type-checked (see ``tests/test_typing_authors.py``).
Each ``assert_type`` states a type the checker must infer. Each line marked
``# type: ignore[code]`` must NOT type-check: with ``warn_unused_ignores`` on,
a missing error fails the check.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from scixplorer.expr import Expr
from scixplorer.fields import NumericExpr, TextExpr
from scixplorer.model import CollectionExpr, RelationExpr
from scixplorer.models.affiliation import Affiliation
from scixplorer.models.author import Author, AuthorExpr
from scixplorer.models.document import Document

if TYPE_CHECKING:
    if sys.version_info >= (3, 11):
        from typing import assert_type
    else:
        from typing_extensions import assert_type


def instances(doc: Document) -> None:
    assert_type(doc.authors, list[Author])
    author = doc.authors[0]
    assert_type(author.name, str | None)
    assert_type(author.position, int | None)
    assert_type(author.aff, str | None)
    assert_type(author.aff_id, list[str] | None)
    assert_type(author.email, str | None)
    assert_type(author.orcid, str | None)
    assert_type(author.orcid_pub, str | None)
    assert_type(author.affiliation, tuple[Affiliation, ...])


def handles() -> None:
    assert_type(Document.authors, CollectionExpr[AuthorExpr])
    assert_type(Document.authors[0], AuthorExpr)
    assert_type(Document.authors[0:3], CollectionExpr[AuthorExpr])
    assert_type(Document.authors[0].name, TextExpr)
    assert_type(Document.authors[0].position, NumericExpr)
    assert_type(Document.authors[0].affiliation, RelationExpr)
    assert_type(Author.name, TextExpr)
    assert_type(Author.affiliation, RelationExpr)


def expressions() -> None:
    assert_type(Document.authors.any(Author.affiliation.country == "New Zealand"), Expr)
    assert_type(Document.authors.all(Author.email.like("*@monash.edu")), Expr)
    assert_type(Document.authors[0].name == "x", Expr)
    assert_type(Document.authors[0].orcid.exact("x"), Expr)
    assert_type(Document.authors[0].affiliation.country == "Chile", Expr)
    assert_type(Document.authors[0].any(Author.name == "x"), Expr)
    assert_type(Document.authors[0:3].any((Author.name == "x") & (Author.aff == "y")), Expr)
    assert_type(Document.authors.any(Author.name == "x") & (Document.year == 2020), Expr)
    assert_type(Author.affiliation.any(Affiliation.country == "Chile"), Expr)
    assert_type(Document.authors[0].affiliation.any(Affiliation.country == "Chile"), Expr)


def must_not_type_check(doc: Document) -> None:
    Document.authors.name  # type: ignore[attr-defined]  # noqa: B018
    Document.authors[0:3].orcid  # type: ignore[attr-defined]  # noqa: B018
    Document.authors[0].nmae  # type: ignore[attr-defined]  # noqa: B018
    Document.authors["x"]  # type: ignore[call-overload]  # noqa: B018
    Document.authors.any("Casey, A")  # type: ignore[arg-type]
    Document.author.any(Document.author == "x")  # type: ignore[attr-defined]
    doc.authors[0].name.upper()  # type: ignore[union-attr]
