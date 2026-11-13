"""Tests for the Library model, its request builders and LibraryBackend."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Callable, Generator
from datetime import datetime, timezone
from typing import Any, cast

import httpx
import pytest

import ads.client
from ads.client import Client, Request, Response
from ads.exceptions import ADSError, APIError, DoesNotExist
from ads.expr import InLibrary
from ads.models import library as lib_module
from ads.models.library import Library, LibraryBackend, fetch_bibcodes, fetch_library
from ads.utils import to_bibcodes

PREFIX = "/v1"


def metadata(library_id: str = "abc", **overrides: Any) -> dict[str, Any]:
    """Library metadata as biblib returns it."""
    data = {
        "id": library_id,
        "name": "Gaia papers",
        "description": "Everything Gaia",
        "public": False,
        "owner": "andy",
        "permission": "owner",
        "num_documents": 3,
        "num_users": 1,
        "date_created": "2021-03-12T04:19:06.469872",
        "date_last_modified": "2021-03-13T05:00:00.000001",
    }
    data.update(overrides)
    return data


Reply = Callable[[httpx.Request], httpx.Response]


class FakeADS:
    """A fake ADS API routing (method, path) to canned JSON replies, recording every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Reply] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, body: Any = None, status: int = 200) -> None:
        self.routes[(method, path)] = lambda request: httpx.Response(status, json=body)

    def on_call(self, method: str, path: str, reply: Reply) -> None:
        self.routes[(method, path)] = reply

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(PREFIX)
        reply = self.routes.get((request.method, path))
        if reply is None:
            return httpx.Response(599, json={"error": f"unexpected {request.method} {path}"})
        return reply(request)

    def client(self) -> Client:
        transport = httpx.MockTransport(self)
        return Client(token="t", transport=transport, async_transport=transport, sleep=lambda _: None)

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path.removeprefix(PREFIX)) for r in self.requests]

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)

    def params(self, index: int = -1) -> dict[str, str]:
        return dict(self.requests[index].url.params)


@pytest.fixture
def server() -> FakeADS:
    return FakeADS()


@pytest.fixture
def client(server: FakeADS) -> Client:
    return server.client()


@pytest.fixture
def default_client(client: Client, monkeypatch: pytest.MonkeyPatch) -> Client:
    """Make ``client`` the default client for the duration of a test."""
    monkeypatch.setattr(ads.client, "_default_client", client)
    return client


def loaded(library_id: str = "abc", **overrides: Any) -> Library:
    """A library as a query returns it: full metadata, already fetched."""
    library = Library._from_row(metadata(library_id, **overrides))
    library._metadata_fetched = True
    return library


def run_by_hand(gen: Generator[Request, Response, Any], replies: list[Any]) -> tuple[list[Request], Any]:
    """Drive a generator with canned response bodies; return the requests it made and its result."""
    requests = [next(gen)]
    for data in replies:
        try:
            requests.append(gen.send(Response(200, {}, data)))
        except StopIteration as stop:
            return requests, stop.value
    raise AssertionError(f"generator still running after {len(replies)} replies: {requests!r}")


def page(documents: list[str], total: int | None = None) -> dict[str, Any]:
    meta = metadata() if total is None else metadata(num_documents=total)
    if total is None:
        del meta["num_documents"]
    return {"metadata": meta, "documents": documents, "solr": {}, "updates": {}}


# Request builders.


def test_list_request() -> None:
    request = lib_module._list_request()
    assert (request.method, request.path, request.params, request.json) == ("GET", "/biblib/libraries", None, None)
    assert request.is_idempotent


def test_get_request_with_and_without_paging() -> None:
    plain = lib_module._get_request("abc")
    assert (plain.method, plain.path, plain.params) == ("GET", "/biblib/libraries/abc", None)
    paged = lib_module._get_request("abc", start=0, rows=50)
    assert paged.params == {"start": 0, "rows": 50}


def test_ids_are_percent_encoded_as_one_segment() -> None:
    assert lib_module._get_request("a/b c?").path == "/biblib/libraries/a%2Fb%20c%3F"
    assert lib_module._delete_request("a/b").path == "/biblib/documents/a%2Fb"
    assert lib_module._operation_request("a/b", "empty").path == "/biblib/libraries/operations/a%2Fb"


