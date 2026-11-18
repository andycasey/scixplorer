"""Running the searches of the ``scix`` command: one request gives a page of documents and their total."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..client import Request, Response, get_default_client
from ..models.document import Document
from ..query import Query, Sender
from .interpret import Interpretation
from .output import FIELDS, SORTS

__all__ = ["Found", "Session"]


@dataclass(frozen=True)
class Found:
    """The answer to a search: the documents fetched, and how many match in all.

    ``interpretation`` is the reading that was searched. It is the wider one
    (``widened``) when the search as first read matched nothing.
    """

    interpretation: Interpretation
    documents: list[Document]
    total: int
    widened: bool = False


class _Counting:
    """Sends requests with a client, and remembers how many documents the last search matched."""

    def __init__(self, client: Sender | None) -> None:
        self._given = client
        self.found: int | None = None

    @property
    def client(self) -> Sender:
        return self._given if self._given is not None else get_default_client()

    def _note(self, request: Request, response: Response) -> None:
        if not request.path.startswith("/search/"):
            return
        body: Any = response.data.get("response") if isinstance(response.data, dict) else None
        found = body.get("numFound") if isinstance(body, dict) else None
        if isinstance(found, int) and not isinstance(found, bool):
            self.found = found

    def send(self, request: Request) -> Response:
        response = self.client.send(request)
        self._note(request, response)
        return response

    async def asend(self, request: Request) -> Response:
        response = await self.client.asend(request)
        self._note(request, response)
        return response


class Session:
    """Runs searches with one client (``None`` for the default client)."""

    def __init__(self, client: Sender | None = None) -> None:
        self._sender = _Counting(client)

    @property
    def client(self) -> Sender:
        """The client requests are sent with."""
        return self._sender.client

    def query(self, interpretation: Interpretation, sort: str) -> Query[Document]:
        """The document query for a search as it was read, in the order ``sort`` names."""
        expr = interpretation.expr
        if expr is None:
            raise ValueError("there is nothing to search for")
        field, descending, _ = SORTS[sort]
        handle = getattr(Document, field)
        return Document.select(*FIELDS).where(expr).order_by(handle.desc() if descending else handle.asc())

    def fetch(
        self, interpretation: Interpretation, sort: str, *, limit: int | None, offset: int = 0
    ) -> tuple[list[Document], int]:
        """``limit`` documents from position ``offset``, and the number that match in all."""
        self._sender.found = None
        query = self.query(interpretation, sort).offset(offset).limit(limit).using(self._sender)
        documents = query.all()
        found = self._sender.found
        return documents, max(found if found is not None else 0, offset + len(documents))

    def bibcodes(self, interpretation: Interpretation, sort: str, *, limit: int | None) -> tuple[list[str], int]:
        """The bibcodes of the first ``limit`` documents, and the number that match in all.

        Nothing but the bibcodes is fetched, so this is the way to hand many
        documents to a service.
        """
        self._sender.found = None
        query = self.query(interpretation, sort)
        fetched = Document.select(Document.bibcode).where(*([query.expr] if query.expr is not None else []))
        documents = fetched.order_by(*query.order).limit(limit).using(self._sender).all()
        found = self._sender.found
        bibcodes = [document.bibcode for document in documents if document.bibcode]
        return bibcodes, max(found if found is not None else 0, len(documents))

    def find(self, interpretation: Interpretation, sort: str, *, limit: int | None) -> Found:
        """Search, and if nothing matches, search again with the wider reading if there is one."""
        documents, total = self.fetch(interpretation, sort, limit=limit)
        if total == 0:
            wider = interpretation.relaxed()
            if wider is not None:
                documents, total = self.fetch(wider, sort, limit=limit)
                if total:
                    return Found(wider, documents, total, widened=True)
        return Found(interpretation, documents, total)
