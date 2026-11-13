"""Institutions known to SciX, from ``affiliations.tsv`` and ``affiliations_country.tsv``:
the copies downloaded by :func:`ads.data.refresh` if there are any, else the
packaged snapshot.

``affiliations.tsv`` has one row per (parent, child) pair, with parent ``0``
meaning "no parent"; an affiliation listed under several parents is merged into
one :class:`Affiliation`. Countries come from ``affiliations_country.tsv``,
whose columns are found by name: the packaged snapshot has ``Country,
[Aff_id_parent], Aff_id_child, ...`` and the current upstream file has ``ISO
Country, Country, Parent, Child, ...`` (with country names such as "United
States" where the snapshot has "USA").
"""

from __future__ import annotations

import csv
import dataclasses
import functools
from collections.abc import Iterator
from typing import ClassVar

from .. import data
from ..expr import NOTHING
from ..fields import StringField
from ..model import Model
from ..query import MemoryBackend, Query

__all__ = ["Affiliation"]

#: Parent id used in ``affiliations.tsv`` for an affiliation without a parent.
NO_PARENT = "0"


#: Header of ``affiliations.tsv``.
AFFILIATION_COLUMNS = ["Parent", "Child", "Abbrev", "Canonical Name"]

#: Accepted headings of the country file's columns, by meaning.
COUNTRY_COLUMNS = {
    "country": ("Country",),
    "parent": ("Parent", "[Aff_id_parent]"),
    "child": ("Child", "Aff_id_child"),
}


def _read_tsv(text: str) -> tuple[list[str], Iterator[list[str]]]:
    """The header of a TSV file and an iterator over its non-blank data rows (fields stripped)."""
    rows = csv.reader(text.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = [value.strip() for value in next(rows, [])]

    def data_rows() -> Iterator[list[str]]:
        for row in rows:
            row = [value.strip() for value in row]
            if any(row):
                yield row

    return header, data_rows()


def _countries(text: str) -> dict[str, str]:
    """Map ``"parent|child"`` to country from the country file, finding its columns by heading."""
    header, rows = _read_tsv(text)
    index = {}
    for meaning, headings in COUNTRY_COLUMNS.items():
        found = [header.index(heading) for heading in headings if heading in header]
        if not found:
            raise ValueError(f"affiliations_country.tsv has no {' or '.join(headings)} column: {header}")
        index[meaning] = found[0]
    width = max(index.values()) + 1
    countries: dict[str, str] = {}
    for row in rows:
        row += [""] * (width - len(row))
        countries[f"{row[index['parent']]}|{row[index['child']]}"] = row[index["country"]]
    return countries


def _country(countries: dict[str, str], parent: str, child: str) -> str | None:
    """The first non-empty country found for one (parent, child) row.

    Keys are tried in the order of the previous implementation: the exact pair,
    then the child with no parent, then the parent with no child. The country
    file leaves the parent empty where ``affiliations.tsv`` has ``0``, so
    top-level affiliations resolve through the second key.
    """
    for key in (f"{parent}|{child}", f"|{child}", f"{parent}|"):
        country = countries.get(key)
        if country:
            return country
    return None


@dataclasses.dataclass
class _Merged:
    """The rows of ``affiliations.tsv`` for one affiliation id, merged."""

    abbreviation: str
    canonical_name: str
    parent_ids: list[str] = dataclasses.field(default_factory=list)
    country: str | None = None


def _parse(read: data.Reader) -> tuple[Affiliation, ...]:
    """Read and merge every affiliation from the two data files."""
    countries = _countries(read("affiliations_country.tsv"))

    header, rows = _read_tsv(read("affiliations.tsv"))
    if header != AFFILIATION_COLUMNS:
        raise ValueError(f"affiliations.tsv should have the columns {AFFILIATION_COLUMNS}, not {header}")
    merged: dict[str, _Merged] = {}
    for parent, child, abbreviation, canonical_name in rows:
        entry = merged.setdefault(child, _Merged(abbreviation, canonical_name))
        if parent and parent != NO_PARENT and parent not in entry.parent_ids:
            entry.parent_ids.append(parent)
        if entry.country is None:
            entry.country = _country(countries, parent, child)

    return tuple(
        Affiliation(
            id=child,
            abbreviation=entry.abbreviation,
            canonical_name=entry.canonical_name,
            country=entry.country,
            parent_ids=tuple(entry.parent_ids),
        )
        for child, entry in merged.items()
    )


@functools.lru_cache(maxsize=None)
def _load() -> tuple[Affiliation, ...]:
    """Every affiliation, read once (until :func:`_reset`)."""
    return data.load("affiliations", _parse)


def _reset() -> None:
    """Forget the loaded affiliations, so the next query reads the data again."""
    _load.cache_clear()
    Affiliation._backend.clear()


class Affiliation(Model):
    """An institution, identified by its ADS canonical affiliation id (e.g. ``'A00211'``).

    Affiliations are held in memory and queried locally::

        Affiliation.select().where(Affiliation.country == "Australia").order_by(Affiliation.abbreviation)

    Institutions form a hierarchy (a department within a university): see
    :attr:`parents`, :attr:`children` and :attr:`siblings`.
    """

    _key = "id"
    _backend: ClassVar[MemoryBackend[Affiliation]] = MemoryBackend(_load, key="id")

    id = StringField("ADS canonical affiliation identifier, e.g. 'A00211'.")
    abbreviation = StringField("Short name of the institution, e.g. 'Harvard U'.")
    canonical_name = StringField("Full canonical name, e.g. 'Harvard University, Massachusetts'.")
    country = StringField("Country of the institution, or None if it is not known.")
    parent_ids = StringField(
        "Identifiers of the parent institutions, in data-file order (empty for a top-level one).",
        multivalued=True,
    )

    @classmethod
    def lookup(cls, affiliation_id: str) -> Affiliation | None:
        """The affiliation with this id, or ``None``. Constant time."""
        return cls._backend.lookup(affiliation_id)

    @property
    def parents(self) -> list[Affiliation]:
        """The parent institutions, in :attr:`parent_ids` order (unknown ids skipped)."""
        found = (self.lookup(parent_id) for parent_id in self.parent_ids or ())
        return [parent for parent in found if parent is not None]

    @property
    def parent(self) -> Affiliation | None:
        """The first parent institution, or ``None``."""
        parents = self.parents
        return parents[0] if parents else None

    @property
    def children(self) -> Query[Affiliation]:
        """A query for the institutions that list this one as a parent."""
        query = type(self).select()
        if self.id is None:
            return query.where(NOTHING)
        return query.where(Affiliation.parent_ids == self.id)

    @property
    def siblings(self) -> Query[Affiliation]:
        """A query for the other institutions sharing at least one parent with this one."""
        query = type(self).select()
        if not self.parent_ids:
            return query.where(NOTHING)
        siblings = Affiliation.parent_ids.in_(self.parent_ids)
        if self.id is not None:
            siblings &= Affiliation.id != self.id
        return query.where(siblings)

    def __str__(self) -> str:
        return self.canonical_name or self.abbreviation or self.id or repr(self)

    def __repr__(self) -> str:
        if self.id is None:
            return super().__repr__()
        name = f" ({self.abbreviation})" if self.abbreviation else ""
        return f"<Affiliation: id={self.id!r}{name}>"


data.register("affiliations", _parse, _reset)
