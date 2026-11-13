from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import aclosing
from typing import Any

import pytest

import ads.client
from ads.client import Request, Response
from ads.exceptions import APIError, DoesNotExist, NotLocallyEvaluable, QueryError
from ads.expr import NOTHING, And, Match, Order
from ads.fields import IntegerField, StringField, VirtualField
from ads.model import Model
from ads.query import (
    Explanation,
    MemoryBackend,
    Query,
    adrive,
    drive,
    filter_rows,
    select_rows,
)

# --- an in-memory model -------------------------------------------------------


class Item(Model):
    _key = "name"
    name = StringField("Name")
    size = IntegerField("Size")
    tags = StringField("Tags", multivalued=True)
    text = VirtualField("Search-only text")


ITEMS = (
    Item(name="a", size=3, tags=("x",)),
    Item(name="b", size=1, tags=("x", "y")),
    Item(name="c", size=2, tags=()),
    Item(name="d"),
    Item(name="e", size=5, tags=("y",)),
)


class Loader:
    """A loader that counts how often it is called."""

    def __init__(self, rows: tuple[Any, ...] = ITEMS):
        self.rows = rows
        self.calls = 0

    def __call__(self) -> tuple[Any, ...]:
        self.calls += 1
        return self.rows


Item._backend = MemoryBackend(Loader(), key="name")


class Bare(Model):
    _key = "name"
    name = StringField("Name")


def names(rows: list[Item]) -> list[str]:
    return [row.name for row in rows]


# --- a remote model with a scripted, paginating backend -----------------------


class Remote(Model):
    _key = "n"
    n = IntegerField("Number")


class FakeClient:
    """Serves rows ``n = 0 .. total-1`` by ``start``/``rows`` params; records every request."""

    def __init__(self, total: int = 5, log: list[Any] | None = None, fail: Exception | None = None):
        self.total = total
        self.sent: list[Request] = []
        self.log = log if log is not None else []
        self.fail = fail

    def _respond(self, request: Request) -> Response:
        self.sent.append(request)
        self.log.append(("send", dict(request.params or {})))
        if self.fail is not None:
            raise self.fail
        params = request.params or {}
        start, rows = params.get("start", 0), params["rows"]
        docs = [{"n": i} for i in range(start, min(start + rows, self.total))]
        return Response(200, {}, {"numFound": self.total, "docs": docs})

    def send(self, request: Request) -> Response:
        return self._respond(request)

    async def asend(self, request: Request) -> Response:
        await asyncio.sleep(0)
        return self._respond(request)


class PagedBackend:
    """Pages through a FakeClient two rows at a time, like a search backend."""

    page_size = 2

    def __init__(self) -> None:
        self.events: list[str] = []

    def run(self, query: Query[Any]) -> Generator[Any, Any, None]:
        self.events.append("start")
        try:
            start, remaining = query.offset_, query.limit_
            while remaining is None or remaining > 0:
                rows = self.page_size if remaining is None else min(self.page_size, remaining)
                response = yield Request("GET", "/fake", params={"start": start, "rows": rows})
                batch = [Remote._from_row(doc) for doc in response.data["docs"]]
                yield batch
                if len(batch) < rows:
                    return
                start += rows
                remaining = None if remaining is None else remaining - rows
        finally:
            self.events.append("closed")

    def count(self, query: Query[Any]) -> Generator[Request, Response, int]:
        response = yield Request("GET", "/fake", params={"rows": 0})
        return response.data["numFound"]

    def explain(self, query: Query[Any]) -> Explanation:
        return Explanation("fake plan", (Request("GET", "/fake", params={"rows": 0}),))


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> PagedBackend:
    backend = PagedBackend()
    monkeypatch.setattr(Remote, "_backend", backend, raising=False)
    return backend


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def no_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> Any:
        raise AssertionError("the default client must not be needed")

    monkeypatch.setattr(ads.client, "get_default_client", fail)


def ns(rows: list[Remote]) -> list[int]:
    return [row.n for row in rows]


def pages(client: FakeClient) -> tuple[list[Any], list[Any]]:
    return [(r.params or {}).get("start") for r in client.sent], [(r.params or {}).get("rows") for r in client.sent]


# --- construction and builders -------------------------------------------------


