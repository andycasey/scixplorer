"""What the ``scix`` command shows: documents as text, sized and styled for a terminal."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from ..models.document import Document
from ..models.journal import Journal
from ..utils import parse_bibcode
from .interpret import Interpretation

__all__ = [
    "FIELDS",
    "SORTS",
    "Style",
    "authors_line",
    "explanation",
    "fit",
    "listing",
    "page_url",
    "pdf_url",
    "reference",
    "short_authors",
    "title_of",
    "wrap",
]

#: The fields the command fetches for every document.
FIELDS = (
    "bibcode",
    "title",
    "author",
    "year",
    "pub",
    "volume",
    "page",
    "doctype",
    "citation_count",
    "abstract",
    "doi",
    "identifier",
    "property",
    "esources",
)

#: The orders ``--sort`` accepts: name -> (field, descending, how to say it).
SORTS: dict[str, tuple[str, bool, str]] = {
    "date": ("date", True, "newest first"),
    "citations": ("citation_count", True, "most cited first"),
    "reads": ("read_count", True, "most read first"),
    "relevance": ("score", True, "most relevant first"),
    "oldest": ("date", False, "oldest first"),
    "author": ("first_author", False, "by first author"),
}

#: Where a document's page is.
SITE = "https://scixplorer.org"

#: The full-text sources ``pdf_url`` opens, best first.
_PDF_SOURCES = ("PUB_PDF", "EPRINT_PDF", "ADS_PDF", "AUTHOR_PDF")

_TAG = re.compile(r"</?[A-Za-z][^>]*>")


@dataclass(frozen=True)
class Style:
    """Text styles, as ANSI escape sequences when ``enabled`` and as nothing otherwise."""

    enabled: bool = False

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled and text else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def accent(self, text: str) -> str:
        return self._wrap("36", text)

    def warn(self, text: str) -> str:
        return self._wrap("33", text)

    def selected(self, text: str) -> str:
        return self._wrap("7", text)


# Text that fits.


def _width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def width_of(text: str) -> int:
    """How many terminal columns ``text`` takes."""
    return sum(_width(character) for character in text)


def fit(text: str, width: int, *, pad: bool = False, right: bool = False) -> str:
    """``text`` cut to ``width`` columns (ending in ``…`` if cut), and padded to it if ``pad``."""
    if width <= 0:
        return ""
    if width_of(text) > width:
        kept, used = [], 0
        for character in text:
            step = _width(character)
            if used + step > width - 1:
                break
            kept.append(character)
            used += step
        text = "".join(kept).rstrip() + "…"
    if not pad:
        return text
    space = " " * (width - width_of(text))
    return space + text if right else text + space


def wrap(text: str, width: int) -> list[str]:
    """``text`` as lines of at most ``width`` columns, broken at spaces."""
    width = max(width, 8)
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            while width_of(word) > width:  # a word longer than a line: cut it
                head = fit(word, width + 1)[:-1]
                if line:
                    lines.append(line)
                    line = ""
                lines.append(head)
                word = word[len(head) :]
            if not line:
                line = word
            elif width_of(line) + 1 + width_of(word) <= width:
                line += " " + word
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _clean(text: str) -> str:
    """Text from SciX without the markup some records carry (``<SUB>``, ``&amp;``)."""
    return " ".join(html.unescape(_TAG.sub("", text)).split())


# Parts of a document.


def title_of(document: Document) -> str:
    titles = document._data.get("title") or []
    return _clean(titles[0]) if titles else "(no title)"


def abstract_of(document: Document) -> str:
    return _clean(document._data.get("abstract") or "")


def _surname(author: str) -> str:
    return author.split(",")[0].strip()


def short_authors(document: Document) -> str:
    """The authors as a citation names them: ``Casey``, ``Casey & Ji``, ``Casey et al.``."""
    authors = document._data.get("author") or []
    if not authors:
        return ""
    surnames = [_surname(author) for author in authors[:3]]
    if len(authors) == 1:
        return surnames[0]
    if len(authors) == 2:
        return f"{surnames[0]} & {surnames[1]}"
    return f"{surnames[0]} et al."


def authors_line(document: Document, limit: int = 8) -> str:
    """The first ``limit`` authors in full, and how many more there are."""
    authors = document._data.get("author") or []
    shown = "; ".join(authors[:limit])
    more = len(authors) - limit
    return f"{shown}; and {more} more" if more > 0 else shown


def reference(document: Document) -> str:
    """Where the document appeared: ``ApJ 880, 125 (2019)``."""
    data = document._data
    journal = ""
    try:
        journal = parse_bibcode(data.get("bibcode") or "")["journal_abbreviation"]
    except ValueError:
        pass
    if Journal.lookup(journal) is None:  # the journal part of a preprint's bibcode is not an abbreviation
        journal = ""
    parts = [journal or data.get("pub") or ""]
    if data.get("volume"):
        parts.append(str(data["volume"]))
    text = " ".join(part for part in parts if part)
    pages = data.get("page") or []
    if pages and data.get("volume"):
        text += f", {pages[0]}"
    if data.get("year"):
        text += f" ({data['year']})"
    return text.strip()


def citations(document: Document) -> str:
    count = document._data.get("citation_count") or 0
    return f"{count} citation{'' if count == 1 else 's'}"


def page_url(document: Document) -> str | None:
    """The document's page on SciX."""
    bibcode = document._data.get("bibcode")
    return f"{SITE}/abs/{quote(bibcode, safe='&')}/abstract" if bibcode else None


