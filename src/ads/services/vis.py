"""The ADS visualisation service: data for author and paper network graphs.

These are the networks drawn by the ADS web interface
(https://ui.adsabs.harvard.edu/help/actions/visualize). The functions return
the network data as ADS sends it, ready for a graph library such as D3.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ads.client import Client, Request, get_default_client
from ads.utils import _require_bibcodes, _response_field

__all__ = ["author_network", "paper_network"]


def author_network(*documents: Any, client: Client | None = None) -> Any:
    """Return the data for a network of the authors of the given documents.

    :param documents: any mix of bibcodes, documents and libraries, or
        iterables of them (see :func:`ads.utils.to_bibcodes`).
    :param client: the :class:`~ads.client.Client` to use (default:
        :func:`~ads.client.get_default_client`).
    """
    return _network("author", documents, client)


def paper_network(*documents: Any, client: Client | None = None) -> Any:
    """Return the data for a network of the given papers, linked by shared references.

    ``documents`` and ``client`` are as for :func:`author_network`.
    """
    return _network("paper", documents, client)


def _request(kind: str, bibcodes: Sequence[str]) -> Request:
    """The POST to ``/vis/<kind>-network``.

    Unlike the other services the body key is ``bibcodes``, not ``bibcode``:
    the API documentation says ``bibcode``, but the service rejects it
    (adsabs/vis-services reads ``bibcodes`` or ``query``).
    """
    return Request(
        "POST", f"/vis/{kind}-network", json={"bibcodes": list(bibcodes)}, idempotent=True
    )


def _network(kind: str, documents: tuple[Any, ...], client: Client | None) -> Any:
    request = _request(kind, _require_bibcodes(documents))
    response = (client if client is not None else get_default_client()).send(request)
    return _response_field(request, response, "data")