def test_defaults_and_fields() -> None:
    query = Query(Item)
    assert query.model is Item
    assert (query.fields, query.expr, query.order, query.limit_, query.offset_, query.client) == (
        (),
        None,
        (),
        None,
        0,
        None,
    )
    assert Item.select("name", Item.size).fields == (Item._fields["name"], Item._fields["size"])
    with pytest.raises(TypeError, match="Model subclass"):
        Query(object)  # type: ignore[type-var]


def test_builders_return_new_queries_and_never_mutate() -> None:
    base = Item.select()
    a, b, c = Item.size > 1, Item.name != "a", Item.tags == "x"
    derived = [base.where(a), base.order_by(Item.size), base.limit(2), base.offset(1), base.using(object())]
    for query in derived:
        assert query is not base
    assert (base.expr, base.order, base.limit_, base.offset_, base.client) == (None, (), None, 0, None)

    same = base.where()
    assert same is not base and same.expr is None
    narrowed = base.where(a)
    assert narrowed.where().expr == a
    assert narrowed.where(b, c).expr == And((a, b, c))
    assert base.where(a & b).where(c).expr == And((a, b, c))
    assert narrowed.expr == a


def test_order_by_accepts_orders_handles_and_names_and_replaces() -> None:
    size, name = Item._fields["size"], Item._fields["name"]
    query = Item.select().order_by("size", Item.name.desc())
    assert query.order == (Order(size), Order(name, True))
    assert query.order_by(Item.size).order == (Order(size),)
    assert query.order_by(size).order == (Order(size),)
    assert query.order_by().order == ()


