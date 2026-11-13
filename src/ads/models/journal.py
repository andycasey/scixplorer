"""Journals known to ADS, from the packaged ``data/journals.json``."""

from __future__ import annotations

import functools
import json
from importlib import resources
from typing import ClassVar

from ..fields import StringField
from ..model import Model
from ..query import MemoryBackend

__all__ = ["Journal"]


@functools.lru_cache(maxsize=None)
def _load() -> tuple[Journal, ...]:
    """Read every journal from the packaged data file (once)."""
    text = (resources.files("ads") / "data" / "journals.json").read_text(encoding="utf-8")
    return tuple(Journal(abbreviation=abbreviation, title=title) for abbreviation, title in json.loads(text).items())


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
