"""Queries over models, the sans-IO backend protocol, and the drivers that run it.

A :class:`Query` is an immutable description of what to fetch. Executing it asks
the model's backend for a generator that yields :class:`~scixplorer.client.Request`
objects (to be sent, with the :class:`~scixplorer.client.Response` sent back in) and
lists of model instances (batches of results). The drivers :func:`drive` and
:func:`adrive` run such a generator with a sync or async ``send`` callable, so
pagination and merging logic is written once in the backend and works both ways.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator, Iterable, Iterator
from contextlib import aclosing, closing
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, overload

from . import client as _http
from . import evaluate
from .client import Request, Response
from .expr import Expr, Order
from .fields import Field, FieldExpr
from .model import Model

__all__ = [
    "Backend",
    "Explanation",
    "MemoryBackend",
    "Query",
    "Sender",
    "adrive",
    "drive",
    "filter_rows",
    "select_rows",
]

M = TypeVar("M", bound=Model)


@dataclass(frozen=True)
class Explanation:
    """What executing a query would do.

    ``requests`` are the first request(s) that would be sent (empty if the query
    is answered without any), and ``notes`` are further details worth showing.
    """

    summary: str
    requests: tuple[Request, ...] = ()
    notes: tuple[str, ...] = ()

    def __str__(self) -> str:
        lines = [self.summary]
        for request in self.requests:
            lines.append(f"{request.method.upper()} {request.path}")
            for key, value in (request.params or {}).items():
                if value is not None:
                    shown = str(value).lower() if isinstance(value, bool) else value
                    lines.append(f"    {key} = {shown}")
            size = _body_size(request)
            if size is not None:
                lines.append(f"    (body: {size} bytes)")
        lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines)


def _body_size(request: Request) -> int | None:
    if request.content is not None:
        content = request.content
        return len(content.encode("utf-8") if isinstance(content, str) else content)
    if request.json is not None:
        return len(json.dumps(request.json).encode("utf-8"))
    return None


class Sender(Protocol):
    """What a query needs from a client to send requests; :class:`~scixplorer.client.Client` is one."""

    def send(self, request: Request) -> Response:
        """Send a request and return its response."""
        ...

    async def asend(self, request: Request) -> Response:
        """Send a request asynchronously and return its response."""
        ...


class Backend(Protocol):
    """Executes queries for a model without performing any I/O itself."""

    def run(self, query: Query[Any]) -> Generator[Request | list[Any], Response | None, None]:
        """Yield a Request to have it executed (the Response is sent back in), or
        yield a list of model instances (a batch of results; None is sent back)."""
        ...

    def count(self, query: Query[Any]) -> Generator[Request, Response, int]:
        """Yield Requests as ``run`` does and return the number of matching rows,
        ignoring the query's offset and limit."""
        ...

    def explain(self, query: Query[Any]) -> Explanation:
        """Describe how the query would be executed."""
        ...


def drive(gen: Generator[Any, Any, Any], send: Callable[[Request], Response]) -> Generator[Any, None, Any]:
    """Run a backend generator synchronously.

    Each yielded :class:`Request` is passed to ``send`` and the response sent
    back in; if ``send`` raises, the exception is thrown into the generator so
    the backend can clean up or translate it. Every other yielded item (a batch
    of rows) is yielded to the caller. Returns the generator's return value.
    Closing this generator closes the backend generator.
    """
    try:
        reply: Response | None = None
        error: Exception | None = None
        while True:
            try:
                item = gen.send(reply) if error is None else gen.throw(error)
            except StopIteration as stop:
                return stop.value
            reply = error = None
            if isinstance(item, Request):
                try:
                    reply = send(item)
                except Exception as exc:
                    error = exc
            else:
                yield item
    finally:
        gen.close()


async def adrive(
    gen: Generator[Any, Any, Any], asend: Callable[[Request], Awaitable[Response]]
) -> AsyncGenerator[Any, None]:
    """Run a backend generator asynchronously; the async counterpart of :func:`drive`.

    Async generators cannot return a value, so the backend generator's return
    value is discarded. To receive it, wrap the backend generator in one that
    yields the value as its last item, as :meth:`Query.acount` does.
    """
    try:
        reply: Response | None = None
        error: Exception | None = None
        while True:
            try:
                item = gen.send(reply) if error is None else gen.throw(error)
            except StopIteration:
                return
            reply = error = None
            if isinstance(item, Request):
                try:
                    reply = await asend(item)
                except Exception as exc:
                    error = exc
            else:
                yield item
    finally:
        gen.close()


