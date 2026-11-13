"""Concepts of the Unified Astronomy Thesaurus (UAT), a controlled vocabulary for astronomy.

The thesaurus is not packaged with this library (it is licensed CC BY-SA 3.0):
download it once with ``refresh("uat")`` from :mod:`scixplorer.data`. It is read from
``uat.json``, the flat list form published by the ``astrothesaurus/UAT``
repository (``UAT_list.json``).

SciX stores the UAT concepts of a record in ``uat_id`` (integers, the number at
the end of the concept URI) and their names in ``uat``.
"""

from __future__ import annotations

import functools
import json
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

from .. import data
from ..expr import NOTHING
from ..fields import BooleanField, IntegerField, StringField, TextField
from ..model import Model
from ..query import MemoryBackend, Query

__all__ = ["UATConcept"]

#: Prefix of every UAT concept URI; the concept number follows it.
URI_PREFIX = "http://astrothesaurus.org/uat/"


def _concept_id(uri: Any) -> int:
    """The concept number at the end of a UAT URI."""
    if not isinstance(uri, str) or not uri.startswith(URI_PREFIX) or not uri[len(URI_PREFIX) :].isdigit():
        raise ValueError(f"not a UAT concept URI: {uri!r}")
    return int(uri[len(URI_PREFIX) :])


def _strings(value: Any, what: str) -> list[str]:
    """A JSON list of strings (``null`` means none)."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{what} must be a list of strings, not {value!r}")
    return value


def _broader_ids(value: Any, what: str) -> list[int]:
    """Concept numbers from a JSON list of ``{"uri": ...}`` objects (``null`` means none)."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{what} must be a list of objects, not {value!r}")
    return list(dict.fromkeys(_concept_id(item.get("uri")) for item in value))


def _concept(item: Any) -> UATConcept:
    if not isinstance(item, dict):
        raise ValueError(f"a concept must be a JSON object, not {item!r}")
    concept_id = _concept_id(item.get("uri"))
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"concept {concept_id} has no name")
    return UATConcept(
        id=concept_id,
        name=name,
        alt_names=tuple(_strings(item.get("altNames"), f"altNames of concept {concept_id}")),
        parent_ids=tuple(_broader_ids(item.get("broader"), f"broader of concept {concept_id}")),
        uri=item["uri"],
        deprecated=item.get("status") == "deprecated",
    )


def _parse(read: data.Reader) -> tuple[UATConcept, ...]:
    """Build every concept from ``uat.json``, a JSON list of concept objects.

    Each object has ``uri``, ``name``, and optionally ``altNames`` (strings),
    ``broader`` (objects with a ``uri``) and ``status`` (``"deprecated"``).
    Any malformed concept makes the whole file invalid.
    """
    items = json.loads(read("uat.json"))
    if not isinstance(items, list):
        raise ValueError("uat.json must be a JSON list of concepts")
    concepts = tuple(_concept(item) for item in items)
    counts = Counter(concept.id for concept in concepts)
    repeated = [concept_id for concept_id, count in counts.items() if count > 1]
    if repeated:
        raise ValueError(f"concepts listed more than once: {repeated[:10]}")
    return concepts


@functools.lru_cache(maxsize=None)
def _load() -> tuple[UATConcept, ...]:
    """Every concept, read once (until :func:`_reset`)."""
    return data.load("uat", _parse)


@functools.lru_cache(maxsize=None)
def _children() -> dict[int, tuple[UATConcept, ...]]:
    """The narrower concepts of each concept number, in data-file order."""
    children: dict[int, list[UATConcept]] = {}
    for concept in _load():
        for parent_id in concept.parent_ids or ():
            children.setdefault(parent_id, []).append(concept)
    return {parent_id: tuple(found) for parent_id, found in children.items()}


def _reset() -> None:
    """Forget the loaded thesaurus, so the next query reads the data again."""
    _load.cache_clear()
    _children.cache_clear()
    UATConcept._backend.clear()


def _walk(start: UATConcept, step: Callable[[UATConcept], Iterable[UATConcept]]) -> list[UATConcept]:
    """Concepts reachable from ``start`` by repeating ``step``, breadth first, each once, excluding ``start``."""
    seen = {start.id}
    found: list[UATConcept] = []
    frontier = [start]
    while frontier:
        following: list[UATConcept] = []
        for concept in frontier:
            for reached in step(concept):
                if reached.id not in seen:
                    seen.add(reached.id)
                    found.append(reached)
                    following.append(reached)
        frontier = following
    return found


class UATConcept(Model):
    """A concept of the Unified Astronomy Thesaurus, identified by its number (e.g. ``486``).

    Concepts are held in memory and queried locally once the thesaurus has been
    downloaded (``refresh("uat")`` in :mod:`scixplorer.data`)::

        UATConcept.lookup(486).name
        UATConcept.select().where(UATConcept.name == "exoplanets").order_by(UATConcept.name)

    Concepts form a hierarchy in which a concept may have several broader
    concepts: see :attr:`parents`, :attr:`children`, :attr:`ancestors` and
    :attr:`descendants`. ``name`` and ``alt_names`` are matched like SciX text
    (``==`` finds the words as a phrase, ignoring case; use ``.exact()`` for
    the whole name).
    """

    _key = "id"
    _backend: ClassVar[MemoryBackend[UATConcept]] = MemoryBackend(_load, key="id")

    id = IntegerField("UAT concept number, e.g. 486; SciX stores these in uat_id.")
    name = TextField("Preferred name of the concept, e.g. 'Exoplanets'.")
    alt_names = TextField("Alternative names (synonyms) of the concept.", multivalued=True)
    parent_ids = IntegerField(
        "Numbers of the broader concepts, in data-file order (empty for a top-level concept).",
        multivalued=True,
    )
    uri = StringField("URI of the concept, e.g. 'http://astrothesaurus.org/uat/486'.")
    deprecated = BooleanField("Whether the concept has been withdrawn from the thesaurus.")

    @classmethod
    def lookup(cls, concept_id: int | str) -> UATConcept | None:
        """The concept with this number (an int, or a string of digits), or ``None``. Constant time."""
        if isinstance(concept_id, str):
            if not concept_id.isdigit():
                return None
            concept_id = int(concept_id)
        return cls._backend.lookup(concept_id)

    @property
    def parents(self) -> list[UATConcept]:
        """The broader concepts, in :attr:`parent_ids` order (unknown numbers skipped)."""
        found = (self.lookup(parent_id) for parent_id in self.parent_ids or ())
        return [parent for parent in found if parent is not None]

    @property
    def children(self) -> Query[UATConcept]:
        """A query for the narrower concepts (those listing this one as a parent)."""
        query = type(self).select()
        if self.id is None:
            return query.where(NOTHING)
        return query.where(UATConcept.parent_ids == self.id)

    @property
    def ancestors(self) -> list[UATConcept]:
        """Every broader concept, nearest first, each once; safe if the data has cycles."""
        return _walk(self, lambda concept: concept.parents)

    @property
    def descendants(self) -> list[UATConcept]:
        """Every narrower concept, nearest first, each once; safe if the data has cycles."""
        children = _children()
        return _walk(self, lambda concept: children.get(concept.id, ()) if concept.id is not None else ())

    def __str__(self) -> str:
        return self.name or repr(self)

    def __repr__(self) -> str:
        if self.id is None:
            return super().__repr__()
        name = f" ({self.name})" if self.name else ""
        return f"<UATConcept: id={self.id}{name}>"


data.register("uat", _parse, _reset)
