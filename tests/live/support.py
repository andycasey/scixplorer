"""Plumbing for the live tests: request budget, rate-limit stop, response cache,
group testing, library cleanup, findings and token redaction.

Nothing here needs the network, so ``tests/test_live_plumbing.py`` proves these
safety properties offline against fake transports.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Generic, TypeVar
from urllib.parse import urlsplit

import httpx
import pytest

from ads.client import Client, RateLimit, service_for
from ads.models.library import Library

#: Requests after which no more are sent and the session stops (the suite is designed to need fewer than 130).
HARD_LIMIT = 150

#: What a secret is replaced with in any text the suite shows.
REDACTED = "[REDACTED]"

#: Start of the name of every library the write tests create.
LIBRARY_PREFIX = "ads-live-test"

LIBRARY_DESCRIPTION = "Temporary library created by the ads live test suite; it is deleted when the run ends."

#: Read-only endpoints whose answers are reused within a session.
_CACHEABLE = frozenset({("GET", "search/query"), ("POST", "search/bigquery")})

#: Statuses worth caching: answers and deterministic rejections (never 429 or 5xx).
_CACHED_STATUSES = frozenset({200, 400})

#: Headers that no longer describe a body that httpx has already decoded.
_STALE_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})

_RATE_HEADERS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")

_MAX_DETAIL = 400

K = TypeVar("K")
E = TypeVar("E")


def redact(text: str, hidden: Iterable[str]) -> str:
    """``text`` with every non-blank string in ``hidden`` replaced by :data:`REDACTED`."""
    for secret in hidden:
        if secret.strip():
            text = text.replace(secret, REDACTED)
    return text


def scrub_report(report: pytest.TestReport, hidden: Sequence[str]) -> None:
    """Remove every secret in ``hidden`` from a test report's failure text and captured output."""
    hidden = [secret for secret in hidden if secret.strip()]
    if not hidden:
        return
    longrepr = report.longrepr
    if isinstance(longrepr, tuple):
        path, line, message = longrepr
        report.longrepr = (path, line, redact(message, hidden))
    elif longrepr is not None:
        text = str(longrepr)
        if any(secret in text for secret in hidden):
            report.longrepr = redact(text, hidden)
    report.sections = [(title, redact(content, hidden)) for title, content in report.sections]


