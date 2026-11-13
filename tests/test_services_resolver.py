from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scixplorer.client import Client, Request
from scixplorer.exceptions import APIError
from scixplorer.services import resolver

BIBCODE = "2018A&A...616A...1G"

RECORDS = [
    {
        "bibcode": BIBCODE,
        "title": "ESOURCE (4)",
        "url": f"/link_gateway/{BIBCODE}/ESOURCE",
        "type": "esource",
        "count": 4,
    }
]
REDIRECT = {
    "service": f"/abs/{BIBCODE}/abstract",
    "action": "redirect",
    "link": f"/abs/{BIBCODE}/abstract",
    "link_type": "ABSTRACT",
}


class Server:
    def __init__(self, body: Any) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.body)


@pytest.fixture
def server() -> Server:
    return Server({"action": "display", "links": {"count": 1, "records": RECORDS}})


@pytest.fixture
def client(server: Server) -> Iterator[Client]:
    with Client(token="t", transport=httpx.MockTransport(server)) as c:
        yield c


def test_link_types() -> None:
    assert len(resolver.LINK_TYPES) == len(set(resolver.LINK_TYPES))
    assert all(kind == kind.lower() for kind in resolver.LINK_TYPES)
    assert {"arxiv", "doi", "esource", "pub_pdf", "data", "simbad", "similar"} <= set(
        resolver.LINK_TYPES
    )


def test_request() -> None:
    assert resolver._request(BIBCODE) == Request("GET", f"/resolver/{BIBCODE}")
    assert resolver._request(BIBCODE, "pub_pdf") == Request("GET", f"/resolver/{BIBCODE}/pub_pdf")
    assert resolver._request(BIBCODE).is_idempotent


def test_request_encodes_the_bibcode_as_one_segment() -> None:
    assert resolver._request("a/b?c#d e").path == "/resolver/a%2Fb%3Fc%23d%20e"


def test_external_resources(server: Server, client: Client) -> None:
    assert resolver.external_resources(BIBCODE, client=client) == RECORDS
    (request,) = server.requests
    assert request.method == "GET"
    assert request.url.raw_path == f"/v1/resolver/{BIBCODE}".encode()
    assert request.content == b""


def test_external_resources_of_a_document(server: Server, client: Client) -> None:
    resolver.external_resources(SimpleNamespace(bibcode=f" {BIBCODE}\n"), client=client)
    assert server.requests[0].url.raw_path == f"/v1/resolver/{BIBCODE}".encode()


@pytest.mark.parametrize("body", [{"links": {}}, {"error": "x"}, [], "text"])
def test_external_resources_unexpected_response(body: Any, server: Server, client: Client) -> None:
    server.body = body
    with pytest.raises(APIError, match=r"unexpected response .*\['links'\]"):
        resolver.external_resources(BIBCODE, client=client)


@pytest.mark.parametrize("link_type", ["abstract", "ABSTRACT", " Abstract "])
def test_external_resource(link_type: str, server: Server, client: Client) -> None:
    server.body = REDIRECT
    assert resolver.external_resource(BIBCODE, link_type, client=client) == REDIRECT
    (request,) = server.requests
    assert request.method == "GET"
    assert request.url.raw_path == f"/v1/resolver/{BIBCODE}/abstract".encode()


@pytest.mark.parametrize("link_type", ["arxiv", "doi"])
def test_external_resource_arxiv_and_doi_need_no_identifier(
    link_type: str, server: Server, client: Client
) -> None:
    resolver.external_resource(BIBCODE, link_type, client=client)
    assert server.requests[0].url.path == f"/v1/resolver/{BIBCODE}/{link_type}"


@pytest.mark.parametrize("link_type", ["ads", "pdf", "", 3, None])
def test_external_resource_rejects_unknown_link_type(
    link_type: Any, server: Server, client: Client
) -> None:
    with pytest.raises(ValueError, match="link_type must be one of arxiv, doi, .*associated; got"):
        resolver.external_resource(BIBCODE, link_type, client=client)
    assert server.requests == []


@pytest.mark.parametrize("body", [[], "text", None])
def test_external_resource_unexpected_response(body: Any, server: Server, client: Client) -> None:
    server.body = body
    with pytest.raises(APIError, match="expected a JSON object"):
        resolver.external_resource(BIBCODE, "abstract", client=client)


@pytest.mark.parametrize(
    "document, error, message",
    [
        ([BIBCODE], TypeError, "not list"),
        (SimpleNamespace(bibcodes=[BIBCODE]), TypeError, "not SimpleNamespace"),
        (42, TypeError, "not int"),
        ("", ValueError, "empty bibcode"),
        (SimpleNamespace(bibcode=None), ValueError, "no bibcode"),
    ],
)
def test_one_document_only(
    document: Any, error: type[Exception], message: str, server: Server, client: Client
) -> None:
    with pytest.raises(error, match=message):
        resolver.external_resources(document, client=client)
    with pytest.raises(error, match=message):
        resolver.external_resource(document, "abstract", client=client)
    assert server.requests == []


def test_redirect_is_not_followed() -> None:
    """ADS answers with JSON describing the link; a real 3xx would surface as APIError."""
    location = "https://arxiv.org/abs/1804.09365"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": location})
    )
    with Client(token="t", transport=transport) as client:
        with pytest.raises(APIError) as info:
            resolver.external_resource(BIBCODE, "eprint_html", client=client)
    assert info.value.status_code == 302
    assert info.value.response.headers["location"] == location
