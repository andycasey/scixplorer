"""Small helpers: parsing bibcodes, normalising document arguments, flattening.

This module duck-types documents and libraries instead of importing the models,
so any module (including the models themselves) can import it without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

from ads.exceptions import APIError

if TYPE_CHECKING:
    from ads.client import Request, Response

__all__ = ["flatten", "parse_bibcode", "to_bibcodes"]

#: A 19-character bibcode: YYYYJJJJJVVVVMPPPPA (https://ui.adsabs.harvard.edu/help/actions/bibcode).
#: Volumes may be letters (``2020svos.conf...11E``) and the qualifier may be
#: lower case (``2016PhRvL.116f1102A``).
_BIBCODE = re.compile(
    r"(?P<year>[0-9]{4})"
    r"(?P<journal_abbreviation>[A-Za-z0-9&.]{5})"
    r"(?P<volume>[A-Za-z0-9.]{4})"
    r"(?P<qualifier>[A-Za-z0-9.])"
    r"(?P<page_number>[0-9.]{4})"
    r"(?P<first_letter_of_last_name>[A-Z])"
)


def parse_bibcode(bibcode: str) -> dict[str, str]:
    """Split a bibcode into its parts, with the padding dots removed.

    Returns a dict with keys ``year``, ``journal_abbreviation``, ``volume``,
    ``qualifier``, ``page_number`` and ``first_letter_of_last_name``. A digit in
    the qualifier position is the leading digit of a five-digit page, so it is
    moved into ``page_number`` and ``qualifier`` is ``""``::

        >>> parse_bibcode("1996PhRvB..5411169K")["page_number"]
        '11169'

    Raises ``ValueError`` if ``bibcode`` is not a 19-character bibcode.
    """
    if not isinstance(bibcode, str):
        raise TypeError(f"parse_bibcode() needs a str, not {type(bibcode).__name__}")
    match = _BIBCODE.fullmatch(bibcode)
    if match is None:
        raise ValueError(f"invalid bibcode {bibcode!r}")
    parts = {key: value.strip(".") for key, value in match.groupdict().items()}
    if parts["qualifier"].isdigit():
        parts["page_number"] = parts["qualifier"] + parts["page_number"]
        parts["qualifier"] = ""
    return parts


_MISSING = object()


def to_bibcodes(*items: Any) -> list[str]:
    """Collect the bibcodes of everything given, in order, without duplicates.

    Each item may be a bibcode string (surrounding whitespace is removed), an
    object with a ``bibcode`` attribute (a document), an object with a
    ``bibcodes`` attribute holding strings (a library; its documents are not
    fetched), or any other iterable of these (lists, sets, generators, queries),
    nested to any depth. Bibcodes are not checked against a pattern, because ADS
    also accepts other identifiers; the server decides what is valid.

    Raises ``ValueError`` for an empty string or a document whose ``bibcode`` is
    ``None``, and ``TypeError`` for anything else.
    """
    return list(dict.fromkeys(_iter_bibcodes(items)))


def _iter_bibcodes(item: Any) -> Iterator[str]:
    if isinstance(item, str):
        yield _clean(item)
        return
    if isinstance(item, (bytes, bytearray)):
        raise TypeError(f"expected a bibcode str, not {type(item).__name__}")
    bibcode = getattr(item, "bibcode", _MISSING)
    if bibcode is not _MISSING:
        if bibcode is None:
            raise ValueError(f"{type(item).__name__} object has no bibcode (it is None)")
        if not isinstance(bibcode, str):
            raise TypeError(
                f"{type(item).__name__}.bibcode must be a str, not {type(bibcode).__name__}"
            )
        yield _clean(bibcode)
        return
    bibcodes = getattr(item, "bibcodes", _MISSING)
    if bibcodes is not _MISSING:
        if isinstance(bibcodes, str) or not isinstance(bibcodes, Iterable):
            raise TypeError(
                f"{type(item).__name__}.bibcodes must be an iterable of str, "
                f"not {type(bibcodes).__name__}"
            )
        for each in bibcodes:
            if not isinstance(each, str):
                raise TypeError(
                    f"{type(item).__name__}.bibcodes must contain str, not {type(each).__name__}"
                )
            yield _clean(each)
        return
    if isinstance(item, Iterable):
        for each in item:
            yield from _iter_bibcodes(each)
        return
    raise TypeError(
        "expected a bibcode str, a document, a library, or an iterable of these, "
        f"not {type(item).__name__}"
    )


def _clean(bibcode: str) -> str:
    stripped = bibcode.strip()
    if not stripped:
        raise ValueError("empty bibcode")
    return stripped


def flatten(obj: Any) -> list[Any]:
    """Flatten nested containers into one list, dropping ``None``.

    Lists, tuples, sets, dicts (their values) and iterators such as generators
    are expanded recursively. Everything else is kept as a single item,
    including strings and other iterables such as libraries or queries (so
    nothing is fetched from ADS). ``None`` is dropped at every level, so that
    ``Document.affiliation`` (one entry per author: an affiliation, a tuple of
    them, or ``None``) can be scanned directly::

        any(aff.country == "Australia" for aff in flatten(doc.affiliation))
    """
    flat: list[Any] = []
    _flatten_into(obj, flat)
    return flat


def _flatten_into(obj: Any, flat: list[Any]) -> None:
    if obj is None:
        return
    if isinstance(obj, dict):
        obj = obj.values()
    elif not isinstance(obj, (list, tuple, set, frozenset, Iterator)):
        flat.append(obj)
        return
    for each in obj:
        _flatten_into(each, flat)


def _require_bibcodes(documents: tuple[Any, ...]) -> list[str]:
    """:func:`to_bibcodes` for a service call, which needs at least one bibcode."""
    bibcodes = to_bibcodes(*documents)
    if not bibcodes:
        raise ValueError("no documents given: pass bibcodes, documents or libraries")
    return bibcodes


def _response_field(request: Request, response: Response, *keys: str) -> Any:
    """``response.data[keys[0]][keys[1]]...``, raising ``APIError`` if it is not there."""
    value = response.data
    for depth, key in enumerate(keys):
        if not isinstance(value, dict) or key not in value:
            path = "".join(f"[{k!r}]" for k in keys[: depth + 1])
            raise APIError(
                f"{request.method} {request.path}: unexpected response from ADS "
                f"(no {path} in {type(response.data).__name__} body)",
                status_code=response.status_code,
                response=response,
            )
        value = value[key]
    return value
