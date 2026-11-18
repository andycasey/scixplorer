"""Read a search typed in plain words the way a person means it.

:func:`interpret` turns ``Kewley 2008``, ``exoplanets 2026`` or ``ApJ 608 119``
into conditions on :class:`~scixplorer.models.document.Document`. It makes no
request: it reads the words against patterns (years, bibcodes, DOIs, arXiv and
ORCID identifiers), the journal list and, once downloaded, the Unified Astronomy
Thesaurus. Each piece of the search becomes one :class:`Reading`, so that the
command can show how every word was understood.

The rules, in the order they are tried on each word:

1. ``field:value`` is a condition on that field (``author:``, ``title:``,
   ``year:``, any document field, and short forms such as ``a:`` and ``j:``);
   ``citations:``, ``references:``, ``trending:``, ``useful:``, ``reviews:``
   and ``similar:`` apply that operator to the interpreted value.
2. A quoted phrase is ``"Surname, Initials"``, a journal title, or else a
   phrase to find in the title, abstract and keywords.
3. A bibcode, DOI, arXiv identifier, ORCID iD, or a link to one.
4. A year (``2005``), a range (``2005-2010``, ``2015-``), or ``since 2020``.
5. A journal, by abbreviation or title, and the volume and page after it.
6. ``Surname, Initials`` or ``Initials Surname``: an author.
7. The longest run of words that names a UAT concept.
8. A capitalised word: an author's surname.
9. Anything else: a word to find in the title, abstract and keywords. A word
   in lower case that is not in the thesaurus may also be an author's surname,
   so it matches either.

A search that already looks like Solr syntax (``AND``, ``[2000 TO 2010]``) is
sent as it is.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any
from urllib.parse import unquote

from ..exceptions import DataNotAvailable, QueryWarning, SciXError
from ..expr import Expr
from ..fields import FloatField, IntegerField
from ..models.document import Document
from ..models.journal import Journal
from ..models.uat import UATConcept
from ..utils import is_valid_orcid, parse_bibcode

__all__ = ["Interpretation", "Reading", "interpret"]

#: The earliest year a four-digit number is read as.
FIRST_YEAR = 1500

#: Words that say nothing about what to search for.
STOPWORDS = frozenset(
    "a an and by et for from in of on or the to with".split()
)

#: Lower-case words that begin a surname, as in "van der Marel".
PARTICLES = frozenset("da das de del della den der di dos du la le ten ter van von".split())

#: Names people use for journals that the journal list does not have.
JOURNAL_ALIASES = {
    "A+A": "A&A",
    "AA": "A&A",
    "AandA": "A&A",
    "ApJL": "ApJL",
    "ApJLett": "ApJL",
    "Icarus": "Icar",
    "Nature": "Natur",
    "PRD": "PhRvD",
    "PRL": "PhRvL",
    "Science": "Sci",
    "apjl": "ApJL",
}

#: Short forms of the fields a search names most.
FIELD_ALIASES = {
    "a": "author",
    "au": "author",
    "fa": "first_author",
    "first": "first_author",
    "t": "title",
    "k": "keyword",
    "kw": "keyword",
    "j": "journal",
    "pub": "journal",
    "y": "year",
    "yr": "year",
    "v": "volume",
    "vol": "volume",
    "p": "page",
    "pp": "page",
    "concept": "uat",
    "cites": "citations",
    "refs": "references",
}

#: Prefixes that apply a second-order operator to the interpreted value.
OPERATORS = ("citations", "references", "trending", "useful", "reviews", "similar")

_KINDS = {"first_author": "first author", "uat": "concept", "abs": "text"}

_SOLR_SYNTAX = re.compile(r"\b(?:AND|OR|NOT)\b|\[[^\]]*\bTO\b[^\]]*\]|\w\(|[*?]\w|\w[*?]")
_TOKEN = re.compile(r'(?:(?P<prefix>[A-Za-z_]+):)?"(?P<quoted>[^"]*)"(?P<sep>[,;]?)|(?P<word>\S+)')
_PREFIXED = re.compile(r"(?P<prefix>[A-Za-z_]+):(?P<value>.+)", re.DOTALL)
_INITIALS = re.compile(r"(?:[A-Z]\.?-?){1,4}")
_LEADING_INITIALS = re.compile(r"(?:[A-Z]\.-?){1,4}|[A-Z]")
_AUTHOR_PHRASE = re.compile(r"[^\W\d_][\w'’ -]*,\s*[^\W\d_][\w.'’ -]*")
_YEAR_RANGE = re.compile(r"(\d{4})(?:-|–|—|\.\.)(\d{4})")
_YEAR_FROM = re.compile(r"(\d{4})(?:-|–|\+|\.\.)")
_YEAR_TO = re.compile(r"(?:-|–|\.\.)(\d{4})")
_PAGE = re.compile(r"[A-Za-z]?\d+[A-Za-z]?")
_DOI = re.compile(r"10\.\d{4,9}/\S+")
_ARXIV = re.compile(r"(?:arxiv:)?(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.IGNORECASE)
_ORCID = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dXx]")
_RECORD_URL = re.compile(r"https?://(?:ui\.adsabs\.harvard\.edu|(?:www\.)?scixplorer\.org)/abs/([^/?#]+)")
_DOI_URL = re.compile(r"https?://(?:dx\.)?doi\.org/(10\..+)")
_ARXIV_URL = re.compile(r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/(.+?)(?:\.pdf)?")
_ORCID_URL = re.compile(r"https?://orcid\.org/(.+)")
_YEAR_WORDS = {"since": ">=", "from": ">=", "after": ">", "before": "<", "until": "<="}


@dataclass(frozen=True)
class Reading:
    """How one piece of a search was understood.

    ``kind`` says what it was read as (``"author"``, ``"year"``,
    ``"journal"``, ``"concept"``, ``"text"``, ...), ``source`` is what was
    typed, ``value`` is what it means in words, and ``expr`` is the condition.
    ``loose``, if there is one, is a wider condition to fall back on when the
    search finds nothing: a concept also as words, a surname also as a word.
    """

    kind: str
    source: str
    value: str
    expr: Expr
    loose: Expr | None = None

    def __str__(self) -> str:
        return f"{self.kind} {self.value}"


@dataclass(frozen=True)
class Interpretation:
    """A search as it was understood: one :class:`Reading` per piece, all of which must hold.

    ``notes`` say what could have been read better, for example that the
    thesaurus has not been downloaded.
    """

    text: str
    readings: tuple[Reading, ...]
    notes: tuple[str, ...] = ()

    @property
    def expr(self) -> Expr | None:
        """Every reading's condition, AND-ed; ``None`` if the search was empty."""
        combined: Expr | None = None
        for reading in self.readings:
            combined = reading.expr if combined is None else combined & reading.expr
        return combined

    def relaxed(self) -> Interpretation | None:
        """The same search with every reading that has a wider condition using it; ``None`` if none has."""
        if not any(reading.loose is not None for reading in self.readings):
            return None
        return replace(self, readings=tuple(_loosened(reading) for reading in self.readings))

    def __str__(self) -> str:
        return " · ".join(str(reading) for reading in self.readings)


