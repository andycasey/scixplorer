"""The ADS citation helper: suggest papers missing from a set of documents."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from scixplorer.client import Client, Request, get_default_client
from scixplorer.exceptions import APIError
from scixplorer.utils import _require_bibcodes

__all__ = ["citation_helper"]


def citation_helper(*documents: Any, client: Client | None = None) -> list[dict[str, Any]]:
    """Suggest up to ten papers that the given documents probably should cite.

    ADS uses a 'friends of friends' analysis: the suggestions cite, or are
    cited by, the given documents but are not among them.

    :param documents: any mix of bibcodes, documents and libraries, or
        iterables of them (see :func:`scixplorer.utils.to_bibcodes`).
    :param client: the :class:`~scixplorer.client.Client` to use (default:
        :func:`~scixplorer.client.get_default_client`).
    :returns: the suggestions as ADS sends them: dicts with keys ``bibcode``,
        ``score`` (an int), and ``title`` and ``author`` (each one string,
        not a list as in search results).
    """
    request = _request(_require_bibcodes(documents))
    response = (client if client is not None else get_default_client()).send(request)
    if not isinstance(response.data, list):
        raise APIError(
            f"{request.method} {request.path}: unexpected response from ADS "
            f"(expected a JSON list, got {type(response.data).__name__})",
            status_code=response.status_code,
            response=response,
        )
    return response.data


def _request(bibcodes: Sequence[str]) -> Request:
    """The POST to ``/citation_helper``; like the vis service it takes ``bibcodes``."""
    return Request("POST", "/citation_helper", json={"bibcodes": list(bibcodes)}, idempotent=True)