def test_create_request_omits_unset_name_description_and_documents() -> None:
    request = lib_module._create_request(None, None, False, [])
    assert (request.method, request.path, request.json) == ("POST", "/biblib/libraries", {"public": False})
    assert not request.is_idempotent
    full = lib_module._create_request("n", "d", True, ["A", "B"])
    assert full.json == {"name": "n", "description": "d", "public": True, "bibcode": ["A", "B"]}


def test_update_request_uses_the_documents_endpoint() -> None:
    request = lib_module._update_request("abc", {"name": "new", "public": True})
    assert (request.method, request.path, request.json) == (
        "PUT", "/biblib/documents/abc", {"name": "new", "public": True}
    )
    assert request.is_idempotent


@pytest.mark.parametrize("action", ["add", "remove"])
def test_documents_request(action: str) -> None:
    request = lib_module._documents_request("abc", action, ["A", "B"])
    assert (request.method, request.path) == ("POST", "/biblib/documents/abc")
    assert request.json == {"bibcode": ["A", "B"], "action": action}
    assert not request.is_idempotent


def test_delete_request() -> None:
    request = lib_module._delete_request("abc")
    assert (request.method, request.path, request.json) == ("DELETE", "/biblib/documents/abc", None)
    assert request.is_idempotent


def test_operation_requests() -> None:
    union = lib_module._operation_request("abc", "union", ["x", "y"], name="n", public=False)
    assert (union.method, union.path) == ("POST", "/biblib/libraries/operations/abc")
    assert union.json == {"action": "union", "libraries": ["x", "y"], "name": "n", "public": False}
    assert not union.is_idempotent
    copy = lib_module._operation_request("abc", "copy", ["x"])
    assert copy.json == {"action": "copy", "libraries": ["x"]}
    assert not copy.is_idempotent
    empty = lib_module._operation_request("abc", "empty")
    assert empty.json == {"action": "empty"}
    assert empty.idempotent is True


def test_permission_and_transfer_requests() -> None:
    get = lib_module._get_permissions_request("abc")
    assert (get.method, get.path) == ("GET", "/biblib/permissions/abc")
    put = lib_module._set_permissions_request("abc", "a@b.c", read=True, write=False, admin=False)
    assert (put.method, put.path) == ("POST", "/biblib/permissions/abc")
    assert put.json == {"email": "a@b.c", "permission": {"read": True, "write": False, "admin": False}}
    assert put.idempotent is True
    transfer = lib_module._transfer_request("abc", "a@b.c")
    assert (transfer.method, transfer.path, transfer.json) == ("POST", "/biblib/transfer/abc", {"email": "a@b.c"})
    assert not transfer.is_idempotent


# fetch_library and fetch_bibcodes, driven by hand.


def test_fetch_library_makes_one_request_and_returns_metadata() -> None:
    requests, result = run_by_hand(fetch_library("abc"), [page(["A"], total=1)])
    assert [(r.method, r.path, r.params) for r in requests] == [("GET", "/biblib/libraries/abc", None)]
    assert result["id"] == "abc"


def test_fetch_library_rejects_a_response_without_metadata() -> None:
    gen = fetch_library("abc")
    next(gen)
    with pytest.raises(APIError, match="metadata"):
        gen.send(Response(200, {}, {"documents": []}))


def test_fetch_bibcodes_single_page() -> None:
    requests, result = run_by_hand(fetch_bibcodes("abc"), [page(["A", "B", "C"], total=3)])
    assert result == ["A", "B", "C"]
    assert [r.params for r in requests] == [{"start": 0, "rows": 2000}]


def test_fetch_bibcodes_several_pages_with_a_short_last_page() -> None:
    replies = [page(["A", "B"], 5), page(["C", "D"], 5), page(["E"], 5)]
    requests, result = run_by_hand(fetch_bibcodes("abc", page_size=2), replies)
    assert result == ["A", "B", "C", "D", "E"]
    assert [r.params for r in requests] == [
        {"start": 0, "rows": 2}, {"start": 2, "rows": 2}, {"start": 4, "rows": 2}
    ]


def test_fetch_bibcodes_terminates_when_the_server_ignores_start() -> None:
    replies = [page(["A", "B"], 10), page(["A", "B"], 10)]
    requests, result = run_by_hand(fetch_bibcodes("abc", page_size=2), replies)
    assert result == ["A", "B"]
    assert len(requests) == 2