def _loosened(reading: Reading) -> Reading:
    if reading.loose is None:
        return reading
    return Reading(f"{reading.kind} or text", reading.source, reading.value, reading.loose)


@dataclass(frozen=True)
class _Token:
    text: str
    quoted: bool = False
    comma: bool = False
    prefix: str | None = None
    first: bool = False


def interpret(query: str | Sequence[str], *, today: date | None = None) -> Interpretation:
    """Read a search typed in plain words; see the module documentation for the rules.

    :param query: the search as one string, or as the words of a command line.
        In a string, double quotes make a phrase. Of several command-line
        words, one that holds spaces was quoted in the shell, and is a phrase.
    :param today: the date that decides which numbers can be years (default: today).
    """
    text = query.strip() if isinstance(query, str) else " ".join(query).strip()
    if _SOLR_SYNTAX.search(_unquoted(text)):
        return Interpretation(text, (Reading("query", text, text, Document.raw(text)),))
    parser = _Parser(_tokens(query), today or date.today())
    parser.run()
    return Interpretation(text, tuple(parser.readings), tuple(dict.fromkeys(parser.notes)))


def _unquoted(text: str) -> str:
    """``text`` without its double-quoted phrases."""
    return re.sub(r'"[^"]*"', '""', text)


# Tokens.


