"""Named features on planetary surfaces, from the IAU/USGS Gazetteer of Planetary Nomenclature.

The gazetteer is not packaged with this library: download it once with
``refresh("planetary_features")`` from :mod:`scixplorer.data`. It is read from
``planetary_features.csv``, the gazetteer's CSV export with the columns
``Feature ID, Clean Feature Name, Target, Feature Type, Approval Status,
Approval Date`` (headings may contain runs of spaces).

SciX stores the features named in a record in ``planetary_feature_id``
(integers, the gazetteer's feature ids; ``gpn_id`` is the deprecated name).
"""

from __future__ import annotations

import csv
import functools
import re
from typing import ClassVar

from .. import data
from ..fields import IntegerField, StringField, TextField
from ..model import Model
from ..query import MemoryBackend

__all__ = ["PlanetaryFeature"]

#: The columns read from the CSV export, by heading (whitespace collapsed).
COLUMNS = ("Feature ID", "Clean Feature Name", "Target", "Feature Type", "Approval Status", "Approval Date")

_YEAR = re.compile(r"\b(\d{4})$")


def _parse(read: data.Reader) -> tuple[PlanetaryFeature, ...]:
    """Build every feature from ``planetary_features.csv``.

    Any row with a non-integer or repeated feature id, or a missing name or
    target, makes the whole file invalid.
    """
    rows = csv.reader(read("planetary_features.csv").splitlines())
    header = [" ".join(heading.split()) for heading in next(rows, [])]
    missing = [heading for heading in COLUMNS if heading not in header]
    if missing:
        raise ValueError(f"planetary_features.csv has no {', '.join(missing)} column; its columns are {header}")
    index = [header.index(heading) for heading in COLUMNS]
    features = []
    seen: set[int] = set()
    for line, row in enumerate(rows, start=2):
        if not any(value.strip() for value in row):
            continue
        if len(row) < len(header):
            raise ValueError(f"line {line} has {len(row)} columns, not {len(header)}")
        feature_id, name, target, feature_type, status, approved = (row[i].strip() for i in index)
        if not feature_id.isdigit() or int(feature_id) in seen:
            raise ValueError(f"line {line}: feature id {feature_id!r} is not a new integer")
        if not name or not target:
            raise ValueError(f"line {line}: feature {feature_id} has no name or target")
        seen.add(int(feature_id))
        year = _YEAR.search(approved)
        features.append(
            PlanetaryFeature(
                id=int(feature_id),
                name=name,
                target=target,
                feature_type=feature_type.partition(",")[0].strip() or None,
                approval_status=status or None,
                approval_year=int(year.group(1)) if year else None,
            )
        )
    return tuple(features)


@functools.lru_cache(maxsize=None)
def _load() -> tuple[PlanetaryFeature, ...]:
    """Every feature, read once (until :func:`_reset`)."""
    return data.load("planetary_features", _parse)


def _reset() -> None:
    """Forget the loaded gazetteer, so the next query reads the data again."""
    _load.cache_clear()
    PlanetaryFeature._backend.clear()


class PlanetaryFeature(Model):
    """A named feature on a planet, moon or small body, identified by its gazetteer id (e.g. ``2071``, Gale crater).

    Features are held in memory and queried locally once the gazetteer has been
    downloaded (``refresh("planetary_features")`` in :mod:`scixplorer.data`)::

        PlanetaryFeature.select().where(
            (PlanetaryFeature.target == "Mars") & (PlanetaryFeature.feature_type == "Crater")
        ).order_by(PlanetaryFeature.name)

    ``name`` is matched like SciX text (``==`` finds the words as a phrase,
    ignoring case, so ``name == "Gale"`` also finds "Gale Dorsa"; use
    ``.exact("Gale")`` for the whole name). The other text fields are matched
    exactly, including case.
    """

    _key = "id"
    _backend: ClassVar[MemoryBackend[PlanetaryFeature]] = MemoryBackend(_load, key="id")

    id = IntegerField("Gazetteer feature id, e.g. 2071; SciX stores these in planetary_feature_id.")
    name = TextField("Name of the feature without diacritics (the gazetteer's 'clean' name), e.g. 'Gale'.")
    target = StringField("The body the feature is on, e.g. 'Mars' or 'Moon'.")
    feature_type = StringField(
        "Descriptor term, singular, e.g. 'Crater' or 'Mons' (the gazetteer's 'Crater, craters' "
        "without the plural), or a category such as 'Satellite Feature'."
    )
    approval_status = StringField("IAU status of the name: 'Approved', 'Dropped' or 'Not Approved'.")
    approval_year = IntegerField("Year the IAU approved the name, or None if not given.")

    @classmethod
    def lookup(cls, feature_id: int | str) -> PlanetaryFeature | None:
        """The feature with this id (an int, or a string of digits), or ``None``. Constant time."""
        if isinstance(feature_id, str):
            if not feature_id.isdigit():
                return None
            feature_id = int(feature_id)
        return cls._backend.lookup(feature_id)

    def __str__(self) -> str:
        return self.name or repr(self)

    def __repr__(self) -> str:
        if self.id is None:
            return super().__repr__()
        where = f" ({self.name}, {self.target})" if self.name and self.target else ""
        return f"<PlanetaryFeature: id={self.id}{where}>"


data.register("planetary_features", _parse, _reset)