def new_tag() -> str:
    """A name prefix unique to this session: the prefix, the UTC start time and a random suffix."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{LIBRARY_PREFIX}-{stamp}-{secrets.token_hex(3)}"


@dataclass(frozen=True)
class Exchange:
    """One request that reached the server, as the suite records it.

    Headers are never recorded (so neither is the token), except the
    rate-limit headers of the response.
    """

    method: str
    service: str
    params: dict[str, str]
    content_type: str | None
    status: int
    rate_headers: dict[str, str]
    detail: str


class BudgetExhausted(BaseException):
    """Raised instead of sending a request over the hard limit.

    A ``BaseException``, so that no ``except Exception`` in the client or the
    tests can swallow it; the live conftest stops the session when it sees it.
    """


class LiveTransport(httpx.BaseTransport):
    """The transport of every live client: counts, caches, enforces the budget, stops on HTTP 429.

    :param inner: the transport that reaches the network (or a fake one).
    :param api_url: the API base URL, whose path is stripped to name services.
    :param limit: the most requests sent; the next raises :class:`BudgetExhausted` instead.

    Answers from ``/search/query`` and ``/search/bigquery`` (HTTP 200 or 400)
    are kept for the session, keyed by method, path, parameters, body and a
    fingerprint of the ``Authorization`` header, and replayed for an identical
    request without counting it. After an HTTP 429 every later request
    raises ``pytest.skip.Exception`` without being sent. Inside
    :meth:`cleanup` requests are sent whatever the budget and the 429 state,
    and a 429 is returned to the client like any other answer.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        api_url: str,
        limit: int = HARD_LIMIT,
    ) -> None:
        self.inner = inner
        self.limit = limit
        self.hidden: tuple[str, ...] = ()
        self.counts: Counter[str] = Counter()
        self.cache_hits = 0
        self.stopped: str | None = None
        self.log: list[Exchange] = []
        self._prefix = urlsplit(api_url).path.rstrip("/")
        self._cache: dict[tuple[Any, ...], tuple[int, list[tuple[str, str]], bytes]] = {}
        self._cleaning = 0

    @property
    def total(self) -> int:
        """The number of requests sent to the server so far."""
        return sum(self.counts.values())

    @contextmanager
    def cleanup(self) -> Iterator[None]:
        """Let requests through regardless of the budget and of an earlier HTTP 429."""
        self._cleaning += 1
        try:
            yield
        finally:
            self._cleaning -= 1

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        service = self._service(request)
        key = self._cache_key(request, service)
        if key is not None and key in self._cache:
            self.cache_hits += 1
            status, headers, content = self._cache[key]
            return httpx.Response(status, headers=headers, content=content, request=request)
        cleaning = self._cleaning > 0
        if not cleaning:
            if self.stopped is not None:
                raise pytest.skip.Exception(self.stopped)
            if self.total >= self.limit:
                raise BudgetExhausted(
                    f"live tests stopped: {self.total} requests already sent, and the hard limit is "
                    f"{self.limit} (the suite is designed to need fewer than 130). Not sent: "
                    f"{request.method} {service}."
                )
        self.counts[service] += 1
        upstream = self.inner.handle_request(request)
        try:
            content = upstream.read()
        finally:
            upstream.close()
        headers = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() not in _STALE_HEADERS]
        self.log.append(self._exchange(request, service, upstream, content))
        if upstream.status_code == 429 and not cleaning:
            self.stopped = _rate_limit_message(service, upstream.headers.get("x-ratelimit-reset"))
            raise pytest.skip.Exception(self.stopped)
        if key is not None and upstream.status_code in _CACHED_STATUSES:
            self._cache[key] = (upstream.status_code, headers, content)
        return httpx.Response(upstream.status_code, headers=headers, content=content, request=request)

    def close(self) -> None:
        self.inner.close()

    def last(self, service: str) -> Exchange | None:
        """The latest exchange with ``service`` (e.g. ``"search/query"``), if any."""
        return next((exchange for exchange in reversed(self.log) if exchange.service == service), None)

    def report_lines(self, rate_limits: Mapping[str, RateLimit]) -> list[str]:
        """Requests per service, cache use, and the rate limits the server last reported."""
        lines = [f"{service:24} {count:4d}" for service, count in sorted(self.counts.items())]
        lines.append(
            f"{'total':24} {self.total:4d} sent (hard limit {self.limit}); "
            f"{self.cache_hits} answered from the session cache"
        )
        for service, rate in sorted(rate_limits.items()):
            lines.append(
                f"rate limit of {service}: {rate.remaining} of {rate.limit} left, "
                f"resets {rate.reset:%Y-%m-%d %H:%M} UTC"
            )
        if self.stopped is not None:
            lines.append(self.stopped)
        return lines

    def _service(self, request: httpx.Request) -> str:
        path = request.url.path
        if self._prefix and path.startswith(self._prefix + "/"):
            path = path[len(self._prefix) :]
        return service_for(path)

    def _cache_key(self, request: httpx.Request, service: str) -> tuple[Any, ...] | None:
        if (request.method, service) not in _CACHEABLE:
            return None
        auth = hashlib.sha256(request.headers.get("authorization", "").encode()).hexdigest()
        body = hashlib.sha256(request.content).hexdigest()
        return (request.method, service, tuple(sorted(request.url.params.multi_items())), body, auth)

    def _exchange(self, request: httpx.Request, service: str, response: httpx.Response, content: bytes) -> Exchange:
        detail = ""
        if not 200 <= response.status_code < 300:
            detail = " ".join(content.decode("utf-8", "replace").split())[:_MAX_DETAIL]
        return Exchange(
            method=request.method,
            service=service,
            params=dict(request.url.params.multi_items()),
            content_type=request.headers.get("content-type"),
            status=response.status_code,
            rate_headers={name: response.headers[name] for name in _RATE_HEADERS if name in response.headers},
            detail=redact(detail, self.hidden),
        )


