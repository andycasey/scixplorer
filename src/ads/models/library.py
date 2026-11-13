"""ADS libraries ("biblib"): collections of documents curated by ADS users.

Every interaction is first described without I/O: a private function builds the
:class:`~ads.client.Request`, or a generator in the sans-IO backend protocol of
:mod:`ads.query` yields the requests and returns the result
(:func:`fetch_library`, :func:`fetch_bibcodes`). The public methods of
:class:`Library` only send those requests and update the object from the
responses, one API call per method.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator, Mapping
from typing import Any, TypeVar
from urllib.parse import quote

from ..client import Client, Request, Response, get_default_client
from ..exceptions import ADSError, APIError
from ..expr import And, Expr, In, Match
from ..fields import BooleanField, DateTimeField, Field, IntegerField, StringField
from ..model import Model
from ..query import Explanation, Query, drive, filter_rows, select_rows
from ..utils import _require_bibcodes, _response_field, to_bibcodes

__all__ = ["DEFAULT_PAGE_SIZE", "Library", "LibraryBackend", "fetch_bibcodes", "fetch_library"]

T = TypeVar("T")

#: Bibcodes requested per page by :func:`fetch_bibcodes`.
DEFAULT_PAGE_SIZE = 2000

_LIBRARIES = "/biblib/libraries"


# Requests. Each is a pure function of its arguments.


def _path(prefix: str, library_id: str) -> str:
    """``/biblib/<prefix>/<library_id>``, with the id percent-encoded as one path segment."""
    return f"/biblib/{prefix}/{quote(library_id, safe='')}"


def _list_request() -> Request:
    """List the caller's libraries."""
    return Request("GET", _LIBRARIES)


def _get_request(library_id: str, *, start: int | None = None, rows: int | None = None) -> Request:
    """Fetch one library's metadata and (a page of) its bibcodes."""
    params = {key: value for key, value in (("start", start), ("rows", rows)) if value is not None}
    return Request("GET", _path("libraries", library_id), params=params or None)


def _create_request(
    name: str | None, description: str | None, public: bool, bibcodes: list[str]
) -> Request:
    """Create a library. ``None`` name or description are left for ADS to choose."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    payload["public"] = public
    if bibcodes:
        payload["bibcode"] = bibcodes
    return Request("POST", _LIBRARIES, json=payload)


def _update_request(library_id: str, changes: Mapping[str, Any]) -> Request:
    """Change a library's name, description or visibility (note: ``documents``, not ``libraries``)."""
    return Request("PUT", _path("documents", library_id), json=dict(changes))


def _documents_request(library_id: str, action: str, bibcodes: list[str]) -> Request:
    """Add (``action="add"``) or remove (``"remove"``) documents."""
    return Request("POST", _path("documents", library_id), json={"bibcode": bibcodes, "action": action})


def _delete_request(library_id: str) -> Request:
    """Delete a library."""
    return Request("DELETE", _path("documents", library_id))


def _operation_request(
    library_id: str,
    action: str,
    libraries: Iterable[str] = (),
    *,
    name: str | None = None,
    description: str | None = None,
    public: bool | None = None,
) -> Request:
    """A set operation: ``union``, ``intersection``, ``difference``, ``copy`` or ``empty``.

    Only ``empty`` is safe to repeat; the others create a library or add documents.
    """
    payload: dict[str, Any] = {"action": action}
    others = list(libraries)
    if others:
        payload["libraries"] = others
    for key, value in (("name", name), ("description", description), ("public", public)):
        if value is not None:
            payload[key] = value
    return Request(
        "POST",
        _path("libraries/operations", library_id),
        json=payload,
        idempotent=True if action == "empty" else None,
    )


def _get_permissions_request(library_id: str) -> Request:
    """List who may access a library."""
    return Request("GET", _path("permissions", library_id))


def _set_permissions_request(
    library_id: str, email: str, *, read: bool, write: bool, admin: bool
) -> Request:
    """Set one user's permissions. Setting the same permissions twice is harmless."""
    payload = {"email": email, "permission": {"read": read, "write": write, "admin": admin}}
    return Request("POST", _path("permissions", library_id), json=payload, idempotent=True)


