"""Institutions known to SciX, from ``affiliations.tsv`` and ``affiliations_country.tsv``:
the copies downloaded by :func:`scixplorer.data.refresh` if there are any, else the
packaged snapshot.

Both files come unmodified from the ``adsabs/CanonicalAffiliations``
repository (GPL-3.0; see ``NOTICE.md`` in :mod:`scixplorer.data`).

``affiliations.tsv`` has one row per (parent, child) pair, with parent ``0``
meaning "no parent"; an affiliation listed under several parents is merged into
one :class:`Affiliation`. Countries come from ``affiliations_country.tsv``,
whose columns are found by heading: ``ISO Country, Country, Parent, Child,
...``. An older layout, ``Country, [Aff_id_parent], Aff_id_child, ...``, with
no ISO codes, is still read (a cache written by an earlier version may hold it).
"""

from __future__ import annotations

import csv
import dataclasses
import functools
from collections.abc import Iterator, Mapping
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

#: Accepted headings of the country file's columns, by meaning. Only ``code`` may be absent.
COUNTRY_COLUMNS = {
    "country": ("Country",),
    "code": ("ISO Country",),
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


@dataclasses.dataclass(frozen=True)
class _Country:
    """A country as one row of the country file gives it."""

    name: str
    code: str | None


def _fit(row: list[str], width: int) -> list[str] | None:
    """``row`` with exactly ``width`` fields, or ``None`` if that cannot be done without guessing.

    A short row is padded with empty fields. A long row loses surplus empty
    fields, but only if all its empty fields are adjacent, so that it does not
    matter which of them are dropped (upstream has a row with one tab too many
    between an empty parent and the child).
    """
    excess = len(row) - width
    if excess <= 0:
        return row + [""] * -excess
    empty = [i for i, value in enumerate(row) if not value]
    if len(empty) < excess or empty != list(range(empty[0], empty[0] + len(empty))):
        return None
    return row[: empty[0]] + row[empty[0] + excess :]


def _countries(text: str) -> dict[str, _Country]:
    """Map ``"parent|child"`` to country from the country file, finding its columns by heading.

    Rows that cannot be fitted to the header (see :func:`_fit`) are skipped.
    """
    header, rows = _read_tsv(text)
    index: dict[str, int] = {}
    for meaning, headings in COUNTRY_COLUMNS.items():
        found = [header.index(heading) for heading in headings if heading in header]
        if found:
            index[meaning] = found[0]
        elif meaning != "code":
            raise ValueError(f"affiliations_country.tsv has no {' or '.join(headings)} column: {header}")
    countries: dict[str, _Country] = {}
    for raw in rows:
        row = _fit(raw, len(header))
        if row is None:
            continue
        code = row[index["code"]] if "code" in index else ""
        countries[f"{row[index['parent']]}|{row[index['child']]}"] = _Country(row[index["country"]], code or None)
    return countries


def _country(countries: Mapping[str, _Country], parent: str, child: str) -> _Country | None:
    """The first country with a name found for one (parent, child) row.

    Keys are tried in the order of the previous implementation: the exact pair,
    then the child with no parent, then the parent with no child. The country
    file leaves the parent empty where ``affiliations.tsv`` has ``0``, so
    top-level affiliations resolve through the second key.
    """
    for key in (f"{parent}|{child}", f"|{child}", f"{parent}|"):
        country = countries.get(key)
        if country is not None and country.name:
            return country
    return None


@dataclasses.dataclass
class _Merged:
    """The rows of ``affiliations.tsv`` for one affiliation id, merged."""

    abbreviation: str
    canonical_name: str
    parent_ids: list[str] = dataclasses.field(default_factory=list)
    country: _Country | None = None


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
            country=entry.country.name if entry.country else None,
            country_code=entry.country.code if entry.country else None,
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

        affiliations = (
            Affiliation
            .select()
            .where(Affiliation.country_code == "AU")
            .order_by(Affiliation.abbreviation)
        )

    Countries are spelled out as the upstream data spells them ("United
    States", "United Kingdom"), and those spellings can change between
    releases of the data; ``country == "USA"`` simply matches nothing. Filter on
    :attr:`country_code` (ISO 3166-1 alpha-2, e.g. "US") for a stable condition.

    Institutions form a hierarchy (a department within a university): see
    :attr:`parents`, :attr:`children` and :attr:`siblings`.
    """

    _key = "id"
    _backend: ClassVar[MemoryBackend[Affiliation]] = MemoryBackend(_load, key="id")

    id = StringField("ADS canonical affiliation identifier, e.g. 'A00211'.")
    abbreviation = StringField("Short name of the institution, e.g. 'Harvard U'.")
    canonical_name = StringField("Full canonical name, e.g. 'Harvard University, Massachusetts'.")
    country = StringField(
        "Country of the institution, spelled out as the upstream data spells it (e.g. 'United States', "
        "'United Kingdom'), or None if not known. Spellings may change between data releases: filter on "
        "country_code for a stable condition."
    )
    country_code = StringField(
        "ISO 3166-1 alpha-2 code of the country as the upstream data gives it (e.g. 'US', 'GB', 'AU'), "
        "or None if not known (also with data in the older layout, which has no codes). The stable "
        "way to filter by country."
    )
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
