from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import scixplorer
from scixplorer.client import Client, Request
from scixplorer.exceptions import APIError
from scixplorer.services import metrics as exported
from scixplorer.services.metrics import METRIC_TYPES, _request, metrics

STATISTICS = {
    "skipped bibcodes": [],
    "basic stats": {"number of papers": 2, "total number of reads": 9081},
    "basic stats refereed": {"number of papers": 2, "total number of reads": 9081},
    "citation stats": {"total number of citations": 772},
    "citation stats refereed": {"total number of citations": 772},
    "indicators": {"h": 2, "g": 2, "i10": 2},
    "indicators refereed": {"h": 2, "g": 2, "i10": 2},
}


class Server:
    def __init__(self) -> None:
        self.body: Any = STATISTICS
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
    assert _request(["A", "B"]) == Request("POST", "/metrics", json={"bibcodes": ["A", "B"]}, idempotent=True)
    assert _request(["A"], ["basic"]) == Request(
        "POST", "/metrics", json={"bibcodes": ["A"], "types": ["basic"]}, idempotent=True
    )


def test_metrics_of_bibcodes_documents_and_libraries(server: Server, client: Client) -> None:
    library = SimpleNamespace(bibcodes=["B", "C"])
    assert metrics("A", SimpleNamespace(bibcode="B"), library, client=client) == STATISTICS
    (request,) = server.requests
    assert (request.method, request.url.path) == ("POST", "/v1/metrics")
    assert json.loads(request.content) == {"bibcodes": ["A", "B", "C"]}


@pytest.mark.parametrize(
    "types, sent",
    [
        ("indicators", ["indicators"]),
        (["basic", "citations"], ["basic", "citations"]),
        (("timeseries", "histograms", "timeseries"), ["timeseries", "histograms"]),
        (iter(METRIC_TYPES), list(METRIC_TYPES)),
    ],
)
def test_types(types: Any, sent: list[str], server: Server, client: Client) -> None:
    metrics("A", types=types, client=client)
    assert json.loads(server.requests[0].content) == {"bibcodes": ["A"], "types": sent}


@pytest.mark.parametrize("types", ["h-index", ["basic", "time series"], [], [None]])
def test_unknown_types_are_refused_before_any_request(types: Any, server: Server, client: Client) -> None:
    with pytest.raises(ValueError, match="basic, citations, indicators, histograms, timeseries"):
        metrics("A", types=types, client=client)
    assert server.requests == []


def test_no_documents(server: Server, client: Client) -> None:
    with pytest.raises(ValueError, match="no documents given"):
        metrics([], client=client)
    assert server.requests == []


def test_no_data_for_any_document_is_an_error(server: Server, client: Client) -> None:
    # SciX answers 200 with the error in the body.
    server.body = {"Error": "Unable to get results!", "Error Info": "No data available to generate metrics"}
    with pytest.raises(APIError, match="POST /metrics: No data available to generate metrics") as error:
        metrics("1900Fake.....0....0X", client=client)
    assert error.value.status_code == 200
    server.body = {"Error": "Unable to get results!"}
    with pytest.raises(APIError, match="POST /metrics: Unable to get results!"):
        metrics("A", client=client)


@pytest.mark.parametrize("body", [[], "text", 3])
def test_unexpected_response(body: Any, server: Server, client: Client) -> None:
    server.body = body
    with pytest.raises(APIError, match=r"POST /metrics: unexpected response from ADS \(expected a JSON object"):
        metrics("A", client=client)


def test_the_default_client_is_used(server: Server, client: Client) -> None:
    scixplorer.set_default_client(client)
    try:
        assert metrics("A")["indicators"]["h"] == 2
    finally:
        scixplorer.set_default_client(None)


def test_exported_and_documented() -> None:
    assert exported is metrics and scixplorer.metrics is metrics
    assert metrics.__doc__ and "metrics" in scixplorer.__all__
    assert METRIC_TYPES == ("basic", "citations", "indicators", "histograms", "timeseries")
