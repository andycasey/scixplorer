"""Creating, changing and deleting libraries (item 18; only with --live --live-write).

Every library here is created through a :class:`~live.support.Janitor`, named
``ads-live-test-<UTC start time>-<random>-<label>``, and deleted when the module
ends, also after a failed assertion or an aborted session. No pre-existing
library is read, changed or deleted. The changes run once, in order, in the
``scenario`` fixture; each test then checks one of their results.

Not tested: permissions (get_permissions/set_permissions) and transfer,
which need a second ADS account.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from ads.client import Client
from ads.models.document import Document
from ads.models.library import Library

from .harness import PINNED, Live, Outcome
from .support import Findings, Janitor, LiveTransport, redact

pytestmark = [pytest.mark.live, pytest.mark.live_write]

UPDATED_DESCRIPTION = "Updated by the ads live test suite."

#: Documents of the two libraries combined by the set operations: they share PINNED[2].
FIRST, SECOND = PINNED[:3], PINNED[2:]

#: Expected sizes of the libraries the set operations create.
SET_OPERATIONS = {"union": 5, "intersection": 1, "difference": 2}


@dataclass
class Step:
    """The value of one change, or the error it raised."""

    value: Any = None
    error: str | None = None


class Scenario:
    """Runs the changes in order, recording each result or error instead of stopping."""

    def __init__(self, hidden: tuple[str, ...]) -> None:
        self.hidden = hidden
        self.steps: dict[str, Step] = {}
        self.libraries: dict[str, Library] = {}

    def run(self, name: str, action: Callable[[], Any]) -> Any:
        try:
            value = action()
        except Exception as exc:
            self.steps[name] = Step(error=redact(f"{type(exc).__name__}: {exc}", self.hidden))
            return None
        self.steps[name] = Step(value)
        if isinstance(value, Library):
            self.libraries[name] = value
        return value

    def need(self, name: str) -> Any:
        """The value of a step; fails with its error, or skips if it never ran."""
        step = self.steps.get(name)
        if step is None:
            pytest.skip(f"the step {name!r} did not run because an earlier step failed")
        if step.error is not None:
            raise AssertionError(f"{name} failed: {step.error}")
        return step.value

    def size(self, name: str) -> Any:
        """``num_documents`` of the library created by step ``name``, from the final listing."""
        library = self.need(name)
        listing = self.need("listing")
        if library.id not in listing:
            raise AssertionError(f"library {library.id} ({name}) is missing from the listing of your libraries")
        return listing[library.id].get("num_documents")


@pytest.fixture(scope="module")
def scenario(
    live: Live, live_client: Client, live_transport: LiveTransport, findings: Findings
) -> Iterator[Scenario]:
    """Create three libraries, change one, combine the others; delete them all afterwards."""
    client = live_client
    janitor = Janitor(client, live_transport)
    try:
        with janitor:
            run = Scenario(live_transport.hidden)
            first = run.run("create first", lambda: janitor.create("first", documents=FIRST))
            second = run.run("create second", lambda: janitor.create("second", documents=SECOND))
            empty = run.run("create empty", lambda: janitor.create("empty"))
            if empty is not None:
                run.run("fresh", lambda: dict(empty.refresh(client=client)._data))
                run.run("add", lambda: empty.add(*PINNED[:2], client=client))
                run.run("remove", lambda: empty.remove(PINNED[0], client=client))
                run.run("update", lambda: empty.update(
                    name=janitor.name("renamed"), description=UPDATED_DESCRIPTION, client=client))
                run.run("edited", lambda: dict(empty.refresh(client=client)._data))
                run.run("empty", lambda: empty.empty(client=client))
            if first is not None and second is not None:
                for operation in SET_OPERATIONS:
                    run.run(operation, lambda operation=operation: janitor.track(
                        getattr(first, operation)(second, name=janitor.name(operation), client=client)))
                run.run("docs in", lambda: live.count(Document.bibcode.in_(PINNED) & Document.in_(first)))
                run.run("docs not in", lambda: live.count(Document.bibcode.in_(PINNED) & Document.not_in(first)))
            run.run("listing", lambda: {lib.id: dict(lib._data) for lib in Library.select().using(client).all()})
            yield run
    finally:
        leftovers = "".join(f"\nNOT DELETED: {problem}" for problem in janitor.problems)
        findings.add(
            "libraries created by --live-write",
            f"created {len(janitor.created)}, deleted {len(janitor.deleted)} (names start {janitor.tag}){leftovers}",
        )
    if janitor.problems:
        pytest.fail("could not delete every library this run created:\n  " + "\n  ".join(janitor.problems))


def test_create_without_documents(scenario: Scenario) -> None:
    """POST /biblib/libraries without a bibcode list creates an empty library (num_documents 0).

    Depends on it: Library.create() with no documents, and _created() reading the new id.
    """
    scenario.need("create empty")
    fresh = scenario.need("fresh")
    assert fresh.get("num_documents") == 0, fresh


def test_create_with_documents(scenario: Scenario) -> None:
    """POST /biblib/libraries with a bibcode list creates a library holding them.

    Depends on it: Library.create(documents=...) sending them as "bibcode".
    """
    assert scenario.size("create first") == len(FIRST)
    assert scenario.size("create second") == len(SECOND)


def test_add_reports_number_added(scenario: Scenario) -> None:
    """Adding two new documents answers {"number_added": 2}.

    Depends on it: Library.add() returning response["number_added"].
    """
    assert scenario.need("add") == 2


def test_remove_reports_number_removed(scenario: Scenario) -> None:
    """Removing one document answers {"number_removed": 1}.

    Depends on it: Library.remove() returning response["number_removed"].
    """
    assert scenario.need("remove") == 1


def test_add_and_remove_change_num_documents(scenario: Scenario) -> None:
    """After adding two documents and removing one, the library's num_documents is 1.

    Depends on it: Library.add()/remove() adjusting the local num_documents the same way.
    """
    assert scenario.need("edited").get("num_documents") == 1


def test_update_changes_name_and_description(scenario: Scenario) -> None:
    """PUT /biblib/documents/<id> with name and description changes them.

    Depends on it: Library.update() and its request path (documents, not libraries).
    """
    scenario.need("update")
    edited = scenario.need("edited")
    assert str(edited.get("name")).endswith("-renamed") and edited.get("description") == UPDATED_DESCRIPTION, edited


def test_empty_removes_every_document(scenario: Scenario) -> None:
    """The "empty" operation leaves the library with no documents.

    Depends on it: Library.empty() (POST /biblib/libraries/operations/<id> with action "empty").
    """
    scenario.need("empty")
    assert scenario.size("create empty") == 0


@pytest.mark.parametrize("operation", sorted(SET_OPERATIONS))
def test_set_operation_creates_a_library(scenario: Scenario, operation: str) -> None:
    """union / intersection / difference create a new library holding the combined documents.

    Depends on it: Library.union(), intersection() and difference() and the
    "libraries" list they send.
    """
    created = scenario.need(operation)
    assert created.id not in (scenario.need("create first").id, scenario.need("create second").id)
    assert scenario.size(operation) == SET_OPERATIONS[operation]


def test_docs_of_a_new_private_library(scenario: Scenario) -> None:
    """docs(library/<id>) works for the caller's private library: of the pinned records, exactly its 3 members match.

    And -docs(library/<id>) matches the other 2. Depends on it:
    config.library_membership = "docs" for private libraries.
    """
    inside: Outcome = scenario.need("docs in").require("docs(library/...) was rejected")
    outside: Outcome = scenario.need("docs not in").require("-docs(library/...) was rejected")
    assert inside.num_found == len(FIRST) and outside.num_found == len(PINNED) - len(FIRST), (
        f"{inside.describe()}\n{outside.describe()}"
    )