def _rate_limit_message(service: str, reset: str | None) -> str:
    when = ""
    try:
        when = f" (it resets at {datetime.fromtimestamp(float(reset or ''), tz=timezone.utc):%Y-%m-%d %H:%M} UTC)"
    except (ValueError, OverflowError, OSError):
        pass
    return (
        f"ADS answered HTTP 429 (rate limit exceeded) for {service}{when}; "
        f"the remaining live tests are skipped. Run them again after the reset."
    )


@dataclass(frozen=True)
class Verdict(Generic[E]):
    """The outcome of group testing for one item.

    ``status`` is ``"pass"``, ``"fail"`` or ``"undetermined"`` (the call cap was
    reached first). ``evidence`` is what ``check`` returned for the smallest
    group that decided it (for an undetermined item, the failing group that
    held it, if any); ``alone`` says whether that group was the item by itself.
    """

    status: str
    evidence: E | None
    alone: bool = False


def isolate(
    items: Sequence[K], check: Callable[[Sequence[K]], tuple[bool, E]], cap: int, *, failure: E | None = None
) -> dict[K, Verdict[E]]:
    """Find which items fail ``check`` with at most ``cap`` calls (adaptive group testing).

    ``check(group)`` returns ``(passed, evidence)`` and must pass exactly when
    every item of the group would pass alone: a conjunction of probes, or a
    query that the server rejects if it rejects any of its clauses. The whole
    group is checked first (unless ``failure`` is the evidence that it fails),
    so if everything passes that is one call. A failing group is split in
    halves; a half known to hold a failure is not checked again, except that
    an item found failing by elimination is checked alone (cap permitting) so
    that its evidence is its own.
    """
    verdicts: dict[K, Verdict[E]] = {}
    calls = 0

    def run(group: Sequence[K]) -> tuple[bool, E] | None:
        nonlocal calls
        if calls >= cap:
            return None
        calls += 1
        return check(group)

    def settle(group: Sequence[K], status: str, evidence: E | None, alone: bool = False) -> None:
        for item in group:
            verdicts[item] = Verdict(status, evidence, alone)

    def solve(group: Sequence[K], failure: E | None) -> None:
        if failure is None or len(group) == 1:
            result = run(group)
            if result is None:
                settle(group, "fail" if failure is not None else "undetermined", failure)
                return
            passed, evidence = result
            if passed or len(group) == 1:
                settle(group, "pass" if passed else "fail", evidence, alone=len(group) == 1)
                return
            failure = evidence
        middle = len(group) // 2
        left, right = group[:middle], group[middle:]
        result = run(left)
        if result is None:
            settle(group, "undetermined", failure)
            return
        passed, evidence = result
        if passed:
            settle(left, "pass", evidence, alone=len(left) == 1)
            solve(right, failure)
        elif len(left) == 1:
            settle(left, "fail", evidence, alone=True)
            solve(right, None)
        else:
            solve(left, evidence)
            solve(right, None)

    if items:
        solve(list(items), failure)
    return verdicts


def peel(
    items: Sequence[str],
    check: Callable[[Sequence[str]], tuple[bool, E]],
    cap: int,
    *,
    message: Callable[[E], str | None],
) -> dict[str, Verdict[E]]:
    """Like :func:`isolate`, for checks whose failure message may name the failing item.

    While the group fails and its message names exactly one item of the group
    (as a whole word, or followed by ``_`` as in ``aff_nosyn``; the longest
    name wins), that item is settled as failing and the rest are checked
    again without it. A failure naming no item, or several, is passed to
    :func:`isolate` with the calls left. So k failing items that the server
    names cost k + 1 calls.
    """
    verdicts: dict[str, Verdict[E]] = {}
    remaining = list(items)
    calls = 0
    while remaining and calls < cap:
        calls += 1
        passed, evidence = check(remaining)
        if passed:
            verdicts.update((item, Verdict("pass", evidence, alone=len(remaining) == 1)) for item in remaining)
            return verdicts
        text = message(evidence) or ""
        named = [item for item in remaining if re.search(rf"(?<!\w){re.escape(item)}(?![A-Za-z0-9])", text)]
        named = [item for item in named if not any(other != item and other.startswith(item) for other in named)]
        if len(named) != 1:
            verdicts.update(isolate(remaining, check, cap - calls, failure=evidence))
            return verdicts
        verdicts[named[0]] = Verdict("fail", evidence, alone=len(remaining) == 1)
        remaining.remove(named[0])
    verdicts.update((item, Verdict("undetermined", None)) for item in remaining)
    return verdicts