def _transfer_request(library_id: str, email: str) -> Request:
    """Make another user the owner of a library."""
    return Request("POST", _path("transfer", library_id), json={"email": email})


# Responses.


def _unexpected(request: Request, response: Response, detail: str) -> APIError:
    return APIError(
        f"{request.method} {request.path}: unexpected response from ADS ({detail})",
        status_code=response.status_code,
        response=response,
    )


def _object(request: Request, response: Response, *keys: str) -> dict[str, Any]:
    """``response.data[keys...]``, which must be a JSON object."""
    value = _response_field(request, response, *keys) if keys else response.data
    if not isinstance(value, dict):
        raise _unexpected(request, response, f"expected a JSON object, got {type(value).__name__}")
    return value


def _strings(request: Request, response: Response, *keys: str) -> list[str]:
    """``response.data[keys...]``, which must be a list of strings."""
    value = _response_field(request, response, *keys)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _unexpected(request, response, f"{keys[-1]!r} is not a list of strings")
    return value


def _count(request: Request, response: Response, key: str) -> int:
    value = _response_field(request, response, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unexpected(request, response, f"{key!r} is not an integer")
    return value


def _listing(request: Request, response: Response) -> list[dict[str, Any]]:
    """The metadata of each library in a ``GET /biblib/libraries`` response."""
    libraries = _response_field(request, response, "libraries")
    if not isinstance(libraries, list) or not all(isinstance(item, dict) for item in libraries):
        raise _unexpected(request, response, "'libraries' is not a list of objects")
    return libraries


def _permissions(request: Request, response: Response) -> dict[str, list[str]]:
    """Merge ADS's list of single-key ``{email: [permission, ...]}`` objects into one dict."""
    entries = response.data
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise _unexpected(request, response, "expected a list of {email: [permission, ...]} objects")
    merged: dict[str, list[str]] = {}
    for entry in entries:
        for email, permissions in entry.items():
            if not isinstance(permissions, list):
                raise _unexpected(request, response, f"permissions of {email!r} are not a list")
            merged.setdefault(email, []).extend(permissions)
    return merged


# Generators in the backend protocol, for use by other modules too.


def fetch_library(library_id: str) -> Generator[Request, Response, dict[str, Any]]:
    """Yield the request for one library and return its metadata dict.

    Works for any library the caller may read, including other users' public
    libraries. The response also carries the first page of bibcodes, which is
    ignored (use :func:`fetch_bibcodes` for those).
    """
    _check_id(library_id)
    return _fetch_library(library_id)


def _fetch_library(library_id: str) -> Generator[Request, Response, dict[str, Any]]:
    request = _get_request(library_id)
    response = yield request
    return _object(request, response, "metadata")


def fetch_bibcodes(
    library_id: str, *, page_size: int = DEFAULT_PAGE_SIZE
) -> Generator[Request, Response, list[str]]:
    """Yield the requests for every bibcode in a library and return them, in order, de-duplicated.

    Pages through ``GET /biblib/libraries/<id>`` with ``start``/``rows``,
    advancing ``start`` by the number of bibcodes each page returned (so a
    server that caps ``rows`` below ``page_size`` loses nothing). It stops when
    ``start`` reaches the library's ``num_documents``, or when a page adds no
    new bibcode (an empty page, or a server that ignores ``start``). Only if
    ``num_documents`` is missing does a short page end the listing, because ADS
    may return fewer bibcodes than ``num_documents`` says. Arguments are
    checked when this is called, not when the generator starts.
    """
    _check_id(library_id)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise ValueError(f"page_size must be a positive int, not {page_size!r}")
    return _fetch_bibcodes(library_id, page_size)


def _fetch_bibcodes(library_id: str, page_size: int) -> Generator[Request, Response, list[str]]:
    found: dict[str, None] = {}
    start = 0
    while True:
        request = _get_request(library_id, start=start, rows=page_size)
        response = yield request
        page = _strings(request, response, "documents")
        total = _object(request, response, "metadata").get("num_documents")
        before = len(found)
        found.update(dict.fromkeys(page))
        start += len(page)
        if len(found) == before:
            break
        if isinstance(total, int) and not isinstance(total, bool):
            if start >= total:
                break
        elif len(page) < page_size:
            break
    return list(found)


def _complete(gen: Generator[Request, Response, T], client: Client) -> T:
    """Run a generator that only yields Requests with ``client`` and return its result."""
    driven = drive(gen, client.send)
    try:
        item = next(driven)
    except StopIteration as stop:
        result: T = stop.value
        return result
    driven.close()
    raise TypeError(f"expected only Requests from {gen!r}, got {item!r}")


# Querying.


def _pinned_ids(expr: Expr | None, id_field: Field) -> tuple[str, ...] | None:
    """The library ids a query is restricted to, or ``None`` if it is not restricted.

    Only top-level conjuncts ``id == v`` and ``id.in_(vs)`` pin ids (several
    pins intersect); anything under ``|`` or ``~`` does not.
    """
    if expr is None:
        return None
    conjuncts = expr.operands if isinstance(expr, And) else (expr,)
    pinned: list[str] | None = None
    for conjunct in conjuncts:
        if isinstance(conjunct, Match) and conjunct.field is id_field:
            values: tuple[Any, ...] = (conjunct.value,)
        elif isinstance(conjunct, In) and conjunct.field is id_field:
            values = conjunct.values
        else:
            continue
        if not all(isinstance(value, str) for value in values):
            continue
        ids = list(dict.fromkeys(values))
        pinned = ids if pinned is None else [i for i in pinned if i in ids]
    return None if pinned is None else tuple(pinned)


class LibraryBackend:
    """Runs :class:`Library` queries: fetch metadata from ADS, then filter locally.

    A query whose filter pins library ids (a top-level ``Library.id == v`` or
    ``Library.id.in_(vs)``) fetches exactly those libraries, so other users'
    public libraries can be found; an id ADS does not know (404) yields no row.
    Any other query lists the caller's own libraries with one request. The
    filter, ordering, offset and limit are then applied in Python.
    """

    def __repr__(self) -> str:
        return "<LibraryBackend>"

    def run(self, query: Query[Any]) -> Generator[Request | list[Any], Response | None, None]:
        """Yield the requests, then the matching libraries as one batch."""
        rows = yield from self._fetch(query)
        yield select_rows(rows, query)

    def count(self, query: Query[Any]) -> Generator[Request, Response, int]:
        """Yield the requests and return the number of matching libraries."""
        rows = yield from self._fetch(query)
        return len(filter_rows(rows, query))

    def explain(self, query: Query[Any]) -> Explanation:
        """Show the request(s) that would be sent; filtering happens locally."""
        ids = _pinned_ids(query.expr, query.model._fields["id"])
        if ids is None:
            summary = "Lists your libraries with one request, then filters them locally."
            requests: tuple[Request, ...] = (_list_request(),)
        else:
            noun = "library" if len(ids) == 1 else "libraries"
            summary = f"Fetches {len(ids)} {noun} by id, then filters them locally."
            requests = tuple(_get_request(library_id) for library_id in ids)
        notes = ["filtering, ordering, offset and limit are applied locally to the library metadata"]
        if ids is not None:
            notes.append("an id that ADS does not know yields no row")
        if query.expr is not None:
            notes.append(f"filter: {query.expr!r}")
        return Explanation(summary, requests, tuple(notes))

    def _fetch(self, query: Query[Any]) -> Generator[Request, Response, list[Any]]:
        model = query.model
        ids = _pinned_ids(query.expr, model._fields["id"])
        if ids is None:
            request = _list_request()
            response = yield request
            found = _listing(request, response)
        else:
            found = []
            for library_id in ids:
                try:
                    metadata = yield from fetch_library(library_id)
                except APIError as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                found.append(metadata)
        rows = []
        for metadata in found:
            row = model._from_row(metadata)
            # The metadata is complete: reading a field ADS did not send must
            # not make a request per row while the query is being filtered.
            row._metadata_fetched = True
            rows.append(row)
        return rows


# The model.


def _check_id(library_id: Any) -> str:
    if not isinstance(library_id, str) or not library_id.strip():
        raise ValueError(f"a library id must be a non-empty str, not {library_id!r}")
    return library_id


def _check_email(email: Any) -> str:
    if not isinstance(email, str) or "@" not in email or not email.strip():
        raise ValueError(f"expected an email address, not {email!r}")
    return email.strip()


def _check_metadata(name: Any, description: Any, public: Any) -> None:
    for label, value in (("name", name), ("description", description)):
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{label} must be a str, not {type(value).__name__}")
    if public is not None and not isinstance(public, bool):
        raise TypeError(f"public must be a bool, not {type(public).__name__}")


def _other_id(library: Any) -> str:
    """The id of another library given as a :class:`Library` or an id string."""
    if isinstance(library, Library):
        return library._remote_id()
    if isinstance(library, str):
        return _check_id(library)
    raise TypeError(f"expected a Library or a library id, not {type(library).__name__}")


class Library(Model):
    """An ADS library: a named collection of documents kept by ADS for a user.

    Find libraries with queries (``Library.get(name="Gaia")``,
    ``Library.select().where(Library.num_documents > 100)``), which list your
    libraries and filter them locally; a query pinning ``Library.id`` fetches
    that library directly, so other users' public libraries work too. Create one
    with :meth:`create`. Every other change is an explicit method making one
    API call (:meth:`update`, :meth:`add`, :meth:`remove`, :meth:`empty`,
    :meth:`delete`, set operations, permissions); nothing is saved implicitly,
    and assigning to a field only changes the local object.

    Metadata fields missing from the object (for example after :meth:`create`)
    are fetched together, once, on first access. :attr:`bibcodes` is fetched on
    first access and cached until a method changes the documents. Fields the
    server maintains (such as ``date_last_modified``) are not updated locally by
    the methods; call :meth:`refresh` for that.

    Requests are sent with the ``client=`` argument of a method if given, else
    with the client in the ``client`` attribute (``None`` unless set by the
    user, or by :meth:`create` and the set operations when given a client),
    else with :func:`~ads.client.get_default_client`. Lazy loading and
    :attr:`bibcodes` use the attribute or the default. Libraries returned by a
    query do not remember the query's client: set ``library.client`` if needed.
    """

    _key = "id"
    _backend = LibraryBackend()

    id = StringField("Unique identifier of the library, assigned by ADS.")
    name = StringField("Name of the library (unique among its owner's libraries).")
    description = StringField("Description of the library.")
    public = BooleanField("Whether anyone may read the library.")
    owner = StringField("ADS username of the owner (not their email address).")
    permission = StringField("Your own access level: 'owner', 'admin', 'write' or 'read'.")
    num_documents = IntegerField("Number of documents in the library.")
    num_users = IntegerField("Number of users with access to the library.")
    date_created = DateTimeField("When the library was created (UTC).")
    date_last_modified = DateTimeField("When the library was last modified (UTC).")

    def __init__(self, **values: Any):
        self.client: Client | None = None
        self._bibcodes: tuple[str, ...] | None = None
        self._metadata_fetched = False
        self._deleted = False
        super().__init__(**values)

    # Creating.

    @classmethod
    def create(
        cls,
        name: str | None = None,
        description: str | None = None,
        public: bool = False,
        documents: Any = (),
        *,
        client: Client | None = None,
    ) -> Library:
        """Create a library on ADS and return it.

        :param name: the name, which must be unique among your libraries
            (ADS chooses one if ``None``).
        :param description: the description (ADS chooses one if ``None``).
        :param public: whether anyone may read the library.
        :param documents: bibcodes, documents, libraries, or iterables of these.
        :param client: the client to use; it is also remembered as ``client``
            on the returned library.
        """
        _check_metadata(name, description, public)
        request = _create_request(name, description, public, to_bibcodes(documents))
        response = _resolve(client).send(request)
        sent = {"name": name, "description": description, "public": public}
        return cls._created(request, response, sent, client)

    @classmethod
    def _created(
        cls, request: Request, response: Response, sent: Mapping[str, Any], client: Client | None
    ) -> Library:
        """The library ADS just created: what it returned, plus what was sent."""
        data = _object(request, response)
        _check_id(_response_field(request, response, "id"))
        library = cls._from_row(data)
        for key, value in sent.items():
            if value is not None and key not in library._data:
                library._data[key] = value
        library.client = client
        return library

    # Metadata.

    def update(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        public: bool | None = None,
        client: Client | None = None,
    ) -> None:
        """Change the name, description and/or visibility on ADS, and locally.

        Only the arguments given are sent. Raises ``ValueError`` if none is given.
        """
        _check_metadata(name, description, public)
        changes = {
            key: value
            for key, value in (("name", name), ("description", description), ("public", public))
            if value is not None
        }
        if not changes:
            raise ValueError("update() needs at least one of name, description or public")
        self._send(_update_request(self._remote_id(), changes), client)
        self._data.update(changes)

    def refresh(self, *, client: Client | None = None) -> Library:
        """Fetch the metadata again, replacing the local values, and forget cached bibcodes."""
        metadata = _complete(fetch_library(self._remote_id()), self._client(client))
        self._data.update(self._convert(metadata))
        self._metadata_fetched = True
        self._bibcodes = None
        return self

    def _load_missing(self, field: Field) -> Any:
        """Fetch the metadata once and fill every field that is still missing."""
        if self._metadata_fetched or self._data.get("id") is None:
            return None
        metadata = _complete(fetch_library(self._remote_id()), self._client(None))
        self._metadata_fetched = True
        for name, value in self._convert(metadata).items():
            self._data.setdefault(name, value)
        return self._data.get(field.name)

    @classmethod
    def _convert(cls, metadata: Mapping[str, Any]) -> dict[str, Any]:
        """Field values (by field name) from an API metadata mapping."""
        return {
            field.name: field.to_python(metadata[field.solr_name])
            for field in cls._fields.values()
            if field.solr_name in metadata
        }

    # Documents.

    @property
    def bibcodes(self) -> tuple[str, ...]:
        """The bibcodes of the documents, fetched on first access and cached."""
        if self._bibcodes is None:
            library_id = self._remote_id()
            self._bibcodes = tuple(_complete(fetch_bibcodes(library_id), self._client(None)))
        return self._bibcodes

    @property
    def documents(self) -> Query[Any]:
        """A query for the documents in this library, searched on ADS."""
        self._remote_id()
        from .document import Document

        query: Query[Any] = Document.select().where(Document.in_(self))
        return query

    def add(self, *documents: Any, client: Client | None = None) -> int:
        """Add documents (bibcodes, documents, libraries, or iterables of these).

        Returns the number ADS actually added (documents already present are not).
        """
        return self._change("add", "number_added", documents, client)

    def remove(self, *documents: Any, client: Client | None = None) -> int:
        """Remove documents; returns the number ADS actually removed."""
        return self._change("remove", "number_removed", documents, client)

    def _change(self, action: str, key: str, documents: tuple[Any, ...], client: Client | None) -> int:
        library_id = self._remote_id()
        request = _documents_request(library_id, action, _require_bibcodes(documents))
        changed = _count(request, self._send(request, client), key)
        self._bibcodes = None
        current = self._data.get("num_documents")
        if isinstance(current, int):
            self._data["num_documents"] = max(0, current + (changed if action == "add" else -changed))
        return changed

    def empty(self, *, client: Client | None = None) -> None:
        """Remove every document."""
        self._send(_operation_request(self._remote_id(), "empty"), client)
        self._data["num_documents"] = 0
        self._bibcodes = ()

    def delete(self, *, client: Client | None = None) -> None:
        """Delete the library from ADS. The object can no longer be used afterwards."""
        self._send(_delete_request(self._remote_id()), client)
        self._deleted = True
        self._bibcodes = None

    # Set operations.

    def union(
        self,
        *libraries: Library | str,
        name: str | None = None,
        description: str | None = None,
        public: bool = False,
        client: Client | None = None,
    ) -> Library:
        """Create a library holding the documents in this library or any of ``libraries``."""
        return self._combine("union", libraries, name, description, public, client)

    def intersection(
        self,
        *libraries: Library | str,
        name: str | None = None,
        description: str | None = None,
        public: bool = False,
        client: Client | None = None,
    ) -> Library:
        """Create a library holding the documents in this library and all of ``libraries``."""
        return self._combine("intersection", libraries, name, description, public, client)

    def difference(
        self,
        *libraries: Library | str,
        name: str | None = None,
        description: str | None = None,
        public: bool = False,
        client: Client | None = None,
    ) -> Library:
        """Create a library holding the documents in this library but in none of ``libraries``."""
        return self._combine("difference", libraries, name, description, public, client)

    def _combine(
        self,
        action: str,
        libraries: tuple[Library | str, ...],
        name: str | None,
        description: str | None,
        public: bool,
        client: Client | None,
    ) -> Library:
        library_id = self._remote_id()
        if not libraries:
            raise ValueError(f"{action}() needs at least one other library")
        _check_metadata(name, description, public)
        request = _operation_request(
            library_id,
            action,
            [_other_id(library) for library in libraries],
            name=name,
            description=description,
            public=public,
        )
        response = self._send(request, client)
        sent = {"name": name, "description": description, "public": public}
        return type(self)._created(request, response, sent, client)

    def copy_to(self, library: Library | str, *, client: Client | None = None) -> None:
        """Add this library's documents to ``library`` (a :class:`Library` or an id)."""
        request = _operation_request(self._remote_id(), "copy", [_other_id(library)])
        self._send(request, client)
        if isinstance(library, Library):
            library._bibcodes = None
            if library._data.pop("num_documents", None) is not None:
                library._metadata_fetched = False

    # Permissions.

    def get_permissions(self, *, client: Client | None = None) -> dict[str, list[str]]:
        """Who may access the library: ``{email: ["owner"] or ["read", "write", ...]}``."""
        request = _get_permissions_request(self._remote_id())
        return _permissions(request, self._send(request, client))

    def set_permissions(
        self,
        email: str,
        *,
        read: bool = False,
        write: bool = False,
        admin: bool = False,
        client: Client | None = None,
    ) -> None:
        """Set the permissions of the ADS user with this email address (all ``False`` revokes them)."""
        address = _check_email(email)
        for label, value in (("read", read), ("write", write), ("admin", admin)):
            if not isinstance(value, bool):
                raise TypeError(f"{label} must be a bool, not {type(value).__name__}")
        request = _set_permissions_request(self._remote_id(), address, read=read, write=write, admin=admin)
        self._send(request, client)

    def transfer(self, email: str, *, client: Client | None = None) -> None:
        """Make the ADS user with this email address the owner.

        You lose all access to the library unless the new owner grants it.
        """
        address = _check_email(email)
        self._send(_transfer_request(self._remote_id(), address), client)

    # Python protocols.

    def __iter__(self) -> Iterator[Any]:
        """Iterate over the documents (searched on ADS)."""
        return iter(self.documents)

    def __len__(self) -> int:
        """The number of documents, as ADS reports it in ``num_documents``."""
        count = self.num_documents
        if count is None:
            raise ADSError(f"{self!r}: the number of documents is not known")
        return count

    def __contains__(self, item: object) -> bool:
        """Whether a document (a bibcode or an object with a ``bibcode``) is in the library."""
        bibcodes = to_bibcodes(item)
        if len(bibcodes) != 1:
            raise ValueError(f"`in` needs a single bibcode or document, not {item!r}")
        return bibcodes[0] in self.bibcodes

    def __bool__(self) -> bool:
        """Always true: an empty library is still a library."""
        return True

    def __repr__(self) -> str:
        library_id = self._data.get("id")
        if library_id is None:
            return super().__repr__()
        name = self._data.get("name")
        return f"<Library: id={library_id!r}{f' name={name!r}' if name is not None else ''}>"

    # Helpers.

    def _remote_id(self) -> str:
        """This library's id; raises ``ADSError`` if it was deleted or never existed on ADS."""
        if self._deleted:
            raise ADSError(f"{self!r} was deleted from ADS and can no longer be used")
        library_id = self._data.get("id")
        if library_id is None:
            raise ADSError(f"{self!r} has no id, so it is not on ADS; use Library.create()")
        return _check_id(library_id)

    def _client(self, client: Client | None) -> Client:
        return client if client is not None else _resolve(self.client)

    def _send(self, request: Request, client: Client | None) -> Response:
        return self._client(client).send(request)


def _resolve(client: Client | None) -> Client:
    return client if client is not None else get_default_client()
