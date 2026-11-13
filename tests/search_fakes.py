"""A fake ADS search API for document tests: an in-memory index served over httpx.MockTransport."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx

from scixplorer.client import Client

PREFIX = "/v1"
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_IDENTIFIER_QUERY = re.compile(r"(bibcode|scix_id|id):")


def bibcodes(n: int, prefix: str = "2020ApJ...") -> list[str]:
    """``n`` distinct 19-character bibcodes."""
    return [f"{prefix}{i:08d}A" for i in range(n)]


def make_docs(n: int, **fields: Any) -> list[dict[str, Any]]:
    """``n`` documents with bibcode, id, title and citation_count (plus ``fields``)."""
    return [
        {"bibcode": b, "id": str(1000 + i), "title": [f"Paper {i}"], "citation_count": i, **fields}
        for i, b in enumerate(bibcodes(n))
    ]


class FakeSearch:
    """Serves ``/search/query``, ``/search/bigquery`` and library listings, recording every request.

    The query string is not evaluated, except a leading ``bibcode:(...)`` or
    ``bibcode:"..."`` (or the same on ``scix_id`` or ``id``) which restricts the
    documents (as lazy loading sends).
    A bigquery restricts to the uploaded bibcodes. Only fields named in ``fl``
    are returned. ``reply`` replaces the search response entirely.
    """

    def __init__(
        self,
        docs: list[dict[str, Any]] | None = None,
        libraries: dict[str, list[str]] | None = None,
    ) -> None:
        self.docs = docs or []
        self.libraries = libraries or {}
        self.requests: list[httpx.Request] = []
        self.reply: Callable[[httpx.Request], httpx.Response] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(PREFIX)
        if path.startswith("/biblib/libraries/"):
            return self._library(request, path.rsplit("/", 1)[1])
        if path in ("/search/query", "/search/bigquery"):
            if self.reply is not None:
                return self.reply(request)
            return self._search(request, path)
        return httpx.Response(599, json={"error": f"unexpected {request.method} {path}"})

    def _library(self, request: httpx.Request, library_id: str) -> httpx.Response:
        members = self.libraries[library_id]
        start = int(request.url.params.get("start", 0))
        rows = int(request.url.params.get("rows", 20))
        return httpx.Response(
            200,
            json={
                "documents": members[start : start + rows],
                "metadata": {"id": library_id, "num_documents": len(members)},
            },
        )

    def _search(self, request: httpx.Request, path: str) -> httpx.Response:
        params = request.url.params
        docs = self.docs
        q = params["q"]
        identifier = _IDENTIFIER_QUERY.match(q)
        if identifier is not None:
            wanted = {m.group(1) for m in _QUOTED.finditer(q)}
            docs = [doc for doc in docs if doc.get(identifier.group(1)) in wanted]
        if path == "/search/bigquery":
            uploaded = set(request.content.decode().split("\n")[1:])
            docs = [doc for doc in docs if doc.get("bibcode") in uploaded]
        start, rows = int(params["start"]), int(params["rows"])
        fields = params["fl"].split(",")
        page = [{k: v for k, v in doc.items() if k in fields} for doc in docs[start : start + rows]]
        return httpx.Response(200, json={"response": {"numFound": len(docs), "start": start, "docs": page}})

    def client(self) -> Client:
        transport = httpx.MockTransport(self)
        return Client(token="t", transport=transport, async_transport=transport, sleep=lambda _: None)

    @property
    def searches(self) -> list[httpx.Request]:
        """The search requests (not library requests), in order."""
        return [r for r in self.requests if r.url.path.removeprefix(PREFIX).startswith("/search/")]

    def params(self, index: int = -1) -> dict[str, str]:
        """The query parameters of the ``index``-th search request."""
        return dict(self.searches[index].url.params)

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path.removeprefix(PREFIX)) for r in self.requests]
