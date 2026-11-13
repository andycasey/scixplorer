"""Offline tests of the live suite's safety plumbing (tests/live/support.py and harness.py).

They prove, against fake transports, the request budget guard, the stop after
HTTP 429, that library cleanup happens even when a test fails midway, that
the token never appears in what the suite shows, and that the default run
cannot reach the network.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from ads._config import Config
from ads.client import Client, Request
from ads.exceptions import APIError, RateLimitExceeded
from live.harness import Live
from live.support import BudgetExhausted, Janitor, LiveTransport, isolate, new_tag, peel, redact, scrub_report

API = "https://api.invalid/v1"
TOKEN = "plumbing-secret-token-0123456789"


def answer(status: int = 200, body: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    data = {"response": {"numFound": 0, "docs": []}} if body is None else body
    return httpx.Response(status, json=data, headers=headers)


class Server:
    """A fake API recording every request that reaches it."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response] = lambda request: answer()) -> None:
        self.handler = handler
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.handler(request)


def setup(server: Server, *, limit: int = 150, token: str = TOKEN) -> tuple[LiveTransport, Client]:
    transport = LiveTransport(httpx.MockTransport(server), api_url=API, limit=limit)
    transport.hidden = (token,)
    client = Client(token, config=Config(api_url=API, max_retries=0), transport=transport)
    return transport, client


def search(client: Client, q: str) -> Any:
    return client.send(Request("GET", "/search/query", params={"q": q, "rows": 0}))


# The request budget.


def test_budget_guard_refuses_to_send_over_the_limit() -> None:
    server = Server()
    transport, client = setup(server, limit=3)
    for q in ("a", "b", "c"):
        search(client, q)
    with pytest.raises(BudgetExhausted, match="hard limit is 3"):
        search(client, "d")
    assert len(server.seen) == 3 and transport.total == 3


def test_the_budget_guard_cannot_be_swallowed_by_except_exception() -> None:
    server = Server()
    transport, client = setup(server, limit=0)
    with pytest.raises(BudgetExhausted):
        try:
            search(client, "a")
        except Exception:
            pytest.fail("BudgetExhausted was caught by except Exception")
    assert server.seen == []


def test_repeated_searches_are_answered_from_the_cache_without_counting() -> None:
    server = Server()
    transport, client = setup(server, limit=1)
    for _ in range(5):
        assert search(client, "same").data == {"response": {"numFound": 0, "docs": []}}
    assert len(server.seen) == 1 and transport.total == 1 and transport.cache_hits == 4


def test_the_cache_is_per_token() -> None:
    server = Server()
    transport, client = setup(server)
    search(client, "same")
    other = Client("another-token-entirely", config=client.config, transport=transport)
    search(other, "same")
    assert len(server.seen) == 2


def test_server_errors_are_not_cached() -> None:
    server = Server(lambda request: answer(503, {"error": "busy"}))
    transport, client = setup(server)
    for _ in range(2):
        with pytest.raises(APIError):
            search(client, "same")
    assert len(server.seen) == 2 and transport.cache_hits == 0


def test_writes_are_never_cached() -> None:
    server = Server(lambda request: answer(200, {"id": "x"}))
    transport, client = setup(server)
    for _ in range(2):
        client.send(Request("POST", "/biblib/libraries", json={"name": "n"}))
    assert len(server.seen) == 2


def test_cleanup_requests_bypass_the_budget() -> None:
    server = Server()
    transport, client = setup(server, limit=1)
    search(client, "a")
    with transport.cleanup():
        client.send(Request("DELETE", "/biblib/documents/abc"))
    assert len(server.seen) == 2
    with pytest.raises(BudgetExhausted):
        search(client, "b")


def test_requests_are_counted_per_service() -> None:
    server = Server()
    transport, client = setup(server)
    search(client, "a")
    search(client, "b")
    client.send(Request("GET", "/biblib/libraries"))
    assert transport.counts == {"search/query": 2, "biblib/libraries": 1}
    assert any("total" in line and "3 sent" in line for line in transport.report_lines({}))


# The stop after HTTP 429.


def test_rate_limit_skips_the_test_with_the_reset_time() -> None:
    server = Server(lambda request: answer(429, {"error": "slow down"}, {"X-RateLimit-Reset": "1893456000"}))
    transport, client = setup(server)
    with pytest.raises(pytest.skip.Exception, match=r"429.*search/query.*2030-01-01 00:00 UTC"):
        search(client, "a")
    assert transport.stopped is not None


def test_nothing_more_is_sent_after_a_rate_limit() -> None:
    statuses = iter([429])
    server = Server(lambda request: answer(next(statuses, 200)))
    transport, client = setup(server)
    with pytest.raises(pytest.skip.Exception):
        search(client, "a")
    with pytest.raises(pytest.skip.Exception):
        client.send(Request("GET", "/biblib/libraries"))
    assert len(server.seen) == 1


