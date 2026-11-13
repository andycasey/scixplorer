"""Journals known to SciX, from ``journals.json``: the copy downloaded by
:func:`ads.data.refresh` if there is one, else the packaged snapshot."""

from __future__ import annotations

import functools
import json
from typing import ClassVar

from .. import data
from ..fields import StringField
from ..model import Model
from ..query import MemoryBackend

__all__ = ["Journal"]


def _parse(read: data.Reader) -> tuple[Journal, ...]:
    """Build every journal from ``journals.json``, a JSON object ``{abbreviation: title}``."""
    titles = json.loads(read("journals.json"))
    if not isinstance(titles, dict) or not all(isinstance(title, str) for title in titles.values()):
        raise ValueError("journals.json must be a JSON object mapping abbreviations to titles")
    return tuple(Journal(abbreviation=abbreviation, title=title) for abbreviation, title in titles.items())


@functools.lru_cache(maxsize=None)
def _load() -> tuple[Journal, ...]:
    """Every journal, read once (until :func:`_reset`)."""
    return data.load("journals", _parse)


def _reset() -> None:
    """Forget the loaded journals, so the next query reads the data again."""
    _load.cache_clear()
    Journal._backend.clear()


class Journal(Model):
    """A journal or other publication series, identified by its ADS abbreviation (bibstem).

    Journals are held in memory and queried locally::

        Journal.get(abbreviation="ApJ").title
        Journal.select().where(Journal.title.contains("astro")).order_by(Journal.title)
    """

    _key = "abbreviation"
    _backend: ClassVar[MemoryBackend[Journal]] = MemoryBackend(_load, key="abbreviation")

    abbreviation = StringField(
        "ADS abbreviation of the journal (its bibstem), e.g. 'ApJ'. This is the journal "
        "part of a bibcode, without the padding dots."
    )
    title = StringField("Full title of the journal, e.g. 'The Astrophysical Journal'.")

    @classmethod
    def lookup(cls, abbreviation: str) -> Journal | None:
        """The journal with this abbreviation, or ``None``. Constant time."""
        return cls._backend.lookup(abbreviation)

    def __str__(self) -> str:
        return self.abbreviation if self.abbreviation is not None else repr(self)


data.register("journals", _parse, _reset)