@pytest.mark.parametrize(
    ("build", "error", "message"),
    [
        (lambda q: q.where(True), TypeError, "and/or/not"),
        (lambda q: q.where(Item.size > 1, False), TypeError, "bool"),
        (lambda q: q.where("size > 1"), TypeError, "needs expressions"),
        (lambda q: q.order_by(Bare.name), TypeError, "not a field of Item"),
        (lambda q: q.order_by(Bare.name.desc()), TypeError, "not a field of Item"),
        (lambda q: q.order_by("nope"), AttributeError, "no field 'nope'"),
        (lambda q: q.order_by(3), TypeError, "Expected a field"),
        (lambda q: q.limit(-1), ValueError, "non-negative"),
        (lambda q: q.limit(1.5), TypeError, "needs an int"),
        (lambda q: q.limit(True), TypeError, "needs an int"),
        (lambda q: q.offset(None), TypeError, "needs an int"),
        (lambda q: q.offset(-2), ValueError, "non-negative"),
        (lambda q: q.offset("1"), TypeError, "needs an int"),
    ],
)
def test_builder_validation(build: Any, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        build(Item.select())


def test_limit_none_and_zero_are_valid() -> None:
    assert Item.select().limit(3).limit(None).limit_ is None
    assert Item.select().limit(0).limit_ == 0
    assert Item.select().offset(0).offset_ == 0


# --- MemoryBackend through Query ----------------------------------------------


def test_memory_filter_order_offset_limit() -> None:
    query = Item.select().where(Item.size >= 2).order_by(Item.size.desc())
    assert names(query.all()) == ["e", "a", "c"]
    assert names(query.offset(1).all()) == ["a", "c"]
    assert names(query.offset(1).limit(1).all()) == ["a"]
    assert names(query.limit(0).all()) == []
    assert names(Item.select().order_by(Item.size).all()) == ["b", "c", "a", "e", "d"]
    assert names(Item.select().where(Item.tags == "y").all()) == ["b", "e"]
    assert names(Item.select().where(Item.name.in_(["c", "a", "zz"])).all()) == ["a", "c"]


def test_memory_count_applies_offset_and_limit() -> None:
    query = Item.select().where(Item.size >= 2)
    assert query.count() == 3
    assert query.offset(1).count() == 2
    assert query.offset(5).count() == 0
    assert query.offset(1).limit(1).count() == 1
    assert Item.select().count() == 5


def test_memory_first_get_exists() -> None:
    query = Item.select().order_by(Item.size)
    assert query.first().name == "b"
    assert query.get().name == "b"
    assert query.exists()
    empty = Item.select().where(Item.size > 100)
    assert empty.first() is None
    assert not empty.exists()
    with pytest.raises(Item.DoesNotExist):
        empty.get()
    assert Item.select().limit(0).first() is None


def test_model_select_and_get_use_query() -> None:
    assert isinstance(Item.select(), Query)
    assert Item.get(name="b").size == 1
    assert Item.get(Item.size > 2, Item.tags == "y").name == "e"
    with pytest.raises(Item.DoesNotExist) as info:
        Item.get(Item.size > 100)
    assert isinstance(info.value, DoesNotExist)


def test_memory_not_locally_evaluable() -> None:
    with pytest.raises(NotLocallyEvaluable):
        Item.select().where(Item.text == "x").all()
    with pytest.raises(NotLocallyEvaluable):
        Item.select().order_by(Item.text).all()


def test_memory_backend_calls_loader_on_every_run() -> None:
    loader = Loader()
    backend = MemoryBackend(loader)
    query = Query(Item)
    assert list(drive(backend.run(query), _no_send)) == [list(ITEMS)]
    assert list(drive(backend.run(query), _no_send)) == [list(ITEMS)]
    assert loader.calls == 2


def _no_send(request: Request) -> Response:
    raise AssertionError(f"MemoryBackend sent {request!r}")


def _exhaust(driver: Generator[Any, None, Any]) -> tuple[list[Any], Any]:
    items = []
    while True:
        try:
            items.append(next(driver))
        except StopIteration as stop:
            return items, stop.value


def test_memory_backend_yields_exactly_one_batch_even_when_empty() -> None:
    backend = MemoryBackend(Loader())
    empty = Query(Item).where(NOTHING)
    assert _exhaust(drive(backend.run(empty), _no_send)) == ([[]], None)
    assert _exhaust(drive(backend.run(Query(Item).offset(10)), _no_send)) == ([[]], None)


def test_memory_backend_count_ignores_offset_and_limit_without_yielding() -> None:
    backend = MemoryBackend(Loader())
    query = Query(Item).where(Item.size >= 2).offset(1).limit(1)
    assert _exhaust(drive(backend.count(query), _no_send)) == ([], 3)


def test_memory_backend_explain() -> None:
    query = Item.select().where(Item.size > 1).order_by(Item.name.desc())
    explanation = query.explain()
    assert explanation.requests == ()
    assert "locally over 5 Item rows" in explanation.summary
    assert repr(query.expr) in "\n".join(explanation.notes)
    assert "name desc" in str(explanation)
    assert "all rows" in str(Item.select().explain())


def test_memory_backend_lookup() -> None:
    first, duplicate = Item(name="k", size=1), Item(name="k", size=2)
    loader = Loader((first, duplicate, Item(name="m")))
    backend = MemoryBackend(loader, key="name")
    assert loader.calls == 0
    assert backend.lookup("k") is first
    assert backend.lookup("m").name == "m"
    assert backend.lookup("zz") is None
    assert loader.calls == 1
    with pytest.raises(TypeError, match="key="):
        MemoryBackend(loader).lookup("k")


def test_memory_queries_never_need_a_client(no_default_client: None) -> None:
    query = Item.select().where(Item.size > 1)
    assert names(query.all()) == ["a", "c", "e"]
    assert query.count() == 3
    assert query.first().name == "a"
    assert query.exists()
    assert query.get().name == "a"
    assert str(query.explain())

    async def main() -> None:
        fresh = Item.select().where(Item.size > 1)
        assert names(await fresh.aall()) == ["a", "c", "e"]
        assert await Item.select().acount() == 5
        assert (await Item.select().afirst()).name == "a"
        assert await Item.select().aexists()
        assert (await Item.select().aget()).name == "a"

    asyncio.run(main())


# --- execution through the drivers ----------------------------------------------


def test_iteration_streams_batches(backend: PagedBackend) -> None:
    log: list[Any] = []
    client = FakeClient(log=log)
    for row in Remote.select().using(client):
        log.append(("row", row.n))
    assert log == [
        ("send", {"start": 0, "rows": 2}),
        ("row", 0),
        ("row", 1),
        ("send", {"start": 2, "rows": 2}),
        ("row", 2),
        ("row", 3),
        ("send", {"start": 4, "rows": 2}),
        ("row", 4),
    ]


def test_completed_iteration_is_cached(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)
    assert ns(list(query)) == [0, 1, 2, 3, 4]
    assert len(client.sent) == 3
    assert ns(list(query)) == [0, 1, 2, 3, 4]
    assert ns(query.all()) == [0, 1, 2, 3, 4]
    assert len(query) == 5
    assert query[1].n == 1 and query[-1].n == 4
    assert ns(query[1:3]) == [1, 2]
    assert query.first().n == 0 and query.get().n == 0
    assert query.count() == 5 and query.exists()
    assert len(client.sent) == 3
    assert backend.events == ["start", "closed"]


def test_builtins_that_ask_for_len_run_the_query_once(backend: PagedBackend) -> None:
    for build in (list, tuple, sorted):
        client = FakeClient()
        rows = build(Remote.select().using(client), **({"key": lambda r: r.n} if build is sorted else {}))
        assert ns(list(rows)) == [0, 1, 2, 3, 4]
        assert len(client.sent) == 3


def test_all_returns_a_fresh_list(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)
    rows = query.all()
    rows.clear()
    assert len(query.all()) == 5
    assert len(client.sent) == 3


def test_len_and_indexing_fetch_once(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)
    assert query[2].n == 2
    assert len(query) == 5
    assert len(client.sent) == 3


def test_partial_iteration_caches_nothing_and_closes_backend(
    backend: PagedBackend, client: FakeClient
) -> None:
    query = Remote.select().using(client)
    iterator = iter(query)
    assert next(iterator).n == 0
    iterator.close()  # type: ignore[attr-defined]
    assert backend.events == ["start", "closed"]
    assert len(client.sent) == 1

    for row in query:
        if row.n == 2:
            break
    assert len(client.sent) == 3

    assert ns(list(query)) == [0, 1, 2, 3, 4]
    assert len(client.sent) == 6
    assert backend.events == ["start", "closed"] * 3


def test_concurrent_iterations_are_independent(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)
    first, second = iter(query), iter(query)
    seen: tuple[list[int], list[int]] = ([], [])
    active = [(first, seen[0]), (second, seen[1])]
    while active:
        for pair in list(active):
            iterator, out = pair
            try:
                out.append(next(iterator).n)
            except StopIteration:
                active.remove(pair)
    assert seen == ([0, 1, 2, 3, 4], [0, 1, 2, 3, 4])
    assert len(client.sent) == 6
    assert ns(list(query)) == [0, 1, 2, 3, 4]
    assert len(client.sent) == 6


def test_new_queries_do_not_share_the_cache(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)
    query.all()
    assert len(client.sent) == 3
    assert ns(query.where().all()) == [0, 1, 2, 3, 4]
    assert len(client.sent) == 6


def test_first_fetches_one_row(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)
    assert query.first().n == 0
    assert query.offset(3).first().n == 3
    assert query.limit(4).first().n == 0
    assert pages(client) == ([0, 3, 0], [1, 1, 1])
    assert query.limit(0).first() is None
    assert len(client.sent) == 3
    assert query.offset(5).first() is None
    assert len(client.sent) == 4


def test_get_and_exists(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)
    assert query.offset(2).get().n == 2
    with pytest.raises(Remote.DoesNotExist, match="No Remote matched"):
        query.offset(5).get()
    assert query.exists()
    assert not query.offset(5).exists()
    assert pages(client) == ([2, 5, 0, 5], [1, 1, 1, 1])
    assert not query.limit(0).exists()
    assert len(client.sent) == 4


@pytest.mark.parametrize(
    ("offset", "limit", "expected"),
    [(0, None, 5), (2, None, 3), (5, None, 0), (9, None, 0), (0, 3, 3), (3, 3, 2), (0, 0, 0), (4, 10, 1)],
)
def test_count_applies_offset_and_limit_to_the_backend_total(
    backend: PagedBackend, offset: int, limit: int | None, expected: int
) -> None:
    client = FakeClient()
    query = Remote.select().using(client).offset(offset).limit(limit)
    assert query.count() == expected
    assert [r.params for r in client.sent] == [{"rows": 0}]
    assert asyncio.run(query.acount()) == expected
    assert len(client.sent) == 2
    assert len(query.all()) == expected
    assert asyncio.run(query.acount()) == query.count() == expected
    assert backend.events.count("start") == 1


class RecoveringBackend:
    """Catches the API error thrown into it and carries on."""

    def __init__(self) -> None:
        self.caught: list[BaseException] = []

    def run(self, query: Query[Any]) -> Generator[Any, Any, None]:
        try:
            yield Request("GET", "/fails")
        except APIError as exc:
            self.caught.append(exc)
            yield [Remote(n=-1)]

    def count(self, query: Query[Any]) -> Generator[Request, Response, int]:
        try:
            yield Request("GET", "/fails")
        except APIError as exc:
            self.caught.append(exc)
            return 42
        raise AssertionError("unreachable")

    def explain(self, query: Query[Any]) -> Explanation:
        return Explanation("recovering")


class TranslatingBackend(RecoveringBackend):
    """Translates the API error thrown into it."""

    def run(self, query: Query[Any]) -> Generator[Any, Any, None]:
        try:
            yield Request("GET", "/fails")
        except APIError as exc:
            raise QueryError("translated") from exc


def test_send_errors_are_thrown_into_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    error = APIError("boom", status_code=500)
    client = FakeClient(fail=error)
    recovering = RecoveringBackend()
    monkeypatch.setattr(Remote, "_backend", recovering, raising=False)
    query = Remote.select().using(client)
    assert ns(query.all()) == [-1]
    assert query.offset(40).count() == 2
    assert recovering.caught == [error, error]

    monkeypatch.setattr(Remote, "_backend", TranslatingBackend(), raising=False)
    with pytest.raises(QueryError, match="translated") as info:
        Remote.select().using(client).all()
    assert info.value.__cause__ is error


def test_uncaught_send_errors_propagate_and_close_the_backend(backend: PagedBackend) -> None:
    error = APIError("boom", status_code=500)
    with pytest.raises(APIError) as info:
        Remote.select().using(FakeClient(fail=error)).all()
    assert info.value is error
    assert backend.events == ["start", "closed"]


def test_async_send_errors_are_thrown_into_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    error = APIError("boom", status_code=500)
    client = FakeClient(fail=error)
    recovering = RecoveringBackend()
    monkeypatch.setattr(Remote, "_backend", recovering, raising=False)

    async def main() -> None:
        assert ns(await Remote.select().using(client).aall()) == [-1]
        assert await Remote.select().using(client).acount() == 42
        monkeypatch.setattr(Remote, "_backend", TranslatingBackend(), raising=False)
        with pytest.raises(QueryError, match="translated") as info:
            await Remote.select().using(client).aall()
        assert info.value.__cause__ is error

    asyncio.run(main())
    assert recovering.caught == [error, error]


def test_default_client_is_resolved_only_when_a_request_is_sent(
    backend: PagedBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    client = FakeClient()

    def default() -> FakeClient:
        calls.append(1)
        return client

    monkeypatch.setattr(ads.client, "get_default_client", default)
    query = Remote.select().limit(1)
    repr(query)
    query.explain()
    assert calls == []
    assert ns(query.all()) == [0]
    assert len(calls) == 1
    assert Remote.select().count() == 5
    assert asyncio.run(Remote.select().aall())[-1].n == 4
    assert len(calls) == len(client.sent) == 5


def test_default_client_errors_are_raised(backend: PagedBackend, no_default_client: None) -> None:
    with pytest.raises(AssertionError, match="must not be needed"):
        Remote.select().all()
    with pytest.raises(AssertionError, match="must not be needed"):
        asyncio.run(Remote.select().acount())


def test_using_selects_the_client(backend: PagedBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    default, explicit = FakeClient(), FakeClient()
    monkeypatch.setattr(ads.client, "get_default_client", lambda: default)
    query = Remote.select().using(explicit)
    assert query.client is explicit
    query.all()
    assert (len(explicit.sent), len(default.sent)) == (3, 0)
    query.using(None).all()
    assert (len(explicit.sent), len(default.sent)) == (3, 3)


def test_model_without_backend_fails_at_execution() -> None:
    query = Query(Bare).where(Bare.name == "x")
    assert "Bare" in repr(query)
    for run in (query.all, query.count, query.first, query.explain, lambda: asyncio.run(query.aall())):
        with pytest.raises(TypeError, match="Bare has no backend"):
            run()


def test_backend_yielding_junk_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Junk:
        def run(self, query: Query[Any]) -> Generator[Any, Any, None]:
            yield (Remote(n=1),)

        def count(self, query: Query[Any]) -> Generator[Any, Any, Any]:
            yield [Remote(n=1)]
            return 1

    monkeypatch.setattr(Remote, "_backend", Junk(), raising=False)
    with pytest.raises(TypeError, match="expected a Request or a list"):
        Remote.select().all()
    with pytest.raises(TypeError, match="must only yield Requests"):
        Remote.select().count()


def test_repr_does_not_execute(backend: PagedBackend, client: FakeClient) -> None:
    query = (
        Remote.select("n").using(client).where(Remote.n > 1).order_by(Remote.n.desc()).offset(1).limit(2)
    )
    text = repr(query)
    assert client.sent == [] and backend.events == []
    for part in ("<Query Remote", "fields n", "where Range", "order by n desc", "offset 1", "limit 2"):
        assert part in text
    assert repr(Remote.select()) == "<Query Remote>"
    query.all()
    assert "2 rows cached" in repr(query)


def test_explain_delegates_to_the_backend(backend: PagedBackend, client: FakeClient) -> None:
    explanation = Remote.select().using(client).explain()
    assert explanation.summary == "fake plan"
    assert client.sent == []


def test_explanation_str() -> None:
    explanation = Explanation(
        "Search ADS",
        (
            Request("GET", "/search/query", params={"q": "year:2020", "rows": 10, "skip": None}),
            Request("POST", "/search/bigquery", params={"q": "*:*"}, content="bibcode\nX"),
            Request("post", "/biblib/libraries", json={"name": "é"}),
        ),
        ("first note", "second note"),
    )
    assert str(explanation) == (
        "Search ADS\n"
        "GET /search/query\n"
        "    q = year:2020\n"
        "    rows = 10\n"
        "POST /search/bigquery\n"
        "    q = *:*\n"
        "    (body: 9 bytes)\n"
        "POST /biblib/libraries\n"
        "    (body: 18 bytes)\n"
        "- first note\n"
        "- second note"
    )
    assert str(Explanation("Nothing to do")) == "Nothing to do"


# --- the drivers directly ------------------------------------------------------


def _script(log: list[Any]) -> Generator[Any, Any, str]:
    try:
        reply = yield Request("GET", "/a")
        yield [reply.data]
        yield []
        return "done"
    finally:
        log.append("closed")


def _echo(request: Request) -> Response:
    return Response(200, {}, request.path)


async def _aecho(request: Request) -> Response:
    return _echo(request)


def test_drive_sends_requests_forwards_batches_and_returns_the_value() -> None:
    log: list[Any] = []
    assert _exhaust(drive(_script(log), _echo)) == ([["/a"], []], "done")
    assert log == ["closed"]


def test_drive_closes_the_backend_generator_on_early_exit() -> None:
    log: list[Any] = []
    driver = drive(_script(log), _echo)
    assert next(driver) == ["/a"]
    driver.close()
    assert log == ["closed"]


def test_adrive_mirrors_drive() -> None:
    async def main() -> None:
        log: list[Any] = []
        assert [batch async for batch in adrive(_script(log), _aecho)] == [["/a"], []]
        assert log == ["closed"]
        async with aclosing(adrive(_script(log), _aecho)) as driver:
            async for batch in driver:
                assert batch == ["/a"]
                break
        assert log == ["closed", "closed"]

    asyncio.run(main())


def test_driver_throws_send_errors_into_the_generator() -> None:
    error = APIError("boom")

    def failing(request: Request) -> Response:
        raise error

    def gen() -> Generator[Any, Any, str]:
        try:
            yield Request("GET", "/a")
        except APIError as exc:
            return f"handled {exc}"
        return "not thrown"

    assert _exhaust(drive(gen(), failing)) == ([], "handled boom")


# --- async parity ----------------------------------------------------------------


def test_async_iteration_streams_batches(backend: PagedBackend) -> None:
    log: list[Any] = []
    client = FakeClient(log=log)

    async def main() -> None:
        async for row in Remote.select().using(client).limit(3):
            log.append(("row", row.n))

    asyncio.run(main())
    assert log == [
        ("send", {"start": 0, "rows": 2}),
        ("row", 0),
        ("row", 1),
        ("send", {"start": 2, "rows": 1}),
        ("row", 2),
    ]


def test_sync_and_async_share_the_cache(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)
    assert ns(asyncio.run(query.aall())) == [0, 1, 2, 3, 4]
    assert len(client.sent) == 3
    assert ns(list(query)) == [0, 1, 2, 3, 4]
    assert query.count() == 5

    other = Remote.select().using(client).offset(1)
    assert ns(other.all()) == [1, 2, 3, 4]
    assert len(client.sent) == 6

    async def main() -> None:
        assert ns(await other.aall()) == [1, 2, 3, 4]
        assert ns([row async for row in other]) == [1, 2, 3, 4]
        assert await other.acount() == 4
        assert (await other.afirst()).n == 1
        assert (await other.aget()).n == 1
        assert await other.aexists()

    asyncio.run(main())
    assert len(client.sent) == 6


def test_async_partial_iteration_caches_nothing(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)

    async def main() -> None:
        async with aclosing(query.__aiter__()) as rows:  # type: ignore[type-var]
            async for row in rows:
                assert row.n == 0
                break
        assert backend.events == ["start", "closed"]
        assert len(client.sent) == 1
        assert ns(await query.aall()) == [0, 1, 2, 3, 4]
        assert len(client.sent) == 4

    asyncio.run(main())


def test_async_concurrent_iterations_are_independent(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)

    async def main() -> tuple[list[Remote], list[Remote]]:
        return await asyncio.gather(query.aall(), query.aall())

    first, second = asyncio.run(main())
    assert ns(first) == ns(second) == [0, 1, 2, 3, 4]
    assert len(client.sent) == 6
    sent_order = [(r.params or {}).get("start") for r in client.sent]
    assert sent_order == [0, 0, 2, 2, 4, 4]


def test_async_first_get_exists_count_requests(backend: PagedBackend, client: FakeClient) -> None:
    query = Remote.select().using(client)

    async def main() -> None:
        assert (await query.offset(3).afirst()).n == 3
        assert await query.limit(0).afirst() is None
        with pytest.raises(Remote.DoesNotExist):
            await query.offset(5).aget()
        assert await query.aexists()
        assert not await query.offset(9).aexists()
        assert await query.offset(1).limit(2).acount() == 2

    asyncio.run(main())
    assert [r.params for r in client.sent] == [
        {"start": 3, "rows": 1},
        {"start": 5, "rows": 1},
        {"start": 0, "rows": 1},
        {"start": 9, "rows": 1},
        {"rows": 0},
    ]


def test_truthiness_fetches_at_most_one_row():
    """bool(query) must not fall back to __len__, which would fetch every row."""
    calls = []

    class Counting:
        def run(self, query):
            calls.append(query.limit_)
            yield [object()][: query.limit_]

        def count(self, query):
            raise AssertionError("not used")
            yield

        def explain(self, query):
            raise AssertionError("not used")

    class Thing(Model):
        _key = "name"
        _backend = Counting()
        name = StringField()

    assert bool(Thing.select()) is True
    assert calls == [1]
    assert bool(Thing.select().limit(0)) is False
    assert calls == [1]


def test_explanation_shows_booleans_as_they_are_sent():
    text = str(Explanation("s", (Request("GET", "/x", params={"flag": True, "n": 3}),)))
    assert "flag = true" in text
    assert "n = 3" in text


def test_select_rows_and_filter_rows_apply_a_query_in_python():
    class Row(Model):
        _key = "name"
        name = StringField()
        size = IntegerField()

    rows = [Row(name=n, size=s) for n, s in [("a", 3), ("b", 1), ("c", 2), ("d", 5)]]
    query = Query(Row).where(Row.size > 1).order_by(Row.size.desc()).offset(1).limit(2)
    assert [r.name for r in filter_rows(rows, query)] == ["a", "c", "d"]
    assert [r.name for r in select_rows(rows, query)] == ["a", "c"]
    assert [r.name for r in select_rows(rows, Query(Row))] == ["a", "b", "c", "d"]