def test_cleanup_still_runs_after_a_rate_limit_and_sees_its_own_429() -> None:
    server = Server(lambda request: answer(429, {"error": "slow down"}))
    transport, client = setup(server)
    with pytest.raises(pytest.skip.Exception):
        search(client, "a")
    with transport.cleanup(), pytest.raises(RateLimitExceeded):
        client.send(Request("DELETE", "/biblib/documents/abc"))
    assert len(server.seen) == 2


# Library cleanup.


class Biblib:
    """A fake library service: create, list, delete; optionally losing one create's answer or failing deletes."""

    def __init__(self, existing: dict[str, str], *, garble: str = "", refuse: str = "", body: str = "") -> None:
        self.libraries = dict(existing)
        self.garble = garble
        self.refuse = refuse
        self.body = body
        self.deleted: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        if request.method == "POST" and path == "/biblib/libraries":
            name = json.loads(request.content)["name"]
            library_id = f"id-{len(self.libraries)}"
            self.libraries[library_id] = name
            if self.garble and name.endswith(self.garble):
                return answer(200, {"unexpected": True})
            return answer(200, {"id": library_id, "name": name, "public": False})
        if request.method == "GET" and path == "/biblib/libraries":
            listing = [{"id": i, "name": n} for i, n in self.libraries.items()]
            return answer(200, {"libraries": listing})
        if request.method == "DELETE" and path.startswith("/biblib/documents/"):
            library_id = path.rsplit("/", 1)[1]
            if library_id == self.refuse:
                return answer(500, {"error": self.body or "cannot delete"})
            self.libraries.pop(library_id)
            self.deleted.append(library_id)
            return answer(200, {})
        return answer(599, {"error": f"unexpected {request.method} {path}"})


def test_janitor_deletes_its_libraries_when_a_test_fails_midway() -> None:
    older_run = "ads-live-test-20200101T000000Z-abcdef-first"
    fake = Biblib({"theirs": "Reading list", "older": older_run})
    transport, client = setup(Server(fake))
    with pytest.raises(AssertionError, match="midway"), Janitor(client, transport) as janitor:
        janitor.create("first", documents=["2020ApJ...900....1A"])
        janitor.create("second")
        raise AssertionError("midway")
    assert sorted(fake.deleted) == sorted(janitor.created) and len(fake.deleted) == 2
    assert fake.libraries == {"theirs": "Reading list", "older": older_run}
    assert janitor.problems == []


def test_janitor_deletes_a_library_whose_creation_answer_was_lost() -> None:
    fake = Biblib({"theirs": "Reading list"}, garble="lost")
    transport, client = setup(Server(fake))
    with Janitor(client, transport) as janitor, pytest.raises(APIError):
        janitor.create("lost")
    assert fake.libraries == {"theirs": "Reading list"} and len(fake.deleted) == 1
    assert janitor.problems == []


def test_janitor_keeps_deleting_after_one_delete_fails_and_reports_it() -> None:
    fake = Biblib({}, refuse="id-0")
    transport, client = setup(Server(fake))
    with Janitor(client, transport) as janitor:
        janitor.create("first")
        janitor.create("second")
    assert fake.deleted == ["id-1"]
    assert len(janitor.problems) == 1 and "id-0" in janitor.problems[0]


def test_janitor_cleans_up_after_the_budget_is_spent_and_after_a_rate_limit() -> None:
    fake = Biblib({})
    transport, client = setup(Server(fake), limit=2)
    with Janitor(client, transport) as janitor:
        janitor.create("first")
        janitor.create("second")
        transport.stopped = "rate limited"
    assert len(fake.deleted) == 2 and fake.libraries == {}


def test_session_tags_are_unique_and_recognisable() -> None:
    tags = {new_tag() for _ in range(20)}
    assert len(tags) == 20 and all(tag.startswith("ads-live-test-") for tag in tags)


# Redaction.


def test_redact_replaces_every_secret() -> None:
    assert redact(f"a {TOKEN} b {TOKEN}", [TOKEN, " "]) == "a [REDACTED] b [REDACTED]"


def echo_token(request: httpx.Request) -> httpx.Response:
    return answer(400, {"error": f"bad request from {request.headers['authorization']}"})


def test_server_error_text_is_redacted_in_outcomes_and_the_log() -> None:
    transport, client = setup(Server(echo_token))
    outcome = Live(client, transport).send(Request("GET", "/search/query", params={"q": "x"}), "x")
    assert outcome.error is not None and "[REDACTED]" in outcome.error
    assert TOKEN not in outcome.explain("expected") and TOKEN not in repr(transport.log)


def test_janitor_problems_are_redacted() -> None:
    fake = Biblib({}, refuse="id-0", body=f"no, {TOKEN}")
    transport, client = setup(Server(fake))
    with Janitor(client, transport) as janitor:
        janitor.create("first")
    assert janitor.problems and TOKEN not in " ".join(janitor.problems)


def report(longrepr: Any, outcome: str = "failed") -> pytest.TestReport:
    sections = [("Captured stdout call", f"printed {TOKEN}")]
    return pytest.TestReport("t.py::t", ("t.py", 1, "t"), {}, outcome, longrepr, "call", sections)


