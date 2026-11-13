"""Authors of documents, built from the per-author arrays ADS stores on each document.

ADS holds what it knows about each author as index-aligned arrays on the
document: ``author[i]``, ``aff[i]``, ``aff_id[i]``, ``email[i]`` and the ORCID
arrays all describe author ``i``. ``Document.authors`` turns them into one
:class:`Author` per author, and lets a search say several things about the
*same* author::

    Document.authors.any((Author.name == "Casey, A") & (Author.affiliation.country == "Australia"))
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..fields import IntegerField, NameField, NumericExpr, StringField, TextExpr, TextField
from ..model import Collection, Model, RecordExpr, Relation, RelationExpr, Source
from .affiliation import Affiliation

__all__ = ["Author", "AuthorExpr", "document_authors"]


def _affiliations_of(author: Author) -> tuple[Affiliation, ...]:
    """The known affiliations among the author's ``aff_id`` (ids missing from the packaged data are skipped)."""
    found = (Affiliation.lookup(i) for i in author.aff_id or ())
    return tuple(affiliation for affiliation in found if affiliation is not None)


class Author(Model):
    """One author of a :class:`~ads.models.document.Document`, from ``doc.authors``.

    Values come from entry :attr:`position` of the document's per-author
    arrays; ``"-"`` (ADS's "none") and missing entries give ``None`` (an empty
    list for :attr:`aff_id`). Authors are not searched on their own: use their
    fields inside ``Document.authors.any(...)`` / ``.all(...)``, where
    everything is about one author.
    """

    position = IntegerField("The author's 0-based position in the document's author list.")
    name = NameField("The author's name, 'Lastname, First M' (from Document.author).")
    aff = TextField("The raw affiliation string as given (from Document.aff), or None.")
    aff_id = StringField(
        "Curated affiliation identifiers (from Document.aff_id, split on ';'); empty if there are none.",
        multivalued=True,
    )
    email = TextField("The author's email address (from Document.email), or None.")
    orcid_pub = TextField("ORCID supplied by the publisher (from Document.orcid_pub), or None.")
    orcid_user = TextField(
        "ORCID claimed by a user who gave ADS consent to expose their profile (from Document.orcid_user), or None."
    )
    orcid_other = TextField(
        "ORCID claimed with the ADS claiming interface (from Document.orcid_other), or None."
    )
    orcid = StringField(
        "The author's ORCID from any source: the first of orcid_pub, orcid_user and orcid_other "
        "that is given (searched with Document.orcid)."
    )

    affiliation: Relation[tuple[Affiliation, ...]] = Relation(
        Affiliation,
        key="aff_id",
        related_key="id",
        resolve=_affiliations_of,
        doc="The author's curated affiliations, from aff_id (possibly several; ids missing from the packaged data are skipped).",
    )

    def __repr__(self) -> str:
        return f"<Author {self.position}: {self.name!r}>"


class AuthorExpr(RecordExpr):
    """A handle on one author of a document, by position: ``Document.authors[0]``.

    ``Document.authors[0].name == x`` is ``Document.authors[0].any(Author.name == x)``.
    """

    __slots__ = ()

    if TYPE_CHECKING:
        position: NumericExpr
        name: TextExpr
        aff: TextExpr
        aff_id: TextExpr
        email: TextExpr
        orcid_pub: TextExpr
        orcid_user: TextExpr
        orcid_other: TextExpr
        orcid: TextExpr
        affiliation: RelationExpr


def document_authors() -> Collection[Author, AuthorExpr]:
    """The collection behind ``Document.authors``: one :class:`Author` per entry of ``Document.author``.

    Each author field is read from (and searched through) the document field of
    the same purpose; ``orcid`` reads the three ORCID arrays and is searched
    through the document's ``orcid`` field, which covers them all.
    """
    return Collection(
        Author,
        sources={
            "name": "author",
            "aff": "aff",
            "aff_id": "aff_id",
            "email": "email",
            "orcid_pub": "orcid_pub",
            "orcid_user": "orcid_user",
            "orcid_other": "orcid_other",
            "orcid": Source(("orcid_pub", "orcid_user", "orcid_other"), search="orcid"),
        },
        length="author",
        index="position",
        handle=AuthorExpr,
        doc=(
            "The authors, in order, as Author objects combining each author's name, affiliations, "
            "email and ORCID. Search with Document.authors.any(...) / .all(...) (each about one author) "
            "or by position: Document.authors[0].orcid == '...'."
        ),
    )