@dataclass(frozen=True)
class _Returned:
    """The return value of a backend generator, yielded as its final item."""

    value: Any


def _returning(gen: Generator[Any, Any, Any]) -> Generator[Any, Any, None]:
    """Re-yield everything ``gen`` yields, then yield its return value as :class:`_Returned`."""
    value = yield from gen
    yield _Returned(value)


def _total(item: Any, backend: Any) -> int:
    """Unwrap the first item of a ``_returning(backend.count(...))`` run, which must be its result."""
    if isinstance(item, _Returned):
        value = item.value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise TypeError(f"{backend!r}.count() must only yield Requests and return an int; got {item!r}")


def _describe_order(orders: Iterable[Order]) -> str:
    return ", ".join(f"{o.field.name} {'desc' if o.descending else 'asc'}" for o in orders)


class Query(Generic[M]):
    """An immutable query over a model; build it with :meth:`Model.select`.

    Builder methods (:meth:`where`, :meth:`order_by`, :meth:`limit`,
    :meth:`offset`, :meth:`using`) return a new query. Iterating streams results
    as the backend produces them; once an iteration completes, the results are
    cached on this query, and later iterations, :meth:`all`, :meth:`count`,
    ``len()`` and indexing make no further requests.
    """

    __slots__ = ("_model", "_fields", "_expr", "_order", "_limit", "_offset", "_client", "_rows")

    def __init__(self, model: type[M], fields: Iterable[str | Field[Any, Any] | FieldExpr] = ()) -> None:
        if not (isinstance(model, type) and issubclass(model, Model)):
            raise TypeError(f"Query needs a Model subclass, not {model!r}")
        self._model = model
        self._fields: tuple[Field[Any, Any], ...] = tuple(model._field(field) for field in fields)
        self._expr: Expr | None = None
        self._order: tuple[Order, ...] = ()
        self._limit: int | None = None
        self._offset = 0
        self._client: Sender | None = None
        self._rows: tuple[M, ...] | None = None

    @property
    def model(self) -> type[M]:
        """The model being queried."""
        return self._model

    @property
    def fields(self) -> tuple[Field[Any, Any], ...]:
        """The fields to fetch; empty means all stored fields."""
        return self._fields

    @property
    def expr(self) -> Expr | None:
        """The filter expression, or ``None`` to match everything."""
        return self._expr

    @property
    def order(self) -> tuple[Order, ...]:
        """The sort keys, most significant first."""
        return self._order

    @property
    def limit_(self) -> int | None:
        """The maximum number of rows, or ``None`` for no limit."""
        return self._limit

    @property
    def offset_(self) -> int:
        """The number of leading rows to skip."""
        return self._offset

    @property
    def client(self) -> Sender | None:
        """The client requests are sent with; ``None`` means the default client."""
        return self._client

    def _replace(self, **changes: Any) -> Query[M]:
        new = copy.copy(self)
        for name, value in changes.items():
            setattr(new, "_" + name, value)
        new._rows = None
        return new

    def where(self, *exprs: Expr) -> Query[M]:
        """Return a query also requiring every one of ``exprs`` (AND-ed with any existing filter)."""
        for expr in exprs:
            if isinstance(expr, bool):
                raise TypeError(
                    f"where() got the bool {expr!r} instead of an expression. This usually "
                    "means and/or/not, `is` or a comparison between plain values was used: "
                    "combine expressions with & | ~ and wrap each comparison in parentheses, "
                    "e.g. .where((Journal.title == 'x') | (Journal.abbreviation == 'y'))."
                )
            if not isinstance(expr, Expr):
                raise TypeError(f"where() needs expressions, not {expr!r}")
        combined = self._expr
        for expr in exprs:
            combined = expr if combined is None else combined & expr
        return self._replace(expr=combined)

    def order_by(self, *keys: Order | str | Field[Any, Any] | FieldExpr) -> Query[M]:
        """Return a query sorted by ``keys`` (replacing any previous ordering).

        Each key is an :class:`~scixplorer.expr.Order` (from ``.asc()``/``.desc()``) or a
        field handle or name, meaning ascending. No keys removes the ordering.
        """
        orders = []
        for key in keys:
            if isinstance(key, Order):
                orders.append(Order(self._model._field(key.field), key.descending))
            else:
                orders.append(Order(self._model._field(key)))
        return self._replace(order=tuple(orders))

    def limit(self, n: int | None) -> Query[M]:
        """Return a query yielding at most ``n`` rows (``None`` for no limit)."""
        if n is not None:
            _check_count("limit", n)
        return self._replace(limit=n)

    def offset(self, n: int) -> Query[M]:
        """Return a query skipping the first ``n`` rows."""
        _check_count("offset", n)
        return self._replace(offset=n)

    def using(self, client: Sender | None) -> Query[M]:
        """Return a query sending requests with ``client`` (``None`` for the default client)."""
        return self._replace(client=client)

    def _backend(self) -> Backend:
        backend: Backend | None = getattr(self._model, "_backend", None)
        if backend is None:
            raise TypeError(f"{self._model.__name__} has no backend, so it cannot be queried")
        return backend

    def _resolve_client(self) -> Sender:
        return self._client if self._client is not None else _http.get_default_client()

    def _send(self, request: Request) -> Response:
        return self._resolve_client().send(request)

    async def _asend(self, request: Request) -> Response:
        return await self._resolve_client().asend(request)

    def _check_batch(self, batch: Any) -> list[M]:
        if not isinstance(batch, list):
            raise TypeError(f"{self._backend()!r} yielded {batch!r}; expected a Request or a list of rows")
        return batch

    def __iter__(self) -> Iterator[M]:
        return self._stream()

    def _stream(self) -> Iterator[M]:
        # The cache is checked when iteration starts, not when the iterator is
        # created: list(query) creates the iterator, then calls len(query) for a
        # size hint, which runs and caches the query; the iterator then reuses it.
        if self._rows is not None:
            yield from self._rows
            return
        rows: list[M] = []
        with closing(drive(self._backend().run(self), self._send)) as batches:
            for batch in batches:
                batch = self._check_batch(batch)
                rows.extend(batch)
                yield from batch
        self._rows = tuple(rows)

    def __aiter__(self) -> AsyncIterator[M]:
        return self._astream()

    async def _astream(self) -> AsyncIterator[M]:
        if self._rows is not None:
            for row in self._rows:
                yield row
            return
        rows: list[M] = []
        async with aclosing(adrive(self._backend().run(self), self._asend)) as batches:
            async for batch in batches:
                batch = self._check_batch(batch)
                rows.extend(batch)
                for row in batch:
                    yield row
        self._rows = tuple(rows)

    def all(self) -> list[M]:
        """All rows, as a new list."""
        return list(iter(self))

    async def aall(self) -> list[M]:
        """Asynchronous :meth:`all`."""
        return [row async for row in self]

    def _first_query(self) -> Query[M] | None:
        """The query that fetches only the first row, or ``None`` if there can be none."""
        return None if self._limit == 0 else self.limit(1)

    def first(self) -> M | None:
        """The first row, or ``None``. Fetches at most one row unless already cached."""
        if self._rows is not None:
            return self._rows[0] if self._rows else None
        query = self._first_query()
        rows = [] if query is None else query.all()
        return rows[0] if rows else None

    async def afirst(self) -> M | None:
        """Asynchronous :meth:`first`."""
        if self._rows is not None:
            return self._rows[0] if self._rows else None
        query = self._first_query()
        rows = [] if query is None else await query.aall()
        return rows[0] if rows else None

    def _require(self, row: M | None) -> M:
        if row is None:
            raise self._model.DoesNotExist(f"No {self._model.__name__} matched {self!r}")
        return row

    def get(self) -> M:
        """The first row; raises ``model.DoesNotExist`` if there is none."""
        return self._require(self.first())

    async def aget(self) -> M:
        """Asynchronous :meth:`get`."""
        return self._require(await self.afirst())

    def exists(self) -> bool:
        """Whether there is at least one row. Fetches at most one row unless cached."""
        return self.first() is not None

    async def aexists(self) -> bool:
        """Asynchronous :meth:`exists`."""
        return await self.afirst() is not None

    def _window(self, total: int) -> int:
        """Apply offset and limit to the backend's total number of matches."""
        n = max(0, total - self._offset)
        return n if self._limit is None else min(n, self._limit)

    def count(self) -> int:
        """The number of rows iteration would yield, asking the backend unless cached."""
        if self._rows is not None:
            return len(self._rows)
        backend = self._backend()
        with closing(drive(_returning(backend.count(self)), self._send)) as items:
            return self._window(_total(next(items), backend))

    async def acount(self) -> int:
        """Asynchronous :meth:`count`."""
        if self._rows is not None:
            return len(self._rows)
        backend = self._backend()
        async with aclosing(adrive(_returning(backend.count(self)), self._asend)) as items:
            return self._window(_total(await anext(items), backend))

    def __len__(self) -> int:
        return len(self._fetched())

    def __bool__(self) -> bool:
        """Whether there is at least one row; like :meth:`exists`, fetches at most one."""
        return self.exists()

    def _fetched(self) -> tuple[M, ...]:
        return self._rows if self._rows is not None else tuple(self._stream())

    @overload
    def __getitem__(self, index: int) -> M: ...
    @overload
    def __getitem__(self, index: slice) -> list[M]: ...
    def __getitem__(self, index: int | slice) -> M | list[M]:
        rows = self._fetched()
        if isinstance(index, slice):
            return list(rows[index])
        return rows[index]

    def explain(self) -> Explanation:
        """Describe how this query would be executed, without executing it."""
        return self._backend().explain(self)

    def __repr__(self) -> str:
        parts = []
        if self._fields:
            parts.append("fields " + ", ".join(field.name for field in self._fields))
        if self._expr is not None:
            parts.append(f"where {self._expr!r}")
        if self._order:
            parts.append("order by " + _describe_order(self._order))
        if self._offset:
            parts.append(f"offset {self._offset}")
        if self._limit is not None:
            parts.append(f"limit {self._limit}")
        if self._client is not None:
            parts.append(f"using {self._client!r}")
        if self._rows is not None:
            parts.append(f"{len(self._rows)} rows cached")
        return f"<Query {self._model.__name__}{': ' if parts else ''}{'; '.join(parts)}>"


