"""Reading libraries, and searching a public library's documents with docs(library/<id>) (items 10 and 18).

The public library comes from --live-library ID or SCIX_LIVE_LIBRARY; the
tests that need it are skipped without one. Nothing here changes a library.
"""

from __future__ import annotations

import base64
import math
import uuid
import warnings

import pytest

from scixplorer.client import Client
from scixplorer.exceptions import APIError, DataWarning
from scixplorer.models.document import Document
from scixplorer.models.library import Library, fetch_bibcodes

from .harness import Live, complete, compiled
from .support import LiveTransport, redact

pytestmark = pytest.mark.live

#: Larger public libraries are not paged through, to stay within the request budget.
MAX_PAGED = 2000

#: How many members are searched for by bibcode.
MEMBERS = 5


def test_library_listing(live_client: Client) -> None:
    """GET /biblib/libraries lists the caller's libraries, each with an id, loading without a DataWarning.

    Depends on it: Library.select() (LibraryBackend) and the field classes of Library.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DataWarning)
        libraries = Library.select().using(live_client).all()
    warned = [str(w.message) for w in caught if issubclass(w.category, DataWarning)]
    assert all(isinstance(library._data.get("id"), str) for library in libraries), libraries
    assert not warned, "DataWarning while loading library metadata:\n  " + "\n  ".join(warned)


def test_unknown_library_id_yields_no_row(live_client: Client, live_transport: LiveTransport) -> None:
    """A library id that does not exist yields no row, because ADS answers HTTP 404 for it.

    Depends on it: LibraryBackend._fetch treating (only) HTTP 404 as "no such library".
    """
    unknown = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")
    try:
        found = Library.select().where(Library.id == unknown).using(live_client).all()
    except APIError as exc:
        raise AssertionError(
            f"ADS answered HTTP {exc.status_code} for an unknown library id, not 404, so the client raises "
            f"instead of returning no row: {redact(str(exc), live_transport.hidden)}"
        ) from None
    exchange = live_transport.last("biblib/libraries")
    assert found == [] and exchange is not None and exchange.status == 404, (found, exchange)


def test_get_public_library_by_id(public_library: Library) -> None:
    """GET /biblib/libraries/<id> returns a public library's metadata: its id, name and num_documents.

    Depends on it: Library.get(id=...) and fetch_library() reading response["metadata"].
    """
    data = public_library._data
    assert isinstance(data.get("id"), str) and isinstance(data.get("name"), str), data
    assert isinstance(data.get("num_documents"), int), data


@pytest.fixture(scope="module")
def library_bibcodes(live_client: Client, public_library: Library) -> list[str]:
    """The public library's bibcodes, in one page."""
    size = public_library._data.get("num_documents")
    if not isinstance(size, int) or size == 0 or size > MAX_PAGED:
        pytest.skip(f"needs a public library with 1 to {MAX_PAGED} documents; it has {size}")
    return complete(fetch_bibcodes(public_library.id), live_client)


def test_fetch_bibcodes_paging_is_consistent(
    live_client: Client, public_library: Library, library_bibcodes: list[str]
) -> None:
    """Paging a library's bibcodes with a small page_size (start/rows) gives the same list as one large page.

    Depends on it: models.library.fetch_bibcodes advancing start by the bibcodes received.
    """
    page_size = math.ceil(len(library_bibcodes) / 3)
    paged = complete(fetch_bibcodes(public_library.id, page_size=page_size), live_client)
    assert set(paged) == set(library_bibcodes), (
        f"page_size={page_size} gave {len(paged)} bibcodes, one page gave {len(library_bibcodes)}; "
        f"only paged: {sorted(set(paged) - set(library_bibcodes))[:5]}, "
        f"only in one page: {sorted(set(library_bibcodes) - set(paged))[:5]}"
    )


def test_docs_library_counts_its_documents(live: Live, public_library: Library) -> None:
    """docs(library/<id>) (Document.in_(library)) finds the library's documents: at least one, at most num_documents.

    Depends on it: config.library_membership = "docs", the default.
    """
    expr = compiled(Document.in_(public_library), f"docs(library/{public_library.id})")
    outcome = live.count(expr).require("docs(library/...) was rejected")
    size = public_library._data.get("num_documents")
    assert isinstance(size, int) and outcome.num_found is not None
    assert 0 < outcome.num_found <= size, outcome.explain(f"expected 1 to {size} documents")


def test_docs_library_matches_its_members(live: Live, public_library: Library, library_bibcodes: list[str]) -> None:
    """bibcode:(<members>) AND docs(library/<id>) finds every member that bibcode:(<members>) finds.

    Depends on it: Document.in_(library) combined with other conditions.
    """
    members = Document.bibcode.in_(library_bibcodes[:MEMBERS])
    whole = live.count(members).require("rejected")
    inside = live.count(members & Document.in_(public_library)).require("rejected")
    assert whole.num_found and inside.num_found == whole.num_found, (
        f"expected every member found by bibcode to be in the library:\n{whole.describe()}\n{inside.describe()}"
    )


def test_not_docs_library_excludes_its_members(
    live: Live, public_library: Library, library_bibcodes: list[str]
) -> None:
    """bibcode:(<members>) AND -docs(library/<id>) (~Document.in_(library)) finds none of the members.

    Depends on it: Document.not_in(library) compiled as -docs(library/<id>).
    """
    expr = Document.bibcode.in_(library_bibcodes[:MEMBERS]) & Document.not_in(public_library)
    outcome = live.count(expr).require("rejected")
    assert outcome.num_found == 0, outcome.explain("expected no members outside the library")
