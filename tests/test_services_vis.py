from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ads.client import Client, Request
from ads.exceptions import APIError
from ads.services import vis

NETWORK = {"root": {"name": [], "children": []}, "link_data": []}


class Server:
    def __init__(self) -> None:
        self.body: Any = {"msg": {"numFound": 2}, "data": NETWORK}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.body)


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def client(server: Server) -> Iterator[Client]:
    with Client(token="t", transport=httpx.MockTransport(server)) as c:
        yield c


FUNCTIONS = [(vis.author_network, "author"), (vis.paper_network, "paper")]


@pytest.mark.parametrize("kind", ["author", "paper"])
def test_request(kind: str) -> None:
    assert vis._request(kind, ["A", "B"]) == Request(
        "POST", f"/vis/{kind}-network", json={"bibcodes": ["A", "B"]}, idempotent=True
    )


@pytest.mark.parametrize("function, kind", FUNCTIONS)
def test_network(function: Any, kind: str, server: Server, client: Client) -> None:
    library = SimpleNamespace(bibcodes=["B", "C"])
    assert function("A", SimpleNamespace(bibcode="B"), library, client=client) == NETWORK
    (request,) = server.requests
    assert request.method == "POST"
    assert request.url.path == f"/v1/vis/{kind}-network"
    assert json.loads(request.content) == {"bibcodes": ["A", "B", "C"]}


@pytest.mark.parametrize("function, kind", FUNCTIONS)
def test_no_documents(function: Any, kind: str, server: Server, client: Client) -> None:
    with pytest.raises(ValueError, match="no documents given"):
        function([], client=client)
    assert server.requests == []


@pytest.mark.parametrize("function, kind", FUNCTIONS)
@pytest.mark.parametrize("body", [{"msg": "error"}, [], "text"])
def test_missing_data(function: Any, kind: str, body: Any, server: Server, client: Client) -> None:
    server.body = body
    with pytest.raises(APIError, match=rf"POST /vis/{kind}-network: .*\['data'\]"):
        function("A", client=client)


@pytest.mark.parametrize("function, kind", FUNCTIONS)
def test_documented(function: Any, kind: str) -> None:
    assert function.__doc__
    assert function.__name__ in vis.__all__
