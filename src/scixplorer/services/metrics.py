"""The ADS metrics service: citation and usage statistics for a set of documents.

These are the numbers on the Metrics page of the ADS web interface
(https://ui.adsabs.harvard.edu/help/actions/analyze): how many papers, how
often they are read and cited, and indicators such as the h-index.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from scixplorer.client import Client, Request, get_default_client
from scixplorer.exceptions import APIError
from scixplorer.utils import _require_bibcodes

__all__ = ["METRIC_TYPES", "metrics"]

#: The values accepted in ``types`` by :func:`metrics`, and the parts of the
#: answer each one asks for.
METRIC_TYPES = ("basic", "citations", "indicators", "histograms", "timeseries")


def metrics(
    *documents: Any, types: str | Iterable[str] | None = None, client: Client | None = None
) -> dict[str, Any]:
    """Return citation and usage statistics for the given documents together.

    :param documents: any mix of bibcodes, documents and libraries, or
        iterables of them (see :func:`scixplorer.utils.to_bibcodes`).
    :param types: which statistics to compute, one or several of
        :data:`METRIC_TYPES`; ``None`` (the default) asks for them all.
        ``"basic"`` gives ``"basic stats"`` (papers, reads, downloads),
        ``"citations"`` gives ``"citation stats"``, ``"indicators"`` gives
        ``"indicators"`` (``h``, ``g``, ``i10``, ``i100``, ``m``, ``tori``,
        ``riq``, ``read10``), ``"histograms"`` gives ``"histograms"`` (counts
        per year) and ``"timeseries"`` gives ``"time series"`` (indicators per
        year).
    :param client: the :class:`~scixplorer.client.Client` to use (default:
        :func:`~scixplorer.client.get_default_client`).
    :returns: the statistics as ADS sends them: a dict keyed by the names
        above, with a second entry ending in ``" refereed"`` for the refereed
        documents alone (``"basic stats refereed"``, ...), and
        ``"skipped bibcodes"``, the bibcodes ADS has no data for.

    Raises :class:`~scixplorer.exceptions.APIError` if ADS has data for none of
    the documents.
    """
    request = _request(_require_bibcodes(documents), _types(types))
    response = (client if client is not None else get_default_client()).send(request)
    data = response.data
    if not isinstance(data, dict):
        raise APIError(
            f"{request.method} {request.path}: unexpected response from ADS "
            f"(expected a JSON object, got {type(data).__name__})",
            status_code=response.status_code,
            response=response,
        )
    if "Error" in data:
        # ADS answers 200 with an error in the body when it has no data for any of the documents.
        detail = data.get("Error Info") or data["Error"]
        raise APIError(
            f"{request.method} {request.path}: {detail}", status_code=response.status_code, response=response
        )
    return data


def _types(types: str | Iterable[str] | None) -> list[str] | None:
    """The ``types`` to send: ``None`` for all of them, else the names given, each once."""
    if types is None:
        return None
    names = [types] if isinstance(types, str) else list(types)
    for name in names:
        if name not in METRIC_TYPES:
            raise ValueError(f"types must be among {', '.join(METRIC_TYPES)}; got {name!r}")
    if not names:
        raise ValueError(f"types needs at least one of {', '.join(METRIC_TYPES)}, or None for all")
    return list(dict.fromkeys(names))


def _request(bibcodes: Sequence[str], types: Sequence[str] | None = None) -> Request:
    """The POST to ``/metrics``; like the vis service it takes ``bibcodes``."""
    payload: dict[str, Any] = {"bibcodes": list(bibcodes)}
    if types is not None:
        payload["types"] = list(types)
    return Request("POST", "/metrics", json=payload, idempotent=True)
