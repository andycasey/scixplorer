"""The ADS resolver service: links from a document to external resources.

These are the full text (publisher, arXiv, scans), data archives, and other
pages that the ADS web interface lists for a record.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ads.client import Client, Request, Response, get_default_client
from ads.exceptions import APIError
from ads.utils import _response_field, to_bibcodes

__all__ = ["LINK_TYPES", "external_resource", "external_resources"]

#: The values accepted for ``link_type`` by :func:`external_resource`.
#: ``arxiv`` and ``doi`` are not in the ADS API documentation, but the resolver
#: answers them; ``similar`` is reported by :func:`external_resources`.
LINK_TYPES = (
    "arxiv", "doi",
    "abstract", "citations", "references", "coreads", "similar", "toc", "openurl",
    "metrics", "graphics",
    # Full text sources and their kinds.
    "esource", "pub_pdf", "eprint_pdf", "author_pdf", "ads_pdf", "pub_html",
    "eprint_html", "author_html", "ads_scan", "gif", "preprint", "ejournal",
    # On-line data and the archives that provide it.
    "data", "aca", "alma", "ari", "astroverse", "atnf", "author", "bavj", "bicep2",
    "cadc", "cds", "chandra", "dryad", "esa", "eso", "figshare", "gcpd", "github",
    "gtc", "heasarc", "herschel", "ibvs", "ines", "iso", "jwst", "koa", "mast",
    "ned", "nexsci", "noao", "pangaea", "pasa", "pdg", "pds", "protocols",
    "simbad", "spitzer", "tns", "vizier", "xmm", "zenodo",
    "inspire", "librarycatalog", "presentation", "associated",
)


def external_resources(document: Any, *, client: Client | None = None) -> list[dict[str, Any]]:
    """Return every external resource ADS knows for one document.

    :param document: a bibcode, or an object with a ``bibcode`` (a document).
    :param client: the :class:`~ads.client.Client` to use (default:
        :func:`~ads.client.get_default_client`).
    :returns: one dict per kind of resource, with keys such as ``type``
        (a link type), ``title``, ``url`` and ``count``.
    """
    request = _request(_bibcode(document))
    resources: list[dict[str, Any]] = _response_field(
        request, _send(request, client), "links", "records"
    )
    return resources


def external_resource(
    document: Any, link_type: str, *, client: Client | None = None
) -> dict[str, Any]:
    """Return the external resources of one kind for one document.

    :param document: a bibcode, or an object with a ``bibcode`` (a document).
    :param link_type: one of :data:`LINK_TYPES` (case-insensitive), such as
        ``"esource"`` (all full-text sources), ``"pub_pdf"`` or ``"data"``.
    :param client: as for :func:`external_resources`.
    :returns: the resolver's answer as-is. When there is a single link it looks
        like ``{"action": "redirect", "link": url, "link_type": ..., "service": ...}``;
        when there are several, ``{"action": "display", "links": {"count": n,
        "records": [{"title": ..., "url": ..., "link_type": ...}, ...]}, ...}``.
    """
    kind = _link_type(link_type)
    request = _request(_bibcode(document), kind)
    response = _send(request, client)
    if not isinstance(response.data, dict):
        raise APIError(
            f"{request.method} {request.path}: unexpected response from ADS "
            f"(expected a JSON object, got {type(response.data).__name__})",
            status_code=response.status_code,
            response=response,
        )
    return response.data


def _request(bibcode: str, link_type: str | None = None) -> Request:
    """The GET for ``/resolver/<bibcode>[/<link_type>]``.

    The bibcode is percent-encoded as one path segment, except ``&``, which
    ADS accepts literally (as in ``A&A`` bibcodes).
    """
    path = f"/resolver/{quote(bibcode, safe='&')}"
    if link_type is not None:
        path += f"/{link_type}"
    return Request("GET", path)


def _send(request: Request, client: Client | None) -> Response:
    return (client if client is not None else get_default_client()).send(request)


def _bibcode(document: Any) -> str:
    if isinstance(document, str) or hasattr(document, "bibcode"):
        return to_bibcodes(document)[0]
    raise TypeError(
        f"expected a bibcode str or a document with a bibcode, not {type(document).__name__}"
    )


def _link_type(link_type: str) -> str:
    kind = link_type.strip().lower() if isinstance(link_type, str) else link_type
    if kind not in LINK_TYPES:
        raise ValueError(f"link_type must be one of {', '.join(LINK_TYPES)}; got {link_type!r}")
    return kind