def test_fetch_bibcodes_deduplicates_in_order() -> None:
    replies = [page(["A", "B", "A"], 6), page(["B", "C", "D"], 6)]
    _, result = run_by_hand(fetch_bibcodes("abc", page_size=3), replies)
    assert result == ["A", "B", "C", "D"]


def test_fetch_bibcodes_stops_on_an_empty_page_when_ads_returns_fewer_than_num_documents() -> None:
    replies = [page(["A", "B"], 3), page([], 3)]
    requests, result = run_by_hand(fetch_bibcodes("abc", page_size=10), replies)
    assert result == ["A", "B"]
    assert [r.params for r in requests] == [{"start": 0, "rows": 10}, {"start": 2, "rows": 10}]


def test_fetch_bibcodes_keeps_going_when_the_server_caps_rows() -> None:
    replies = [page(["A", "B"], 4), page(["C", "D"], 4)]
    requests, result = run_by_hand(fetch_bibcodes("abc", page_size=100), replies)
    assert result == ["A", "B", "C", "D"]
    assert [r.params["start"] for r in requests if r.params] == [0, 2]


def test_fetch_bibcodes_without_num_documents_stops_on_a_short_page() -> None:
    requests, result = run_by_hand(fetch_bibcodes("abc", page_size=2), [page(["A", "B"]), page(["C"])])
    assert result == ["A", "B", "C"]
    assert len(requests) == 2


def test_fetch_bibcodes_of_an_empty_library() -> None:
    requests, result = run_by_hand(fetch_bibcodes("abc"), [page([], 0)])
    assert result == []
    assert len(requests) == 1


@pytest.mark.parametrize("page_size", [0, -1, True, 2.0])
def test_fetch_bibcodes_checks_page_size_when_called(page_size: Any) -> None:
    with pytest.raises(ValueError, match="page_size"):
        fetch_bibcodes("abc", page_size=page_size)


@pytest.mark.parametrize("library_id", ["", "  ", None, 3])
def test_fetch_functions_check_the_id_when_called(library_id: Any) -> None:
    with pytest.raises(ValueError, match="library id"):
        fetch_bibcodes(library_id)
    with pytest.raises(ValueError, match="library id"):
        fetch_library(library_id)


def test_fetch_bibcodes_rejects_documents_that_are_not_strings() -> None:
    gen = fetch_bibcodes("abc")
    next(gen)
    with pytest.raises(APIError, match="documents"):
        gen.send(Response(200, {}, {"metadata": metadata(), "documents": [{"bibcode": "A"}]}))


# LibraryBackend.


def listing(*libraries: dict[str, Any]) -> dict[str, Any]:
    return {"libraries": list(libraries)}


def test_select_lists_the_callers_libraries(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries", listing(metadata("a"), metadata("b", name="Other")))
    libraries = Library.select().using(client).all()
    assert [library.id for library in libraries] == ["a", "b"]
    assert server.calls == [("GET", "/biblib/libraries")]
    assert server.params() == {}
    first = libraries[0]
    assert first.name == "Gaia papers" and first.public is False and first.num_documents == 3
    assert first.owner == "andy" and first.permission == "owner" and first.num_users == 1


def test_select_filters_orders_and_limits_locally(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries", listing(
        metadata("a", name="Gaia DR3", num_documents=150),
        metadata("b", name="gaia small", num_documents=20),
        metadata("c", name="Other", num_documents=900),
        metadata("d", name="More Gaia", num_documents=500),
        metadata("e", name="gaia mid", num_documents=101),
    ))
    query = (
        Library.select()
        .where(Library.name.contains("gaia") & (Library.num_documents > 100))
        .order_by(Library.num_documents.desc())
        .limit(2)
        .using(client)
    )
    assert [library.id for library in query] == ["d", "a"]
    assert query.count() == 2
    assert Library.select().where(Library.name.contains("gaia")).using(client).count() == 4
    assert server.calls == [("GET", "/biblib/libraries")] * 2


