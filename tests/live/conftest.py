"""Fixtures and hooks shared by the live tests.

One session-scoped :class:`~ads.client.Client` sends every request through a
:class:`~live.support.LiveTransport`, which counts requests per service,
replays identical searches from a session cache, refuses request
:data:`~live.support.HARD_LIMIT` + 1 (the test making it fails with
:class:`~live.support.BudgetExhausted`, and the session then stops, running
every teardown), and skips the remaining tests after an HTTP 429. The client
is also made the default client, so that no request (a lazy load, a library
method) can bypass the transport. Results that several tests use are fetched
once, by session fixtures.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from ads import _config
from ads.client import Client, set_default_client
from ads.models.document import Document
from ads.models.library import Library

from . import probes as probing
from . import schema
from .harness import PINNED, PINNED_FIELDS, Live, Outcome, choose_lead, closed_set
from .support import BudgetExhausted, Findings, LiveTransport, scrub_report

LIBRARY_ENV = "SCIX_LIVE_LIBRARY"

NO_TOKEN = (
    "no ADS API token: set ADS_API_TOKEN (or ADS_DEV_KEY), or put the token on the first line "
    "of ~/.ads/token (see docs/live-tests.md)"
)

TRANSPORT = pytest.StashKey[LiveTransport]()
CLIENT = pytest.StashKey[Client]()
FINDINGS = pytest.StashKey[Findings]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[Any]:
    """Stop the session once the request budget is spent; remove the token from every report."""
    report: pytest.TestReport = yield
    if call.excinfo is not None and call.excinfo.errisinstance(BudgetExhausted):
        item.session.shouldstop = str(call.excinfo.value)
    scrub_report(report, _config.config.resolve_tokens())
    return report


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    """Print the requests made per service, the rate limits, and the findings."""
    transport = config.stash.get(TRANSPORT, None)
    if transport is None:
        return
    client = config.stash.get(CLIENT, None)
    terminalreporter.section("live API requests")
    for line in transport.report_lines(client.rate_limits if client is not None else {}):
        terminalreporter.write_line(line)
    findings = config.stash.get(FINDINGS, None)
    if findings is not None and findings.entries:
        terminalreporter.section("live API findings")
        for line in findings.lines():
            terminalreporter.write_line(line)


@pytest.fixture(scope="session")
def findings(pytestconfig: pytest.Config) -> Findings:
    """Observations printed at the end of the run."""
    found = Findings()
    pytestconfig.stash[FINDINGS] = found
    return found


@pytest.fixture(scope="session")
def live_transport(pytestconfig: pytest.Config) -> Iterator[LiveTransport]:
    """The counting, caching, budget-enforcing transport of every live client."""
    transport = LiveTransport(httpx.HTTPTransport(), api_url=_config.config.api_url)
    pytestconfig.stash[TRANSPORT] = transport
    yield transport
    transport.close()


@pytest.fixture(autouse=True)
def _stop_after_rate_limit(live_transport: LiveTransport) -> None:
    if live_transport.stopped is not None:
        pytest.skip(live_transport.stopped)


@pytest.fixture(scope="session")
def live_tokens() -> tuple[str, ...]:
    """The API token(s) the client would use; skips every test needing them if there are none."""
    tokens = _config.config.resolve_tokens()
    if not tokens:
        pytest.skip(NO_TOKEN)
    return tokens


@pytest.fixture(scope="session")
def live_client(pytestconfig: pytest.Config, live_tokens: tuple[str, ...], live_transport: LiveTransport) -> Iterator[Client]:
    """The client of the live tests (one retry on 502/503/504; retries count towards the budget)."""
    live_transport.hidden = live_tokens
    client = Client(config=dataclasses.replace(_config.config, max_retries=1), transport=live_transport)
    pytestconfig.stash[CLIENT] = client
    set_default_client(client)
    yield client
    set_default_client(None)
    client.close()


@pytest.fixture(scope="session")
def live(live_client: Client, live_transport: LiveTransport) -> Live:
    return Live(live_client, live_transport)


@pytest.fixture(scope="session")
def pinned_outcome(live: Live) -> Outcome:
    """The pinned records with :data:`PINNED_FIELDS`, from one ``bibcode:("…" OR …)`` request."""
    return live.search(Document.bibcode.in_(PINNED), fields=PINNED_FIELDS, rows=len(PINNED))


@pytest.fixture(scope="session")
def pinned(pinned_outcome: Outcome) -> dict[str, dict[str, Any]]:
    """The pinned records by bibcode (raw JSON)."""
    pinned_outcome.require("the pinned records could not be fetched, so the tests using them cannot run")
    return {str(doc["bibcode"]): doc for doc in pinned_outcome.docs if doc.get("bibcode") in PINNED}


@pytest.fixture(scope="session")
def lead(pinned: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The pinned record the probes and single-record tests are anchored to."""
    record = choose_lead(pinned)
    if record is None:
        pytest.fail("no pinned record has three authors, a spelled-out first name and a plain title; change PINNED")
    return record


@pytest.fixture(scope="session")
def probes(live: Live, pinned: dict[str, dict[str, Any]]) -> probing.ProbeResults:
    """Every probe's verdict, with one request if they all pass."""
    return probing.ProbeResults(live, probing.build(pinned))


@pytest.fixture(scope="session")
def full_records(live: Live) -> Outcome:
    """The pinned records with the default field list (every stored, non-deferred field)."""
    return live.search(Document.bibcode.in_(PINNED), rows=len(PINNED))


@pytest.fixture(scope="session")
def sample(live: Live) -> Outcome:
    """The most cited records of the closed set (so of several years), with their years.

    Ten more than config.max_inline_bibcodes, so that a search for them goes to /search/bigquery.
    """
    size = _config.config.max_inline_bibcodes + 10
    order = (Document.citation_count.desc(),)
    outcome = live.search(closed_set(), fields=("bibcode", "year"), order=order, rows=size)
    outcome.require("the sample of the closed set could not be fetched")
    if len(outcome.docs) != size:
        pytest.fail(outcome.explain(f"expected {size} records in the sample"))
    return outcome


@pytest.fixture(scope="session")
def schema_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The current schema.xml of the index, downloaded into a new empty directory (no token needed)."""
    return schema.download(tmp_path_factory.mktemp("schema"))


@pytest.fixture(scope="session")
def public_library_id(pytestconfig: pytest.Config) -> str:
    """The public library named by --live-library or SCIX_LIVE_LIBRARY; skips if there is none."""
    value = (pytestconfig.getoption("live_library") or os.environ.get(LIBRARY_ENV, "")).strip()
    if not value:
        pytest.skip(f"needs a public library: pass --live-library ID or set {LIBRARY_ENV}")
    return value


@pytest.fixture(scope="session")
def public_library(live_client: Client, public_library_id: str) -> Library:
    """The public library, fetched by id (one request)."""
    found = Library.select().where(Library.id == public_library_id).using(live_client).all()
    if not found:
        pytest.fail(f"library {public_library_id} was not found (HTTP 404); check --live-library")
    return found[0]