def pdf_url(document: Document) -> str | None:
    """A link to the full text, the publisher's if there is one; ``None`` if SciX lists none."""
    bibcode = document._data.get("bibcode")
    sources = document._data.get("esources") or []
    for source in _PDF_SOURCES:
        if bibcode and source in sources:
            return f"{SITE}/link_gateway/{quote(bibcode, safe='&')}/{source}"
    return None


def facts(document: Document) -> str:
    """One line about a document: where it appeared, how often it is cited, its bibcode."""
    data = document._data
    parts = [reference(document), citations(document)]
    if "refereed" in (each.lower() for each in data.get("property") or []):
        parts.append("refereed")
    elif data.get("doctype"):
        parts.append(str(data["doctype"]))
    parts.append(data.get("bibcode") or "")
    return " · ".join(part for part in parts if part)


# Whole outputs.


def found_line(total: int, shown: int, sort: str) -> str:
    """``498 documents, newest first; showing 10``."""
    noun = "document" if total == 1 else "documents"
    line = f"{total} {noun}"
    if total > 1:
        line += f", {SORTS[sort][2]}"
    if shown < total:
        line += f"; showing {shown}"
    return line


def listing(documents: Sequence[Document], *, width: int, style: Style, start: int = 1) -> list[str]:
    """The documents as a numbered list, three lines each."""
    lines: list[str] = []
    gutter = len(str(start + len(documents) - 1)) + 2
    indent = " " * gutter
    for number, document in enumerate(documents, start):
        if lines:
            lines.append("")
        title = wrap(title_of(document), width - gutter)
        lines.append(style.dim(f"{number:>{gutter - 2}}  ") + style.bold(title[0]))
        lines.extend(indent + style.bold(line) for line in title[1:])
        who = " · ".join(part for part in (short_authors(document), reference(document), citations(document)) if part)
        lines.append(indent + fit(who, width - gutter))
        lines.append(indent + style.accent(document._data.get("bibcode") or ""))
    return lines


def explanation(interpretation: Interpretation, q: str, sort: str, style: Style) -> list[str]:
    """How a search was read, piece by piece, and the query it becomes."""
    lines: list[str] = []
    readings = interpretation.readings
    typed = max((width_of(reading.source) for reading in readings), default=0)
    kinds = max((len(reading.kind) for reading in readings), default=0)
    for reading in readings:
        lines.append(
            f"{fit(reading.source, typed, pad=True)}  {style.accent(fit(reading.kind, kinds, pad=True))}  {reading.value}"
        )
    if readings:
        lines.append("")
    lines.append(f"{style.dim('query')}  {q}")
    lines.append(f"{style.dim('sort ')}  {SORTS[sort][2]}")
    lines.extend(style.warn(note) for note in interpretation.notes)
    return lines