class Janitor:
    """Creates the write tests' libraries and deletes every one of them afterwards.

    Each library is named ``<tag>-<label>``; the tag holds the session's UTC
    start time and a random suffix, so cleanup can never match a library that
    existed before the run. Used as a context manager, on exit (also after a
    failed assertion, a skip or an aborted session) it deletes each library
    it saw created, then lists the account's libraries once and deletes any
    other whose name carries the tag (one created by a request whose answer
    the client could not read). Cleanup requests bypass the budget and the
    rate-limit stop. Libraries it could not delete are described in ``problems``.
    """

    def __init__(self, client: Client, transport: LiveTransport, tag: str | None = None) -> None:
        self.client = client
        self.transport = transport
        self.tag = tag or new_tag()
        self.created: dict[str, str] = {}
        self.deleted: list[str] = []
        self.problems: list[str] = []
        self._attempted: set[str] = set()

    def name(self, label: str) -> str:
        """The name of the library with this label."""
        return f"{self.tag}-{label}"

    def create(self, label: str, *, documents: Sequence[str] = ()) -> Library:
        """Create a private library named for ``label`` holding ``documents``, and track it."""
        library = Library.create(
            name=self.name(label),
            description=LIBRARY_DESCRIPTION,
            public=False,
            documents=list(documents),
            client=self.client,
        )
        return self.track(library)

    def track(self, library: Library) -> Library:
        """Remember ``library`` (for example the result of a set operation) for deletion."""
        library_id = library._data.get("id")
        if isinstance(library_id, str):
            self.created[library_id] = str(library._data.get("name"))
        return library

    def __enter__(self) -> Janitor:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Delete every tracked library, then any other library named with the tag."""
        with self.transport.cleanup():
            for library_id in list(self.created):
                self._delete(library_id)
            self._sweep()

    def _delete(self, library_id: str) -> None:
        if library_id in self._attempted:
            return
        self._attempted.add(library_id)
        try:
            Library(id=library_id).delete(client=self.client)
        except Exception as exc:
            self.problems.append(
                f"could not delete library {library_id} ({self.created.get(library_id)!r}): "
                f"{redact(str(exc), self.transport.hidden)}"
            )
        else:
            self.deleted.append(library_id)

    def _sweep(self) -> None:
        try:
            libraries = Library.select().using(self.client).all()
        except Exception as exc:
            self.problems.append(
                f"could not list libraries to look for leftovers named {self.tag}-*: "
                f"{redact(str(exc), self.transport.hidden)}"
            )
            return
        for library in libraries:
            library_id, name = library._data.get("id"), library._data.get("name")
            if isinstance(library_id, str) and isinstance(name, str) and name.startswith(self.tag + "-"):
                self.created.setdefault(library_id, name)
                self._delete(library_id)


@dataclass
class Findings:
    """Observations recorded by the live tests (accept/reject tables, measured limits), printed at the end."""

    entries: list[tuple[str, str]] = field(default_factory=list)

    def add(self, topic: str, text: str) -> None:
        """Record ``text`` under ``topic``."""
        self.entries.append((topic, text))

    def lines(self) -> list[str]:
        """The findings, one block per entry."""
        lines: list[str] = []
        for topic, text in self.entries:
            lines.append(f"[{topic}]")
            lines.extend("    " + line for line in text.splitlines())
        return lines