def test_failure_reports_are_scrubbed() -> None:
    failed = report(f"AssertionError: token was {TOKEN}")
    scrub_report(failed, [TOKEN])
    assert TOKEN not in str(failed.longrepr) and TOKEN not in str(failed.sections)
    skipped = report(("t.py", 1, f"Skipped: {TOKEN}"), "skipped")
    scrub_report(skipped, [TOKEN])
    assert skipped.longrepr == ("t.py", 1, "Skipped: [REDACTED]")


# Group testing.


def checker(failing: set[int], log: list[Sequence[int]]) -> Callable[[Sequence[int]], tuple[bool, str]]:
    def check(group: Sequence[int]) -> tuple[bool, str]:
        log.append(tuple(group))
        return not failing & set(group), f"group {tuple(group)}"

    return check


def test_isolate_needs_one_call_when_everything_passes() -> None:
    log: list[Sequence[int]] = []
    verdicts = isolate(range(10), checker(set(), log), cap=5)
    assert len(log) == 1 and {v.status for v in verdicts.values()} == {"pass"}


@pytest.mark.parametrize("failing", [{0}, {9}, {3, 4}, {0, 5, 9}, set(range(10))])
def test_isolate_finds_exactly_the_failing_items(failing: set[int]) -> None:
    log: list[Sequence[int]] = []
    verdicts = isolate(list(range(10)), checker(failing, log), cap=100)
    assert {item for item, v in verdicts.items() if v.status == "fail"} == failing
    assert all(v.status == "pass" for item, v in verdicts.items() if item not in failing)
    assert all(verdicts[item].alone and verdicts[item].evidence == f"group {(item,)}" for item in failing)


def test_isolate_respects_its_cap() -> None:
    log: list[Sequence[int]] = []
    verdicts = isolate(list(range(16)), checker(set(range(16)), log), cap=3)
    assert len(log) == 3
    assert {v.status for v in verdicts.values()} <= {"fail", "undetermined"}
    assert any(v.status == "undetermined" for v in verdicts.values())


def test_isolate_skips_rechecking_a_group_known_to_fail() -> None:
    log: list[Sequence[int]] = []
    verdicts = isolate([0, 1], checker({1}, log), cap=5, failure="known")
    assert log == [(0,), (1,)] and verdicts[1].status == "fail"


def naming(failing: set[str], log: list[Sequence[str]], *, names: bool = True) -> Callable[..., tuple[bool, str]]:
    """A check whose failure message names the first failing field, like Solr's "undefined field"."""

    def check(group: Sequence[str]) -> tuple[bool, str]:
        log.append(tuple(group))
        bad = [item for item in group if item in failing]
        if not bad:
            return True, "ok"
        return False, f'undefined field "{bad[0]}_nosyn"' if names else "syntax error"

    return check


def test_peel_costs_one_call_per_named_failure_plus_one() -> None:
    log: list[Sequence[str]] = []
    fields = ["aff", "aff_abbrev", "title", "email", "orcid", "orcid_pub"]
    verdicts = peel(fields, naming({"aff_abbrev", "orcid"}, log), cap=10, message=lambda text: text)
    assert {f for f, v in verdicts.items() if v.status == "fail"} == {"aff_abbrev", "orcid"}
    assert {f for f, v in verdicts.items() if v.status == "pass"} == {"aff", "title", "email", "orcid_pub"}
    assert len(log) == 3


def test_peel_falls_back_to_bisection_when_the_message_names_nothing() -> None:
    log: list[Sequence[str]] = []
    fields = [f"f{i}" for i in range(8)]
    verdicts = peel(fields, naming({"f5"}, log, names=False), cap=10, message=lambda text: text)
    assert {f for f, v in verdicts.items() if v.status == "fail"} == {"f5"}
    assert all(v.status == "pass" for f, v in verdicts.items() if f != "f5")


def test_peel_respects_its_cap() -> None:
    log: list[Sequence[str]] = []
    verdicts = peel(["a", "b", "c", "d"], naming({"a", "b", "c", "d"}, log), cap=2, message=lambda text: text)
    assert len(log) == 2 and [verdicts[f].status for f in "abcd"] == ["fail", "fail", "undetermined", "undetermined"]


def test_isolate_of_nothing_makes_no_call() -> None:
    log: list[Sequence[int]] = []
    assert isolate([], checker(set(), log), cap=3) == {} and log == []


# The network guard of the default run.


def test_network_is_blocked_without_live(pytestconfig: pytest.Config) -> None:
    if pytestconfig.getoption("live"):
        pytest.skip("the network is only blocked without --live")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(Exception, match="blocked unless the tests run with --live"):
            sock.connect(("127.0.0.1", 9))
    with pytest.raises(Exception, match="blocked unless the tests run with --live"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(Exception, match="blocked unless the tests run with --live"):
        httpx.get("https://example.com", timeout=1)
