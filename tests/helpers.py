"""Throwaway models shared by the tests (the real Document/Journal come later)."""

from __future__ import annotations

from typing import Any

from scixplorer.fields import (
    BooleanField,
    DateField,
    DateTimeField,
    FloatField,
    IntegerField,
    NameField,
    StringField,
    TextField,
    VirtualField,
)
from scixplorer.model import Model, Relation


class Journal(Model):
    _key = "abbreviation"
    abbreviation = StringField("Abbreviation")
    title = StringField("Title")


class Institute(Model):
    _key = "id"
    id = StringField("Identifier")
    country = StringField("Country")


JOURNALS = {
    "ApJ": Journal(abbreviation="ApJ", title="The Astrophysical Journal"),
    "MNRAS": Journal(abbreviation="MNRAS", title="Monthly Notices of the Royal Astronomical Society"),
}

INSTITUTES = {
    "A1": Institute(id="A1", country="Australia"),
    "A2": Institute(id="A2", country="Germany"),
    "A3": Institute(id="A3", country=None),
}


def resolve_journal(paper: Paper) -> Journal | None:
    """The journal named by the bibcode's journal part."""
    bibcode = paper.bibcode
    return None if bibcode is None else JOURNALS.get(bibcode[4:9].strip("."))


def resolve_institutes(paper: Paper) -> list[Any] | None:
    """One entry per author: None for '-', an Institute, or a tuple for 'A1; A2'."""
    if paper.aff_id is None:
        return None
    result: list[Any] = []
    for entry in paper.aff_id:
        ids = [part.strip() for part in entry.split(";")]
        found = tuple(INSTITUTES.get(i) for i in ids if i != "-")
        result.append(None if not found else found[0] if len(found) == 1 else found)
    return result


class Paper(Model):
    _key = "bibcode"
    bibcode = StringField("Bibcode")
    title = TextField("Title", multivalued=True)
    author = NameField("Authors", multivalued=True)
    year = IntegerField("Year")
    citation_count_norm = FloatField("Normalised citations")
    entry_date = DateTimeField("Entry date")
    pubdate = DateField("Publication date")
    public = BooleanField("Public")
    full = VirtualField("Full text")
    bibstem = StringField("Bibstem", multivalued=True)
    aff_id = StringField("Affiliation ids", multivalued=True)
    citation_count = IntegerField("Citations", solr_name="citation_count")
    doctype = StringField("Type", stored=False)
    journal = Relation(Journal, key="bibstem", related_key="abbreviation", resolve=resolve_journal)
    institute = Relation(lambda: Institute, key="aff_id", related_key="id", resolve=resolve_institutes)
