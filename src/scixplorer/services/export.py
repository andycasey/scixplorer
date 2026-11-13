"""The ADS export service: citations for documents in BibTeX and other formats.

Every function takes any mix of bibcodes, documents and libraries, or
iterables of them (see :func:`scixplorer.utils.to_bibcodes`), sends one request to
``/export/<format>`` and returns the exported text::

    from scixplorer.services import export
    print(export.bibtex("2015ApJ...808...16N", library, sort="date desc"))
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from scixplorer.client import Client, Request, get_default_client
from scixplorer.expr import Order
from scixplorer.utils import _require_bibcodes, _response_field

__all__ = [
    "CSL_FORMATS",
    "CSL_STYLES",
    "JOURNAL_FORMATS",
    "aastex",
    "ads",
    "bibtex",
    "bibtexabs",
    "csl",
    "custom",
    "dcxml",
    "endnote",
    "icarus",
    "ieee",
    "medlars",
    "mnras",
    "procite",
    "refabsxml",
    "refworks",
    "refxml",
    "ris",
    "soph",
    "votable",
]

#: Values of ``journal_format``: 1 = AASTeX macros where they exist, else the
#: full journal name; 2 = journal abbreviations; 3 = full journal names.
JOURNAL_FORMATS = (1, 2, 3)

#: Values of ``style`` for :func:`csl`.
CSL_STYLES = ("aastex", "icarus", "mnras", "soph", "aspc", "apsj", "aasj", "ieee")

#: Values of ``format`` for :func:`csl`: 1 = Unicode, 2 = HTML, 3 = LaTeX.
CSL_FORMATS = (1, 2, 3)

_COMMON_PARAMS = """
:param sort: ``None`` (the default) sends ADS ``"no sort"``; a string such as
    ``"citation_count desc"`` or an :class:`~scixplorer.expr.Order` such as
    ``Document.year.desc()`` sorts; ``False`` sends no ``sort`` at all.
:param client: the :class:`~scixplorer.client.Client` to use (default:
    :func:`~scixplorer.client.get_default_client`).