def test_filtering_on_a_field_missing_from_the_listing_makes_no_request(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries", listing({"id": "a", "name": "Gaia"}))
    assert Library.select().where(Library.num_users == 1).using(client).all() == []
    assert len(server.requests) == 1


def test_a_pinned_id_fetches_only_that_library(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/pub", page(["A"], total=1) | {"metadata": metadata("pub", owner="other")})
    found = Library.select().where((Library.id == "pub") & Library.public.in_([False])).using(client).all()
    assert [library.owner for library in found] == ["other"]
    assert server.calls == [("GET", "/biblib/libraries/pub")]
    assert server.params() == {}


def test_pinned_ids_with_in(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/a", {"metadata": metadata("a"), "documents": []})
    server.on("GET", "/biblib/libraries/b", {"metadata": metadata("b"), "documents": []})
    found = Library.select().where(Library.id.in_(["a", "b", "a"])).using(client).all()
    assert [library.id for library in found] == ["a", "b"]
    assert server.calls == [("GET", "/biblib/libraries/a"), ("GET", "/biblib/libraries/b")]


def test_several_pins_intersect(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/b", {"metadata": metadata("b"), "documents": []})
    query = Library.select().where(Library.id.in_(["a", "b"]), Library.id == "b").using(client)
    assert [library.id for library in query] == ["b"]
    assert server.calls == [("GET", "/biblib/libraries/b")]


def test_an_empty_pin_makes_no_request(server: FakeADS, client: Client) -> None:
    assert Library.select().where(Library.id.in_([])).using(client).all() == []
    assert server.requests == []


def test_a_pin_under_or_or_not_is_not_a_pin(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries", listing(metadata("a"), metadata("b", name="x")))
    either = Library.select().where((Library.id == "zzz") | (Library.name == "x")).using(client)
    assert [library.id for library in either] == ["b"]
    negated = Library.select().where(Library.id != "a").using(client)
    assert [library.id for library in negated] == ["b"]
    assert server.calls == [("GET", "/biblib/libraries")] * 2


def test_a_pinned_id_ads_does_not_know_yields_no_row(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/gone", {"error": "Library does not exist"}, status=404)
    server.on("GET", "/biblib/libraries/b", {"metadata": metadata("b"), "documents": []})
    found = Library.select().where(Library.id.in_(["gone", "b"])).using(client).all()
    assert [library.id for library in found] == ["b"]
    assert Library.select().where(Library.id == "gone").using(client).count() == 0


def test_other_errors_for_a_pinned_id_propagate(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/bad", {"error": "boom"}, status=500)
    with pytest.raises(APIError, match="boom") as raised:
        Library.select().where(Library.id == "bad").using(client).all()
    assert raised.value.status_code == 500


def test_a_listing_error_propagates(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries", {"error": "boom"}, status=500)
    with pytest.raises(APIError, match="boom"):
        Library.select().using(client).all()


def test_a_malformed_listing_is_an_api_error(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries", {"libraries": ["abc"]})
    with pytest.raises(APIError, match="libraries"):
        Library.select().using(client).all()


def test_explain_shows_the_requests_and_says_filtering_is_local() -> None:
    listed = Library.select().where(Library.name == "x").explain()
    assert [r.path for r in listed.requests] == ["/biblib/libraries"]
    assert any("locally" in note for note in listed.notes)
    pinned = Library.select().where(Library.id.in_(["a", "b"])).explain()
    assert [(r.method, r.path) for r in pinned.requests] == [
        ("GET", "/biblib/libraries/a"), ("GET", "/biblib/libraries/b")
    ]
    assert "2 libraries" in str(pinned)


def test_get_by_name_and_by_id(server: FakeADS, default_client: Client) -> None:
    server.on("GET", "/biblib/libraries", listing(metadata("a", name="One"), metadata("b", name="Two")))
    assert Library.get(name="Two").id == "b"
    with pytest.raises(Library.DoesNotExist):
        Library.get(name="Three")
    server.on("GET", "/biblib/libraries/gone", {"error": "no such library"}, status=404)
    with pytest.raises(Library.DoesNotExist) as raised:
        Library.get(id="gone")
    assert isinstance(raised.value, DoesNotExist)


def test_the_async_path(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries", listing(metadata("a"), metadata("b", num_documents=10)))
    server.on("GET", "/biblib/libraries/gone", {"error": "no"}, status=404)
    server.on("GET", "/biblib/libraries/b", {"metadata": metadata("b"), "documents": []})

    async def main() -> tuple[list[Library], int, list[Library]]:
        rows = await Library.select().order_by(Library.num_documents.desc()).using(client).aall()
        count = await Library.select().where(Library.num_documents > 5).using(client).acount()
        pinned = await Library.select().where(Library.id.in_(["gone", "b"])).using(client).aall()
        await client.aclose()
        return rows, count, pinned

    rows, count, pinned = asyncio.run(main())
    assert [library.id for library in rows] == ["b", "a"]
    assert count == 1
    assert [library.id for library in pinned] == ["b"]


def test_the_backend_is_a_library_backend() -> None:
    assert isinstance(Library._backend, LibraryBackend)


# Fields.


def test_dates_parse_the_biblib_format_as_aware_utc() -> None:
    library = loaded()
    assert library.date_created == datetime(2021, 3, 12, 4, 19, 6, 469872, tzinfo=timezone.utc)
    assert library.date_last_modified is not None
    assert library.date_last_modified.tzinfo == timezone.utc


def test_date_fields_compare_in_queries(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries", listing(
        metadata("old", date_last_modified="2020-01-01T00:00:00.000000"),
        metadata("new", date_last_modified="2022-06-01T12:00:00.5"),
    ))
    query = Library.select().where(Library.date_last_modified >= datetime(2021, 12, 1)).using(client)
    assert [library.id for library in query] == ["new"]


# Creating.


def test_create_sends_the_payload_and_builds_the_library(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/libraries", {"id": "new", "name": "Mine", "description": "d", "bibcode": ["A"]})

    class Doc:
        bibcode = "B"

    other = loaded("other")
    other._bibcodes = ("C", "A")
    library = Library.create("Mine", "d", public=True, documents=["A", Doc(), [other]], client=client)
    assert server.body() == {"name": "Mine", "description": "d", "public": True, "bibcode": ["A", "B", "C"]}
    assert (library.id, library.name, library.description) == ("new", "Mine", "d")
    assert library._data["public"] is True
    assert "num_documents" not in library._data
    assert library.client is client
    assert len(server.requests) == 1


def test_create_without_name_lets_ads_choose(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/libraries", {"id": "new", "name": "Untitled", "description": "Untitled library"})
    library = Library.create(client=client)
    assert server.body() == {"public": False}
    assert library.name == "Untitled"
    assert library.description == "Untitled library"


def test_create_needs_an_id_in_the_response(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/libraries", {"name": "Mine"})
    with pytest.raises(APIError, match="id"):
        Library.create("Mine", client=client)


def test_create_checks_argument_types(client: Client) -> None:
    with pytest.raises(TypeError, match="public"):
        Library.create("x", public="yes", client=client)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="name"):
        Library.create(3, client=client)  # type: ignore[arg-type]


def test_a_created_library_loads_missing_metadata_once(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/libraries", {"id": "new", "name": "Mine"})
    server.on("GET", "/biblib/libraries/new", {
        "metadata": metadata("new", name="server name", num_documents=0), "documents": []
    })
    library = Library.create("Mine", client=client)
    assert library.num_documents == 0
    assert library.owner == "andy"
    assert library.date_created is not None
    assert library.num_users == 1
    assert library.name == "Mine"
    assert server.calls == [("POST", "/biblib/libraries"), ("GET", "/biblib/libraries/new")]
    assert server.params() == {}


# Lazy metadata.


def test_lazy_metadata_is_fetched_once_for_any_number_of_missing_fields(
    server: FakeADS, default_client: Client
) -> None:
    server.on("GET", "/biblib/libraries/abc", {"metadata": metadata(public=True), "documents": []})
    library = Library(id="abc", name="local name")
    assert library.public is True
    assert library.owner == "andy"
    assert library.num_documents == 3
    assert library.name == "local name"
    assert len(server.requests) == 1


def test_lazy_metadata_returns_none_for_fields_ads_did_not_send(server: FakeADS, default_client: Client) -> None:
    server.on("GET", "/biblib/libraries/abc", {"metadata": {"id": "abc"}, "documents": []})
    library = Library(id="abc")
    assert library.name is None
    assert library.owner is None
    assert len(server.requests) == 1


def test_no_fetch_when_every_field_is_present(server: FakeADS, default_client: Client) -> None:
    library = Library._from_row(metadata())
    assert library.name == "Gaia papers" and library.num_documents == 3
    assert server.requests == []


def test_no_fetch_without_an_id(server: FakeADS, default_client: Client) -> None:
    library = Library(name="local")
    assert library.owner is None
    assert server.requests == []


def test_lazy_metadata_uses_the_library_client(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/abc", {"metadata": metadata(), "documents": []})
    library = Library(id="abc")
    library.client = client
    assert library.owner == "andy"


# Client selection.


def test_method_client_beats_library_client_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = {name: FakeADS() for name in ("default", "attribute", "argument")}
    for fake in servers.values():
        fake.on("PUT", "/biblib/documents/abc", {})
    monkeypatch.setattr(ads.client, "_default_client", servers["default"].client())
    library = loaded()
    library.update(name="1")
    library.client = servers["attribute"].client()
    library.update(name="2")
    library.update(name="3", client=servers["argument"].client())
    assert [len(fake.requests) for fake in servers.values()] == [1, 1, 1]


# Metadata methods.


def test_update_sends_only_the_given_keys(server: FakeADS, client: Client) -> None:
    server.on("PUT", "/biblib/documents/abc", {"name": "New"})
    library = loaded()
    library.update(name="New", public=True, client=client)
    assert server.body() == {"name": "New", "public": True}
    assert (library.name, library.public, library.description) == ("New", True, "Everything Gaia")


def test_update_can_clear_the_description(server: FakeADS, client: Client) -> None:
    server.on("PUT", "/biblib/documents/abc", {})
    library = loaded()
    library.update(description="", client=client)
    assert server.body() == {"description": ""}
    assert library.description == ""


def test_update_needs_something_to_change(server: FakeADS, client: Client) -> None:
    with pytest.raises(ValueError, match="at least one"):
        loaded().update(client=client)
    with pytest.raises(TypeError, match="public"):
        loaded().update(public=1, client=client)  # type: ignore[arg-type]
    assert server.requests == []


def test_update_leaves_local_values_alone_on_failure(server: FakeADS, client: Client) -> None:
    server.on("PUT", "/biblib/documents/abc", {"error": "name taken"}, status=409)
    library = loaded()
    with pytest.raises(APIError, match="name taken"):
        library.update(name="Taken", client=client)
    assert library.name == "Gaia papers"


def test_refresh_replaces_metadata_and_drops_cached_bibcodes(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/abc", {"metadata": metadata(name="Renamed", num_documents=7), "documents": []})
    library = loaded()
    library._bibcodes = ("A",)
    assert library.refresh(client=client) is library
    assert (library.name, library.num_documents) == ("Renamed", 7)
    assert library._bibcodes is None
    assert server.params() == {}


# Documents.


def test_bibcodes_are_fetched_once_and_cached(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/abc", page(["A", "B", "C"], total=3))
    library = loaded()
    library.client = client
    assert library.bibcodes == ("A", "B", "C")
    assert library.bibcodes == ("A", "B", "C")
    assert len(server.requests) == 1
    assert server.params() == {"start": "0", "rows": "2000"}


def test_bibcodes_page_through_the_library(server: FakeADS, default_client: Client) -> None:
    pages = {"0": ["A", "B"], "2": ["C"]}
    server.on_call("GET", "/biblib/libraries/abc", lambda request: httpx.Response(
        200, json=page(pages[request.url.params["start"]], total=3)
    ))
    assert loaded().bibcodes == ("A", "B", "C")


def test_bibcodes_of_a_library_not_on_ads() -> None:
    with pytest.raises(ADSError, match="no id"):
        Library(name="local").bibcodes


def test_to_bibcodes_uses_bibcodes_without_iterating_documents(
    server: FakeADS, default_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.on("GET", "/biblib/libraries/abc", page(["A", "B"], total=2))

    def fail(self: Library) -> Any:
        raise AssertionError("documents were iterated")

    monkeypatch.setattr(Library, "__iter__", fail)
    assert to_bibcodes(loaded(), "C") == ["A", "B", "C"]
    assert not hasattr(Library, "bibcode")


@pytest.mark.parametrize(
    ("method", "key", "count", "expected_total"),
    [("add", "number_added", 2, 5), ("remove", "number_removed", 1, 2)],
)
def test_add_and_remove(
    server: FakeADS, client: Client, method: str, key: str, count: int, expected_total: int
) -> None:
    server.on("POST", "/biblib/documents/abc", {key: count})
    library = loaded()
    library._bibcodes = ("A",)
    assert getattr(library, method)("X", ["Y", "X"], client=client) == count
    assert server.body() == {"bibcode": ["X", "Y"], "action": method}
    assert library.num_documents == expected_total
    assert library._bibcodes is None


def test_add_leaves_an_unloaded_count_unloaded(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/documents/abc", {"number_added": 1})
    library = Library(id="abc")
    assert library.add("X", client=client) == 1
    assert "num_documents" not in library._data
    assert len(server.requests) == 1


def test_add_and_remove_need_documents(server: FakeADS, client: Client) -> None:
    with pytest.raises(ValueError, match="no documents"):
        loaded().add(client=client)
    with pytest.raises(ValueError, match="no documents"):
        loaded().remove([], client=client)
    assert server.requests == []


def test_add_rejects_a_malformed_response(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/documents/abc", {"number_added": "2"})
    with pytest.raises(APIError, match="number_added"):
        loaded().add("X", client=client)


def test_empty(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/libraries/operations/abc", {})
    library = loaded()
    library.empty(client=client)
    assert server.body() == {"action": "empty"}
    assert library.num_documents == 0
    assert library.bibcodes == ()
    assert len(server.requests) == 1


def test_documents_query_with_a_fake_document_module(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeQuery:
        def __init__(self, exprs: tuple[Any, ...] = ()) -> None:
            self.exprs = exprs

        def where(self, *exprs: Any) -> FakeQuery:
            return FakeQuery(self.exprs + exprs)

        def __iter__(self) -> Any:
            return iter(["doc"])

    class Document:
        @staticmethod
        def select() -> FakeQuery:
            return FakeQuery()

        @staticmethod
        def in_(library: Library) -> InLibrary:
            return InLibrary(library.id)

    module = types.ModuleType("ads.models.document")
    module.Document = Document  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ads.models.document", module)
    library = loaded()
    assert cast(Any, library.documents).exprs == (InLibrary("abc"),)
    assert list(library) == ["doc"]


# Set operations.


@pytest.mark.parametrize("action", ["union", "intersection", "difference"])
def test_set_operations_create_a_library(server: FakeADS, client: Client, action: str) -> None:
    server.on("POST", "/biblib/libraries/operations/abc", {
        "id": "result", "name": "Combined", "description": "from ADS", "bibcode": ["A", "B"]
    })
    result = getattr(loaded(), action)(loaded("x"), "y", name="Combined", client=client)
    assert server.body() == {"action": action, "libraries": ["x", "y"], "name": "Combined", "public": False}
    assert (result.id, result.name, result.description) == ("result", "Combined", "from ADS")
    assert result._data["public"] is False
    assert result.client is client
    assert not server.requests[-1].url.params


def test_set_operation_result_loads_the_rest_lazily(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/libraries/operations/abc", {"id": "result", "bibcode": ["A"]})
    server.on("GET", "/biblib/libraries/result", {"metadata": metadata("result", num_documents=1), "documents": []})
    result = loaded().union("x", client=client)
    assert len(result) == 1
    assert result.name == "Gaia papers"
    assert len(server.requests) == 2


@pytest.mark.parametrize("action", ["union", "intersection", "difference"])
def test_set_operations_need_another_library(server: FakeADS, client: Client, action: str) -> None:
    with pytest.raises(ValueError, match="at least one other library"):
        getattr(loaded(), action)(client=client)
    with pytest.raises(TypeError, match="Library or a library id"):
        getattr(loaded(), action)(["x"], client=client)
    with pytest.raises(ADSError, match="no id"):
        getattr(loaded(), action)(Library(name="local"), client=client)
    assert server.requests == []


def test_copy_to_invalidates_the_target(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/libraries/operations/abc", {})
    server.on("GET", "/biblib/libraries/target", {"metadata": metadata("target", num_documents=9), "documents": []})
    target = loaded("target", num_documents=2)
    target._bibcodes = ("Z",)
    target.client = client
    loaded().copy_to(target, client=client)
    assert server.body() == {"action": "copy", "libraries": ["target"]}
    assert target._bibcodes is None
    assert target.num_documents == 9
    assert target.name == "Gaia papers"
    assert len(server.requests) == 2


def test_copy_to_an_id(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/libraries/operations/abc", {})
    loaded().copy_to("target", client=client)
    assert server.body() == {"action": "copy", "libraries": ["target"]}


# Permissions.


def test_get_permissions_merges_the_list(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/permissions/abc", [{"me@x.org": ["owner"]}, {"you@y.org": ["read", "write"]}])
    assert loaded().get_permissions(client=client) == {"me@x.org": ["owner"], "you@y.org": ["read", "write"]}


def test_get_permissions_rejects_a_malformed_response(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/permissions/abc", {"me@x.org": ["owner"]})
    with pytest.raises(APIError, match="permission"):
        loaded().get_permissions(client=client)


def test_set_permissions(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/permissions/abc", {})
    loaded().set_permissions(" you@y.org ", read=True, admin=True, client=client)
    assert server.body() == {"email": "you@y.org", "permission": {"read": True, "write": False, "admin": True}}


def test_transfer(server: FakeADS, client: Client) -> None:
    server.on("POST", "/biblib/transfer/abc", {})
    loaded().transfer("new@owner.org", client=client)
    assert server.body() == {"email": "new@owner.org"}


@pytest.mark.parametrize("email", ["", "   ", "nobody", None, 3])
def test_emails_are_checked(server: FakeADS, client: Client, email: Any) -> None:
    with pytest.raises(ValueError, match="email"):
        loaded().set_permissions(email, read=True, client=client)
    with pytest.raises(ValueError, match="email"):
        loaded().transfer(email, client=client)
    assert server.requests == []


def test_set_permissions_needs_bools(client: Client) -> None:
    with pytest.raises(TypeError, match="write"):
        loaded().set_permissions("a@b.c", write="yes", client=client)  # type: ignore[arg-type]


# Deletion.


def test_delete_then_every_remote_method_raises(server: FakeADS, client: Client) -> None:
    server.on("DELETE", "/biblib/documents/abc", {})
    library = Library(id="abc", name="x")
    library.client = client
    library.delete()
    assert server.calls == [("DELETE", "/biblib/documents/abc")]
    calls: list[Callable[[], Any]] = [
        lambda: library.update(name="y"),
        library.refresh,
        lambda: library.add("A"),
        lambda: library.remove("A"),
        library.empty,
        library.delete,
        lambda: library.union("x"),
        lambda: library.intersection("x"),
        lambda: library.difference("x"),
        lambda: library.copy_to("x"),
        library.get_permissions,
        lambda: library.set_permissions("a@b.c"),
        lambda: library.transfer("a@b.c"),
        lambda: library.bibcodes,
        lambda: library.documents,
        lambda: library.owner,
        lambda: "A" in library,
        lambda: loaded("other").copy_to(library, client=client),
        lambda: loaded("other").union(library, client=client),
    ]
    for call in calls:
        with pytest.raises(ADSError, match="deleted"):
            call()
    assert len(server.requests) == 1
    assert library.name == "x"


def test_methods_on_a_library_not_on_ads(server: FakeADS, client: Client) -> None:
    with pytest.raises(ADSError, match="Library.create"):
        Library(name="x").add("A", client=client)
    assert server.requests == []


# Python protocols.


def test_len_is_num_documents(server: FakeADS, default_client: Client) -> None:
    assert len(loaded(num_documents=42)) == 42
    with pytest.raises(ADSError, match="not known"):
        len(Library(name="local"))


def test_contains_checks_one_bibcode(server: FakeADS, client: Client) -> None:
    server.on("GET", "/biblib/libraries/abc", page(["A", "B"], total=2))
    library = loaded()
    library.client = client

    class Doc:
        bibcode = "B"

    assert "A" in library
    assert Doc() in library
    assert "C" not in library
    with pytest.raises(ValueError, match="single"):
        ["A", "B"] in library  # noqa: B015
    assert len(server.requests) == 1


def test_an_empty_library_is_truthy() -> None:
    assert bool(loaded(num_documents=0))
    assert bool(Library())


def test_repr_never_fetches(server: FakeADS, default_client: Client) -> None:
    assert repr(Library(id="abc")) == "<Library: id='abc'>"
    assert repr(loaded()) == "<Library: id='abc' name='Gaia papers'>"
    assert repr(Library()) == "<Library>"
    assert server.requests == []