def _check_count(name: str, n: Any) -> None:
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError(f"{name}() needs an int, not {n!r}")
    if n < 0:
        raise ValueError(f"{name}() needs a non-negative int, not {n}")


def filter_rows(rows: Iterable[M], query: Query[M]) -> list[M]:
    """The ``rows`` that satisfy the query's filter, evaluated in Python."""
    if query.expr is None:
        return list(rows)
    test = evaluate.predicate(query.expr)
    return [row for row in rows if test(row)]


def select_rows(rows: Iterable[M], query: Query[M]) -> list[M]:
    """Apply the query's filter, ordering, offset and limit to ``rows`` in Python.

    This is what a backend holding its whole collection in memory yields.
    """
    selected = filter_rows(rows, query)
    if query.order:
        selected = evaluate.sort_rows(selected, query.order)
    start = query.offset_
    stop = None if query.limit_ is None else start + query.limit_
    return selected[start:stop]


class MemoryBackend(Generic[M]):
    """Evaluates queries in Python over a collection held in memory.

    :param loader: zero-argument callable returning the whole collection. It is
        called on every execution, so it should cache its result.
    :param key: name of a field with unique values, enabling :meth:`lookup`.

    The query's ``fields`` are ignored: rows are returned whole.
    """

    def __init__(self, loader: Callable[[], Iterable[M]], *, key: str | None = None):
        self._loader = loader
        self._key = key
        self._index: dict[Any, M] | None = None

    def __repr__(self) -> str:
        return f"<MemoryBackend {getattr(self._loader, '__qualname__', self._loader)!r}>"

    def run(self, query: Query[M]) -> Generator[Request | list[M], Response | None, None]:
        """Yield the matching rows, ordered, offset and limited, as a single batch."""
        yield select_rows(self._loader(), query)

    def count(self, query: Query[M]) -> Generator[Request, Response, int]:
        """Return the number of matching rows, ignoring offset and limit."""
        yield from ()
        return len(filter_rows(self._loader(), query))

    def explain(self, query: Query[M]) -> Explanation:
        """Say that the query is evaluated locally, over how many rows."""
        total = sum(1 for _ in self._loader())
        notes = [f"filter: {query.expr!r}" if query.expr is not None else "filter: none (all rows)"]
        if query.order:
            notes.append(f"order: {_describe_order(query.order)}")
        return Explanation(
            f"Evaluated locally over {total} {query.model.__name__} rows; no requests are made.",
            notes=tuple(notes),
        )

    def lookup(self, value: Any) -> M | None:
        """The row whose ``key`` field equals ``value``, or ``None``.

        The index is built on first use; if several rows share a key, the first wins.
        """
        if self._key is None:
            raise TypeError(f"{self!r} was created without key=..., so it does not support lookup()")
        if self._index is None:
            index: dict[Any, M] = {}
            for row in self._loader():
                index.setdefault(getattr(row, self._key), row)
            self._index = index
        return self._index.get(value)

    def clear(self) -> None:
        """Drop the lookup index, so the next :meth:`lookup` rebuilds it from the loader.

        Call this when the data the loader returns has changed.
        """
        self._index = None