"""


def bibtex(
    *documents: Any,
    max_author: int = 10,
    author_cutoff: int = 200,
    key_format: str | None = None,
    journal_format: int = 1,
    sort: Any = None,
    client: Client | None = None,
) -> str:
    """Export documents as BibTeX.

    :param max_author: the most authors listed before the list is truncated.
    :param author_cutoff: records with more authors than this list only ``max_author``.
    :param key_format: how citation keys are built from the first authors' last
        names (``%1H``, ``%2H``, ...), year (``%Y``), journal (``%q``) and
        bibcode, for example ``"%1H:%Y"`` -> ``Accomazzi:2019``. Add ``%zm`` to
        have duplicate keys enumerated; without it duplicates are kept. ``None``
        uses the bibcode.
    :param journal_format: how journal names are written, one of
        :data:`JOURNAL_FORMATS`: 1 = AASTeX macros where they exist, else the
        full name; 2 = abbreviations; 3 = full names.
    :param sort: ``None`` (the default) sends ADS ``"no sort"``; a string such as
        ``"citation_count desc"`` or an :class:`~scixplorer.expr.Order` such as
        ``Document.year.desc()`` sorts; ``False`` sends no ``sort`` at all.
    :param client: the :class:`~scixplorer.client.Client` to use (default:
        :func:`~scixplorer.client.get_default_client`).
    """
    return _export(
        "bibtex",
        documents,
        client,
        sort=_sort(sort),
        **_bibtex_options(max_author, author_cutoff, key_format, journal_format),
    )


def bibtexabs(
    *documents: Any,
    max_author: int = 0,
    author_cutoff: int = 200,
    key_format: str | None = None,
    journal_format: int = 1,
    sort: Any = None,
    client: Client | None = None,
) -> str:
    """Export documents as BibTeX with abstracts.

    The options are those of :func:`bibtex`. The default ``max_author``
    differs because the two ADS endpoints have different defaults.
    """
    return _export(
        "bibtexabs",
        documents,
        client,
        sort=_sort(sort),
        **_bibtex_options(max_author, author_cutoff, key_format, journal_format),
    )


def aastex(
    *documents: Any,
    journal_format: int = 1,
    sort: Any = None,
    client: Client | None = None,
) -> str:
    """Export documents in AASTeX format.

    ``journal_format``, ``sort`` and ``client`` are as for :func:`bibtex`.
    """
    _check_choice("journal_format", journal_format, JOURNAL_FORMATS)
    return _export("aastex", documents, client, sort=_sort(sort), journalformat=journal_format)


def csl(
    *documents: Any,
    style: str,
    format: int,
    journal_format: int | None = None,
    sort: Any = None,
    client: Client | None = None,
) -> str:
    """Export documents in a Citation Style Language style.

    :param style: one of :data:`CSL_STYLES` (case-insensitive).
    :param format: one of :data:`CSL_FORMATS`: 1 = Unicode, 2 = HTML, 3 = LaTeX.
    :param journal_format: as for :func:`bibtex`; ADS uses it only for the
        ``aastex``, ``aspc`` and ``aasj`` styles. ``None`` leaves it to ADS.

    ``sort`` and ``client`` are as for :func:`bibtex`.
    """
    canonical = style.strip().lower() if isinstance(style, str) else style
    if canonical not in CSL_STYLES:
        raise ValueError(f"style must be one of {', '.join(CSL_STYLES)}; got {style!r}")
    _check_choice("format", format, CSL_FORMATS)
    if journal_format is not None:
        _check_choice("journal_format", journal_format, JOURNAL_FORMATS)
    return _export(
        "csl",
        documents,
        client,
        sort=_sort(sort),
        style=canonical,
        format=format,
        journalformat=journal_format,
    )


def custom(*documents: Any, format: str, client: Client | None = None) -> str:
    """Export documents in a custom format.

    :param format: the format specification, for example
        ``"%ZEncoding:latex\\\\bibitem[]  %l %T (%Y) %q, %V, %p-%P."``.
    :param client: as for :func:`bibtex`.

    There is no ``sort``: ADS answers ``/export/custom`` with HTTP 500 when
    given ``sort="no sort"``, so no sort is sent.
    """
    return _export("custom", documents, client, format=format)


def _simple_format(endpoint: str, description: str) -> Callable[..., str]:
    """Build the export function for a format that has no options besides ``sort``."""

    def export(*documents: Any, sort: Any = None, client: Client | None = None) -> str:
        return _export(endpoint, documents, client, sort=_sort(sort))

    export.__name__ = export.__qualname__ = endpoint
    export.__doc__ = f"Export documents in {description}.\n" + _COMMON_PARAMS
    return export


# The formats without options of their own. Each function is named after its
# endpoint, /export/<name>. (ADS also serves /export/rss, which is not in its
# API documentation; it is not exposed here.)
ads = _simple_format("ads", "the ADS generic tagged format")
endnote = _simple_format("endnote", "EndNote format")
medlars = _simple_format("medlars", "MEDLARS format")
procite = _simple_format("procite", "ProCite format")
refworks = _simple_format("refworks", "RefWorks format")
ris = _simple_format("ris", "RIS (Refman) format")
icarus = _simple_format("icarus", "Icarus format")
mnras = _simple_format("mnras", "Monthly Notices of the Royal Astronomical Society format")
soph = _simple_format("soph", "Solar Physics (SoPh) format")
dcxml = _simple_format("dcxml", "Dublin Core (DC) XML format")
refxml = _simple_format("refxml", "REF-XML format")
refabsxml = _simple_format("refabsxml", "REFABS-XML format")
votable = _simple_format("votable", "VOTable format")
ieee = _simple_format("ieee", "IEEE format (Unicode)")


def _request(endpoint: str, bibcodes: Sequence[str], **options: Any) -> Request:
    """The request for ``/export/<endpoint>``.

    Options that are ``None`` are left out of the body: ADS fails on some of
    them (for example ``keyformat``) when they are sent as ``null``.
    """
    payload: dict[str, Any] = {"bibcode": list(bibcodes)}
    payload.update({key: value for key, value in options.items() if value is not None})
    return Request("POST", f"/export/{endpoint}", json=payload, idempotent=True)


def _export(endpoint: str, documents: tuple[Any, ...], client: Client | None, **options: Any) -> str:
    request = _request(endpoint, _require_bibcodes(documents), **options)
    response = (client if client is not None else get_default_client()).send(request)
    text: str = _response_field(request, response, "export")
    return text


def _sort(sort: Any) -> str | None:
    """The ``sort`` value to send for the ``sort`` argument; ``None`` means send none."""
    if sort is None:
        return "no sort"
    if sort is False:
        return None
    if isinstance(sort, str):
        return sort
    if isinstance(sort, Order):
        return f"{sort.field.solr_name} {'desc' if sort.descending else 'asc'}"
    raise TypeError(
        "sort must be None, False, a str such as 'date desc', or an Order such as "
        f"Document.date.desc(), not {type(sort).__name__}"
    )


def _bibtex_options(
    max_author: int, author_cutoff: int, key_format: str | None, journal_format: int
) -> dict[str, Any]:
    """Validate the BibTeX options and return them under their API names."""
    for name, value in (("max_author", max_author), ("author_cutoff", author_cutoff)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    if key_format is not None and not isinstance(key_format, str):
        raise TypeError(f"key_format must be a str or None, not {type(key_format).__name__}")
    _check_choice("journal_format", journal_format, JOURNAL_FORMATS)
    return {
        "maxauthor": max_author,
        "authorcutoff": author_cutoff,
        "keyformat": key_format,
        "journalformat": journal_format,
    }


def _check_choice(name: str, value: Any, allowed: tuple[Any, ...]) -> None:
    if isinstance(value, bool) or value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(map(repr, allowed))}; got {value!r}")