def _known_prefix(prefix: str) -> str | None:
    """The field or operator a ``prefix:`` names, or ``None`` if it names none."""
    name = prefix.lower()
    name = FIELD_ALIASES.get(name, name)
    if name in OPERATORS or name in ("journal", "uat") or name in Document._fields:
        return name
    return None


def _tokens(query: str | Sequence[str]) -> list[_Token]:
    if isinstance(query, str):
        return _split(query)
    words = [word for word in query if word.strip()]
    if len(words) == 1:
        return _split(words[0])
    tokens: list[_Token] = []
    for word in words:
        if not re.search(r"\s", word.strip()):
            tokens.extend(_split(word))
            continue
        # A command-line word holding spaces was quoted in the shell.
        prefix, value = None, word.strip()
        match = _PREFIXED.fullmatch(value)
        if match is not None and _known_prefix(match["prefix"]) is not None:
            prefix, value = _known_prefix(match["prefix"]), match["value"].strip()
        tokens.append(_Token(value.strip('"'), quoted=True, prefix=prefix))
    return tokens


def _split(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in _TOKEN.finditer(text):
        if match["word"] is None:
            prefix = _known_prefix(match["prefix"]) if match["prefix"] else None
            if match["quoted"].strip():
                tokens.append(_Token(match["quoted"].strip(), quoted=True, comma=bool(match["sep"]), prefix=prefix))
            continue
        token = _word_token(match["word"])
        if token is not None:
            tokens.append(token)
    return tokens


def _word_token(word: str) -> _Token | None:
    """One unquoted word, without the punctuation of the sentence or citation around it."""
    comma = False
    while word and word[-1] in ",;":
        comma, word = True, word[:-1]
    if word.startswith("(") and word.endswith(")"):
        word = word[1:-1]
    elif word.startswith(("(", "[")) and not any(c in word for c in ")]"):
        word = word[1:]
    elif word.endswith((")", "]")) and not any(c in word for c in "(["):
        word = word[:-1]
    first = word.startswith("^") and len(word) > 1
    if first:
        word = word[1:]
    if word.endswith(".") and not _INITIALS.fullmatch(word) and not _DOI.fullmatch(word):
        word = word.rstrip(".")
    if not word:
        return None
    prefix = None
    match = _PREFIXED.fullmatch(word)
    if match is not None and not match["value"].startswith("//"):
        prefix = _known_prefix(match["prefix"])
        if prefix is not None:
            word = match["value"].strip('"')
    return _Token(word, comma=comma, prefix=prefix, first=first) if word else None


# Reference data.


def _fold(text: str) -> str:
    """Lower case, letters and digits only: ``ARA&A`` and ``araa`` fold to the same key."""
    return "".join(c for c in text.lower() if c.isalnum())


def _distinctive(abbreviation: str) -> bool:
    """Whether an abbreviation is unlikely to be an ordinary word or name (``ApJ``, not ``Icar``)."""
    return len(abbreviation) >= 3 and re.fullmatch(r"[A-Z]?[a-z]+", abbreviation) is None


class _Journals:
    """The journal list, indexed by abbreviation, by folded abbreviation and by title."""

    def __init__(self, journals: Sequence[Journal]) -> None:
        self.by_abbreviation: dict[str, Journal] = {}
        self.by_title: dict[str, Journal] = {}
        folded: dict[str, list[Journal]] = {}
        for journal in journals:
            abbreviation, title = journal.abbreviation, journal.title
            if not abbreviation:
                continue
            self.by_abbreviation.setdefault(abbreviation, journal)
            if _distinctive(abbreviation):
                folded.setdefault(_fold(abbreviation), []).append(journal)
            if title:
                key = " ".join(title.lower().split())
                self.by_title.setdefault(key, journal)
                self.by_title.setdefault(key.removeprefix("the "), journal)
        self.folded = {key: found[0] for key, found in folded.items() if len(found) == 1}

    def named(self, text: str) -> tuple[str, str] | None:
        """``(abbreviation, title)`` of the journal ``text`` names outright, in any spelling."""
        text = text.strip()
        alias = JOURNAL_ALIASES.get(text)
        if alias is not None:
            journal = self.by_abbreviation.get(alias)
            return alias, (journal.title or "") if journal is not None else ""
        journal = (
            self.by_abbreviation.get(text)
            or self.folded.get(_fold(text))
            or self.by_title.get(" ".join(text.lower().split()))
        )
        return None if journal is None else (journal.abbreviation or text, journal.title or "")


class _Thesaurus:
    """UAT concepts by name, and the words their names are made of."""

    def __init__(self, concepts: Sequence[UATConcept]) -> None:
        self.exact: dict[str, UATConcept] = {}
        self.singular: dict[str, UATConcept] = {}
        self.words: set[str] = set()
        ranked = sorted(concepts, key=lambda concept: bool(concept.deprecated))
        # Preferred names first, so that a name one concept prefers is never taken by another's synonym.
        for preferred in (True, False):
            for concept in ranked:
                for name in [concept.name] if preferred else list(concept.alt_names or ()):
                    if not name:
                        continue
                    key = " ".join(name.lower().split())
                    self.exact.setdefault(key, concept)
                    self.singular.setdefault(_singular(key), concept)
                    for word in re.findall(r"[^\W\d_]+", key):
                        self.words.update((word, _singular(word)))

    def named(self, text: str) -> UATConcept | None:
        """The concept with this name, ignoring case and a plural ending."""
        key = " ".join(text.lower().split())
        return self.exact.get(key) or self.singular.get(_singular(key))


def _singular(text: str) -> str:
    """``text`` with a plural ending removed from its last word: enough to match ``black hole(s)``."""
    if text.endswith("ies") and len(text) > 4:
        return text[:-3] + "y"
    if text.endswith("s") and not text.endswith(("ss", "us", "is")) and len(text) > 3:
        return text[:-1]
    return text


_cache: dict[str, tuple[Any, Any]] = {}


def _cached(name: str, rows: Any, build: Callable[[Any], Any]) -> Any:
    """``build(rows)``, rebuilt only when the data behind ``rows`` has been reloaded."""
    held = _cache.get(name)
    if held is None or held[0] is not rows:
        held = _cache[name] = (rows, build(rows))
    return held[1]


def _journals() -> _Journals:
    # The rows come from the model's own backend, so they are the ones its queries see.
    journals: _Journals = _cached("journals", Journal._backend._loader(), _Journals)
    return journals


def _thesaurus() -> _Thesaurus | None:
    """The thesaurus, or ``None`` until it has been downloaded."""
    try:
        rows = UATConcept._backend._loader()
    except DataNotAvailable:
        return None
    thesaurus: _Thesaurus = _cached("uat", rows, _Thesaurus)
    return thesaurus


# Small readers.


def _is_name(text: str) -> bool:
    """Whether ``text`` looks like a surname: capitalised, with lower-case letters, and nothing but letters."""
    return (
        text[:1].isupper()
        and any(c.islower() for c in text)
        and all(c.isalpha() or c in "-'’" for c in text)
        and text[-1].isalpha()
    )


def _either(word: str) -> Expr:
    return (Document.author == word) | (Document.abs == word)


def _identifier(text: str) -> Reading | None:
    """A bibcode, DOI, arXiv identifier or ORCID iD, or a link to one."""
    for pattern, read in ((_DOI_URL, _doi), (_ARXIV_URL, _arxiv), (_ORCID_URL, _orcid)):
        match = pattern.fullmatch(text)
        if match is not None:
            return read(text, unquote(match[1]).strip("/"))
    match = _RECORD_URL.match(text)
    if match is not None:
        identifier = unquote(match[1])
        return _bibcode(text, identifier) or Reading(
            "identifier", text, identifier, Document.identifier == identifier
        )
    if _DOI.fullmatch(text):
        return _doi(text, text)
    if _ORCID.fullmatch(text):
        return _orcid(text, text)
    return _bibcode(text, text) or _arxiv(text, text)


def _bibcode(source: str, text: str) -> Reading | None:
    try:
        year = int(parse_bibcode(text)["year"])
    except ValueError:
        return None
    if year < FIRST_YEAR:
        return None
    return Reading("bibcode", source, text, Document.bibcode == text)


def _doi(source: str, text: str) -> Reading:
    return Reading("doi", source, text, Document.doi == text)


def _arxiv(source: str, text: str) -> Reading | None:
    match = _ARXIV.fullmatch(text)
    if match is None:
        return None
    return Reading("arxiv", source, match[1], Document.arxiv == match[1])


def _orcid(source: str, text: str) -> Reading:
    orcid = text.upper()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", QueryWarning)
        expr = Document.orcid == orcid
    return Reading("orcid", source, orcid, expr)


class _Parser:
    def __init__(self, tokens: list[_Token], today: date) -> None:
        self.tokens = tokens
        self.last_year = today.year + 2
        self.readings: list[Reading] = []
        self.notes: list[str] = []
        self.journals = _journals()
        self.thesaurus = _thesaurus()

    def run(self) -> None:
        rules = (
            self._prefixed,
            self._quoted,
            self._identified,
            self._et_al,
            self._year_words,
            self._year,
            self._journal,
            self._full_name,
            self._concept,
            self._surname_alone,
            self._word,
        )
        position = 0
        while position < len(self.tokens):
            for rule in rules:
                following = rule(position)
                if following is not None:
                    position = following
                    break
        if not self.readings and self.tokens:
            # Nothing but words like "the": search for them after all.
            text = " ".join(token.text for token in self.tokens)
            self._add("text", text, f'"{text}"', Document.abs == text)
        if self.thesaurus is None and any(reading.kind in ("text", "author or text") for reading in self.readings):
            self.notes.append(
                "The Unified Astronomy Thesaurus is not downloaded, so topics are searched as words; "
                "run `scix --refresh-data` once to have them recognised as concepts."
            )

    def _add(self, kind: str, source: str, value: str, expr: Expr, loose: Expr | None = None) -> None:
        self.readings.append(Reading(kind, source, value, expr, loose))

    def _plain(self, position: int) -> _Token | None:
        """The token at ``position`` if it is an ordinary unquoted word."""
        if position >= len(self.tokens):
            return None
        token = self.tokens[position]
        return None if token.quoted or token.prefix is not None else token

    def _year_of(self, text: str) -> int | None:
        if len(text) == 4 and text.isdigit() and FIRST_YEAR <= int(text) <= self.last_year:
            return int(text)
        return None

    def _source(self, start: int, end: int) -> str:
        return " ".join(
            ("^" if token.first else "") + token.text + ("," if token.comma and index < end - 1 else "")
            for index, token in enumerate(self.tokens[start:end], start)
        )

    # Rule 1: field:value.

    def _prefixed(self, position: int) -> int | None:
        token = self.tokens[position]
        name, value = token.prefix, token.text
        if name is None:
            return None
        source = f"{name}:{value}"
        if name in OPERATORS:
            inner = interpret(value)
            if inner.expr is None:
                return position + 1
            operator: Callable[[Expr], Expr] = getattr(Document, name)
            self._add(name, source, f"of {inner}", operator(inner.expr))
            self.notes.extend(inner.notes)
        elif name == "journal":
            abbreviation, title = self.journals.named(value) or (value, "")
            self._journal_reading(source, abbreviation, title)
        elif name == "uat":
            self._uat(source, value)
        elif name == "year":
            if not self._year_reading(source, value):
                self._add("year", source, value, Document.raw(f"year:{value}"))
        elif name == "orcid":
            self.readings.append(_orcid(source, value))
        else:
            self._add(_KINDS.get(name, name.replace("_", " ")), source, value, self._field(name, value))
        return position + 1

    def _field(self, name: str, value: str) -> Expr:
        """``Document.<name> == value``, or the same in Solr syntax if the value does not suit the field."""
        field = Document._fields[name]
        typed: Any = value
        try:
            if isinstance(field, IntegerField):
                typed = int(value)
            elif isinstance(field, FloatField):
                typed = float(value)
            condition: Expr = getattr(Document, name) == typed
        except (SciXError, TypeError, ValueError):
            return Document.raw(f"{field.solr_name}:{value}")
        return condition

    def _uat(self, source: str, value: str) -> None:
        if value.isdigit():
            concept = UATConcept.lookup(value) if self.thesaurus is not None else None
            name = f"{concept.name} (UAT {value})" if concept is not None else f"UAT {value}"
            self._add("concept", source, name, Document.uat_concepts == int(value))
            return
        concept = self.thesaurus.named(value) if self.thesaurus is not None else None
        if concept is None:
            self._add("concept", source, value, Document.uat == value)
        else:
            self._concept_reading(source, concept, value)

    def _concept_reading(self, source: str, concept: UATConcept, typed: str) -> None:
        expr = Document.uat_concepts == concept
        self._add("concept", source, f"{concept.name} (UAT {concept.id})", expr, expr | (Document.abs == typed))

    # Rule 2: quoted phrases.

    def _quoted(self, position: int) -> int | None:
        token = self.tokens[position]
        if not token.quoted:
            return None
        text = token.text
        first = text.startswith("^")  # "^Kewley, L": the first author
        text = text.lstrip("^").strip()
        if not text:
            return position + 1
        reading = _identifier(text)
        journal = self.journals.named(text) if " " in text else None
        if reading is not None:
            self.readings.append(reading)
        elif (_AUTHOR_PHRASE.fullmatch(text) or (first and _is_name(text))) and text[:1].isupper():
            self._author(token.text, text, first=first)
        elif journal is not None:
            self._journal_reading(text, *journal)
        else:
            self._add("text", f'"{text}"', f'"{text}"', Document.abs == text)
        return position + 1

    # Rule 3: identifiers.

    def _identified(self, position: int) -> int | None:
        reading = _identifier(self.tokens[position].text)
        if reading is None:
            return None
        self.readings.append(reading)
        return position + 1

    # "et al." makes the first author named the first author of the paper.

    def _et_al(self, position: int) -> int | None:
        token = self.tokens[position]
        if token.text == "&":
            return position + 1
        following = self._plain(position + 1)
        if token.text.lower() != "et" or following is None or following.text.lower().rstrip(".") != "al":
            return None
        for index, reading in enumerate(self.readings):
            if reading.kind == "author":
                name = reading.value
                self.readings[index] = Reading(
                    "first author", f"{reading.source} et al.", name, Document.first_author == name
                )
                break
        return position + 2

    # Rule 4: years.

    def _year_words(self, position: int) -> int | None:
        following = self._plain(position + 1)
        operator = _YEAR_WORDS.get(self.tokens[position].text.lower())
        year = self._year_of(following.text) if following is not None else None
        if operator is None or year is None or following is None:
            return None
        source = f"{self.tokens[position].text} {following.text}"
        if operator == ">=":
            self._add("year", source, f"{year} or later", Document.year >= year)
        elif operator == ">":
            self._add("year", source, f"after {year}", Document.year > year)
        elif operator == "<":
            self._add("year", source, f"before {year}", Document.year < year)
        else:
            self._add("year", source, f"{year} or earlier", Document.year <= year)
        return position + 2

    def _year(self, position: int) -> int | None:
        text = self.tokens[position].text
        return position + 1 if self._year_reading(text, text) else None

    def _year_reading(self, source: str, text: str) -> bool:
        year = self._year_of(text)
        if year is not None:
            self._add("year", source, text, Document.year == year)
            return True
        match = _YEAR_RANGE.fullmatch(text)
        if match is not None:
            first, last = self._year_of(match[1]), self._year_of(match[2])
            if first is not None and last is not None and first <= last:
                self._add("year", source, f"{first}–{last}", Document.year.between(first, last))
                return True
            return False
        match = _YEAR_FROM.fullmatch(text)
        if match is not None and self._year_of(match[1]) is not None:
            self._add("year", source, f"{match[1]} or later", Document.year >= int(match[1]))
            return True
        match = _YEAR_TO.fullmatch(text)
        if match is not None and self._year_of(match[1]) is not None:
            self._add("year", source, f"{match[1]} or earlier", Document.year <= int(match[1]))
            return True
        return False

    # Rule 5: journals, with volume and page.

    def _is_number(self, position: int) -> bool:
        token = self._plain(position)
        return token is not None and token.text.isdigit()

    def _journal(self, position: int) -> int | None:
        found = self._journal_at(position)
        if found is None:
            return None
        end, abbreviation, title = found
        self._journal_reading(self._source(position, end), abbreviation, title)
        return self._volume_and_page(end)

    def _journal_at(self, position: int) -> tuple[int, str, str] | None:
        """``(end, abbreviation, title)`` of the journal named by the words starting at ``position``."""
        token = self._plain(position)
        if token is None:
            return None
        text = token.text
        numbered = self._is_number(position + 1)
        volume_follows = numbered and self._year_of(self.tokens[position + 1].text) is None
        alias = JOURNAL_ALIASES.get(text)
        if alias is not None:
            journal = self.journals.by_abbreviation.get(alias)
            return position + 1, alias, (journal.title or "") if journal is not None else ""
        # A word that also names a concept ("MHD") is the journal only before a volume: "MHD 12 34".
        is_concept = self.thesaurus is not None and self.thesaurus.named(text) is not None
        journal = self.journals.by_abbreviation.get(text)
        if journal is not None and (volume_follows or (not is_concept and (numbered or _distinctive(text)))):
            return position + 1, text, journal.title or ""
        journal = self.journals.folded.get(_fold(text)) if len(text) >= 3 else None
        if journal is not None and journal.abbreviation and not is_concept and not self._is_topic_word(text):
            return position + 1, journal.abbreviation, journal.title or ""
        if not text[:1].isupper():
            return None
        # A title of two words or more, written with a capital: "Astrophysical Journal 900 12".
        run = self._run(position, limit=10)
        for length in range(len(run), 1, -1):
            phrase = " ".join(run[:length])
            journal = self.journals.by_title.get(phrase.lower())
            if journal is None or not journal.abbreviation:
                continue
            is_concept = self.thesaurus is not None and self.thesaurus.named(phrase) is not None
            following = self._plain(position + length)
            before_volume = following is not None and following.text.isdigit() and self._year_of(following.text) is None
            if not is_concept or before_volume:
                return position + length, journal.abbreviation, journal.title or ""
        return None

    def _is_topic_word(self, text: str) -> bool:
        return self.thesaurus is not None and text.lower() in self.thesaurus.words

    def _journal_reading(self, source: str, abbreviation: str, title: str) -> None:
        value = f"{title} ({abbreviation})" if title and title != abbreviation else abbreviation
        self._add("journal", source, value, Document.journal == abbreviation)

    def _volume_and_page(self, position: int) -> int:
        """Read the numbers after a journal: a volume, then a page; a lone year stays a year."""
        first = self._plain(position)
        if first is None or not first.text.isdigit():
            return position
        second = self._plain(position + 1)
        third = self._plain(position + 2)
        has_page = second is not None and _PAGE.fullmatch(second.text) is not None
        if self._year_of(first.text) is not None:
            if not has_page:
                return position  # "ApJ 2020": the year rule reads it.
            if third is not None and second is not None and second.text.isdigit() and _PAGE.fullmatch(third.text):
                # "ApJ 2004 608 119": year, volume, page.
                self._add("year", first.text, first.text, Document.year == int(first.text))
                return self._volume_and_page(position + 1)
        self._add("volume", first.text, first.text, Document.volume == first.text)
        if not has_page or second is None:
            return position + 1
        self._add("page", second.text, second.text, Document.page == second.text)
        return position + 2

    # Rules 6 and 8: authors.

    def _surname(self, position: int) -> tuple[str, int] | None:
        """``(surname, end)`` of the surname starting at ``position``, with any particles before it."""
        parts: list[str] = []
        index = position
        while True:
            token = self._plain(index)
            if token is None:
                return None
            if token.text.lower() in PARTICLES and not token.comma and self._plain(index + 1) is not None:
                parts.append(token.text)
                index += 1
                continue
            if not _is_name(token.text):
                return None
            parts.append(token.text)
            return " ".join(parts), index + 1

    def _given(self, position: int) -> tuple[list[str], int]:
        """The initials at ``position`` and after it, stopping after one that ends in a comma."""
        given: list[str] = []
        while True:
            token = self._plain(position)
            if token is None or not _INITIALS.fullmatch(token.text) or self._is_journal_reference(position):
                return given, position
            given.append(token.text)
            position += 1
            if token.comma:
                return given, position

    def _is_journal_reference(self, position: int) -> bool:
        """Whether the word at ``position`` is a journal with its volume: the ``AJ`` of "AJ 125 525"."""
        return self._is_number(position + 1) and self._journal_at(position) is not None

    def _full_name(self, position: int) -> int | None:
        """``Surname, Initials``, ``Surname, Given`` or ``Initials Surname``."""
        start = self.tokens[position]
        if start.quoted or start.prefix is not None:
            return None
        # Initials first: "L. J. Kewley".
        if _LEADING_INITIALS.fullmatch(start.text):
            initials, end = self._leading_initials(position)
            surname = self._surname(end)
            if initials and surname is not None:
                name, end = surname
                self._author(self._source(position, end), f"{name}, {' '.join(initials)}", first=start.first)
                return end
            return None
        surname = self._surname(position)
        if surname is None:
            return None
        name, end = surname
        if not self.tokens[end - 1].comma:
            return None
        given, after = self._given(end)
        if not given:
            token = self._plain(end)
            if token is None or not _is_name(token.text) or self._begins_another_author(end):
                return None
            more, after = self._given(end + 1) if not token.comma else ([], end + 1)
            given = [token.text, *more]
        self._author(self._source(position, after), f"{name}, {' '.join(given)}", first=start.first)
        return after

    def _leading_initials(self, position: int) -> tuple[list[str], int]:
        initials: list[str] = []
        while True:
            token = self._plain(position)
            if token is None or token.comma or not _LEADING_INITIALS.fullmatch(token.text):
                return initials, position
            initials.append(token.text)
            position += 1

    def _begins_another_author(self, position: int) -> bool:
        """Whether the name at ``position`` is the next surname of a list, not a given name."""
        token, following = self.tokens[position], self._plain(position + 1)
        if not token.comma or following is None:
            return False
        return bool(_INITIALS.fullmatch(following.text)) or _is_name(following.text)

    def _surname_alone(self, position: int) -> int | None:
        surname = self._surname(position)
        if surname is None:
            return None
        name, end = surname
        self._author(self._source(position, end), name, first=self.tokens[position].first, loose=_either(name))
        return end

    def _author(self, source: str, name: str, *, first: bool, loose: Expr | None = None) -> None:
        if first:
            self._add("first author", source, name, Document.first_author == name)
        else:
            self._add("author", source, name, Document.author == name, loose)

    # Rule 7: UAT concepts.

    def _run(self, position: int, limit: int) -> list[str]:
        """The ordinary words from ``position`` on: up to ``limit``, ending at a comma, a year or a phrase."""
        words: list[str] = []
        while len(words) < limit:
            token = self._plain(position + len(words))
            if token is None or token.first or self._year_of(token.text) is not None:
                break
            words.append(token.text)
            if token.comma:
                break
        return words

    def _concept(self, position: int) -> int | None:
        if self.thesaurus is None:
            return None
        run = self._run(position, limit=9)
        for length in range(len(run), 0, -1):
            phrase = " ".join(run[:length])
            if length == 1 and (len(phrase) < 3 or phrase.lower() in STOPWORDS or phrase.lower() in PARTICLES):
                break
            concept = self.thesaurus.named(phrase)
            if concept is not None:
                self._concept_reading(self._source(position, position + length), concept, phrase)
                return position + length
        return None

    # Rule 9: words.

    def _word(self, position: int) -> int:
        text = self.tokens[position].text
        if text.lower() in STOPWORDS:
            return position + 1
        if text.lower() == "refereed":
            self._add("property", text, "refereed", Document.property == "refereed")
            return position + 1
        if text.isalpha() and text.islower() and len(text) > 1 and not self._is_topic_word(text):
            self._add("author or text", text, text, _either(text))
        else:
            self._add("text", text, text, Document.abs == text)
        return position + 1
