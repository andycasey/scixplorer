from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ads.client import Client, Request
from ads.exceptions import APIError
from ads.expr import Order
from ads.fields import IntegerField, StringField
from ads.model import Model
from ads.services import export

BIBCODES = ["2015ApJ...808...16N", "2000A&AS..143...41K"]

#: Every format without options of its own. Hard-coded on purpose: a typo in
#: the module's table must make these tests fail.
SIMPLE_FORMATS = [
    "ads",
    "endnote",
    "medlars",
    "procite",
    "refworks",
    "ris",
    "icarus",
    "mnras",
    "soph",
    "dcxml",
    "refxml",
    "refabsxml",
    "votable",
    "ieee",
]
OTHER_FORMATS = ["bibtex", "bibtexabs", "aastex", "csl", "custom"]


class Paper(Model):
    _key = "bibcode"
    bibcode = StringField("Bibcode")
    citation_count = IntegerField("Citations", solr_name="citation_count_solr")


class Server:
    """A fake ADS answering every request with one JSON body; records requests."""

    def __init__(self, body: Any = None, status: int = 200) -> None:
        self.body = {"export": "EXPORTED"} if body is None else body
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def last(self) -> tuple[str, str, Any]:
        request = self.requests[-1]
        return request.method, request.url.path, json.loads(request.content)


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def client(server: Server) -> Iterator[Client]:
    with Client(token="t", transport=httpx.MockTransport(server), sleep=lambda _: None) as c:
        yield c


def test_all_lists_every_format() -> None:
    names = {name for name in export.__all__ if name.islower()}
    assert names == set(SIMPLE_FORMATS) | set(OTHER_FORMATS)


@pytest.mark.parametrize("name", SIMPLE_FORMATS)
def test_simple_format(name: str, server: Server, client: Client) -> None:
    function = getattr(export, name)
    assert function.__name__ == name
    assert function.__module__ == "ads.services.export"
    assert "Export documents in" in inspect.getdoc(function)
    assert list(inspect.signature(function).parameters) == ["documents", "sort", "client"]

    assert function(*BIBCODES, client=client) == "EXPORTED"
    assert server.last == ("POST", f"/v1/export/{name}", {"bibcode": BIBCODES, "sort": "no sort"})


@pytest.mark.parametrize("name", OTHER_FORMATS)
def test_other_formats_are_documented(name: str) -> None:
    function = getattr(export, name)
    assert function.__name__ == name
    assert inspect.getdoc(function)


def test_request_is_idempotent_post() -> None:
    request = export._request("bibtex", ["A"], sort="no sort", keyformat=None, maxauthor=0)
    assert request == Request(
        "POST",
        "/export/bibtex",
        json={"bibcode": ["A"], "sort": "no sort", "maxauthor": 0},
        idempotent=True,
    )


def test_transient_failure_is_retried() -> None:
    replies = [httpx.Response(503), httpx.Response(200, json={"export": "OK"})]
    transport = httpx.MockTransport(lambda request: replies.pop(0))
    with Client(token="t", transport=transport, sleep=lambda _: None) as client:
        assert export.ris("A", client=client) == "OK"
    assert replies == []


def test_documents_and_libraries(server: Server, client: Client) -> None:
    library = SimpleNamespace(bibcodes=["C", "A"])
    export.ads(["A", SimpleNamespace(bibcode="B")], library, client=client)
    assert server.last[2]["bibcode"] == ["A", "B", "C"]


def test_no_documents(server: Server, client: Client) -> None:
    with pytest.raises(ValueError, match="no documents given"):
        export.ads(client=client)
    with pytest.raises(ValueError, match="no documents given"):
        export.bibtex([], client=client)
    assert server.requests == []


