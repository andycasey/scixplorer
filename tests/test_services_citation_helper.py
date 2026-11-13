from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ads.client import Client, Request
from ads.exceptions import APIError
from ads.services.citation_helper import _request, citation_helper

SUGGESTIONS = [
    {
        "bibcode": "2016A&A...595A...1G",
        "score": 37,
        "title": "The Gaia mission",
        "author": "Gaia Collaboration; Prusti, T.",
    }
]


class Server:
    def __init__(self) -> None:
        self.body: Any = SUGGESTIONS
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


def test_request() -> None:
    assert _request(["A", "B"]) == Request(
        "POST", "/citation_helper", json={"bibcodes": ["A", "B"]}, idempotent=True
    )


def test_citation_helper(server: Server, client: Client) -> None:
    library = SimpleNamespace(bibcodes=["A", "C"])
    assert citation_helper(["A", "B"], library, client=client) == SUGGESTIONS
    (request,) = server.requests
    assert request.method == "POST"
    assert request.url.path == "/v1/citation_helper"
    assert json.loads(request.content) == {"bibcodes": ["A", "B", "C"]}


def test_no_documents(server: Server, client: Client) -> None:
    with pytest.raises(ValueError, match="no documents given"):
        citation_helper(client=client)
    assert server.requests == []


@pytest.mark.parametrize("body", [{"suggestions": []}, "text", None])
def test_unexpected_response(body: Any, server: Server, client: Client) -> None:
    server.body = body
    with pytest.raises(APIError, match="POST /citation_helper: .*expected a JSON list"):
        citation_helper("A", client=client)