def test_default_client_is_used(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client(token="t", transport=httpx.MockTransport(server))
    monkeypatch.setattr("ads.services.export.get_default_client", lambda: client)
    assert export.ads("A") == "EXPORTED"
    client.close()


@pytest.mark.parametrize(
    "sort, sent",
    [
        (None, {"sort": "no sort"}),
        (False, {}),
        ("citation_count desc", {"sort": "citation_count desc"}),
        (Order(Paper.citation_count.field, descending=True), {"sort": "citation_count_solr desc"}),
        (Paper.citation_count.asc(), {"sort": "citation_count_solr asc"}),
    ],
)
def test_sort(sort: Any, sent: dict[str, str], server: Server, client: Client) -> None:
    export.endnote("A", sort=sort, client=client)
    assert server.last[2] == {"bibcode": ["A"], **sent}


@pytest.mark.parametrize("sort", [True, [], Paper.citation_count, 1])
def test_bad_sort(sort: Any, server: Server, client: Client) -> None:
    with pytest.raises(TypeError, match="sort must be"):
        export.ads("A", sort=sort, client=client)
    assert server.requests == []


@pytest.mark.parametrize("name, max_author", [("bibtex", 10), ("bibtexabs", 0)])
def test_bibtex_defaults(name: str, max_author: int, server: Server, client: Client) -> None:
    assert getattr(export, name)("A", client=client) == "EXPORTED"
    assert server.last == (
        "POST",
        f"/v1/export/{name}",
        {
            "bibcode": ["A"],
            "sort": "no sort",
            "maxauthor": max_author,
            "authorcutoff": 200,
            "journalformat": 1,
        },
    )


@pytest.mark.parametrize("name", ["bibtex", "bibtexabs"])
def test_bibtex_options(name: str, server: Server, client: Client) -> None:
    function = getattr(export, name)
    function(
        "A",
        max_author=2,
        author_cutoff=50,
        key_format="%1H:%Y%zm",
        journal_format=3,
        sort="date desc",
        client=client,
    )
    assert server.last[2] == {
        "bibcode": ["A"],
        "sort": "date desc",
        "maxauthor": 2,
        "authorcutoff": 50,
        "keyformat": "%1H:%Y%zm",
        "journalformat": 3,
    }


@pytest.mark.parametrize(
    "options, error, message",
    [
        ({"max_author": "infinity"}, TypeError, "max_author must be an int, not str"),
        ({"max_author": True}, TypeError, "max_author must be an int, not bool"),
        ({"author_cutoff": 2.5}, TypeError, "author_cutoff must be an int, not float"),
        ({"key_format": 1}, TypeError, "key_format must be a str or None, not int"),
        ({"journal_format": 0}, ValueError, "journal_format must be one of 1, 2, 3; got 0"),
        ({"journal_format": "aas"}, ValueError, "journal_format must be one of 1, 2, 3"),
        ({"journal_format": True}, ValueError, "journal_format must be one of 1, 2, 3"),
    ],
)
@pytest.mark.parametrize("name", ["bibtex", "bibtexabs"])
def test_bibtex_validation(
    name: str, options: dict[str, Any], error: type[Exception], message: str, client: Client
) -> None:
    with pytest.raises(error, match=message):
        getattr(export, name)("A", client=client, **options)


def test_aastex(server: Server, client: Client) -> None:
    assert export.aastex("A", client=client) == "EXPORTED"
    assert server.last == (
        "POST",
        "/v1/export/aastex",
        {"bibcode": ["A"], "sort": "no sort", "journalformat": 1},
    )
    export.aastex("A", journal_format=2, sort=False, client=client)
    assert server.last[2] == {"bibcode": ["A"], "journalformat": 2}
    with pytest.raises(ValueError, match="journal_format must be one of 1, 2, 3; got 4"):
        export.aastex("A", journal_format=4, client=client)


def test_csl(server: Server, client: Client) -> None:
    assert export.csl("A", style=" MNRAS ", format=2, client=client) == "EXPORTED"
    assert server.last == (
        "POST",
        "/v1/export/csl",
        {"bibcode": ["A"], "sort": "no sort", "style": "mnras", "format": 2},
    )
    export.csl("A", style="aastex", format=3, journal_format=1, client=client)
    assert server.last[2] == {
        "bibcode": ["A"],
        "sort": "no sort",
        "style": "aastex",
        "format": 3,
        "journalformat": 1,
    }


@pytest.mark.parametrize(
    "options, message",
    [
        ({"style": "aas", "format": 1}, "style must be one of aastex, icarus, .*ieee; got 'aas'"),
        ({"style": 1, "format": 1}, "style must be one of"),
        ({"style": "mnras", "format": 4}, "format must be one of 1, 2, 3; got 4"),
        ({"style": "mnras", "format": 1, "journal_format": 0}, "journal_format must be one of"),
    ],
)
def test_csl_validation(options: dict[str, Any], message: str, server: Server, client: Client) -> None:
    with pytest.raises(ValueError, match=message):
        export.csl("A", client=client, **options)
    assert server.requests == []


def test_custom_sends_no_sort(server: Server, client: Client) -> None:
    assert export.custom("A", format="%T (%Y)", client=client) == "EXPORTED"
    assert server.last == ("POST", "/v1/export/custom", {"bibcode": ["A"], "format": "%T (%Y)"})
    assert "sort" not in inspect.signature(export.custom).parameters


def test_custom_needs_format() -> None:
    with pytest.raises(TypeError, match="format"):
        export.custom("A")  # type: ignore[call-arg]


@pytest.mark.parametrize("body", [{"msg": "no export"}, ["x"], "not json"])
def test_missing_export_key(body: Any, client: Client, server: Server) -> None:
    server.body = body
    with pytest.raises(APIError, match=r"POST /export/ads: unexpected response .*\['export'\]"):
        export.ads("A", client=client)


def test_api_error_propagates(server: Server, client: Client) -> None:
    server.status, server.body = 400, {"error": "no records found"}
    with pytest.raises(APIError, match="no records found"):
        export.bibtex("A", client=client)
