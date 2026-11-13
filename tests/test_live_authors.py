"""Offline tests of the per-author live checks (tests/live/authors.py), against a fake index.

The fake answers the requests those checks send, through the live suite's own
client and counting transport: the arrays of the pinned records, and counts
for pos() clauses, rejecting or mis-answering the fields it is told to. This
exercises the request building, the request caps, and how answers become
verdicts; what the real server answers is only known from a --live run.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from scixplorer._config import Config
from scixplorer.client import Client
from scixplorer.models.document import Document
from scixplorer.search import to_solr
from live import authors
from live.harness import PINNED, Live
from live.support import LiveTransport

API = "https://api.invalid/v1"

FIRST: dict[str, Any] = {
    "bibcode": PINNED[0],
    "author": ["Ada, A.", "Bea, B.", "Cy, C."],
    "aff": ["Inst X", "Inst Y; Inst Z", "-"],
    "aff_id": ["A00001", "A00002; A00003", "-"],
    "email": ["-", "bea@example.org", "-"],
    "orcid_pub": ["-", "-", "0000-0001-0000-0001"],
    "orcid_user": ["0000-0002-0000-0002", "-", "-"],
    "orcid_other": ["-", "-", "-"],
}
SECOND: dict[str, Any] = {"bibcode": PINNED[1], "author": ["Dee, D."], "aff": ["Inst W"]}


class FakeIndex:
    """Answers the per-author live checks: the records (rows > 0), and counts for pos() clauses.

    ``rejected``: pos() on these fields is an error (naming the field).
    ``ignored``: pos() on these fields matches at any position.
    ``missed``: pos() on these fields never matches.
    """

    def __init__(self, *, rejected: set[str] = set(), ignored: set[str] = set(), missed: set[str] = set()) -> None:
        self.rejected, self.ignored, self.missed = rejected, ignored, missed

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        q, fields = params["q"], params["fl"].split(",")
        if params["rows"] != "0":
            docs = [{k: v for k, v in doc.items() if k in fields} for doc in (FIRST, SECOND)]
            return httpx.Response(200, json={"response": {"numFound": len(docs), "docs": docs}})
        for field in self.rejected:
            if f"pos({field}:" in q:
                return httpx.Response(400, json={"error": f"pos() is not supported on field {field}"})
        anchors = q.count('bibcode:"') - q.count('OR bibcode:"')
        failing = any(f"-pos({field}:" in q for field in self.ignored) or any(
            re.search(rf'(?<!-)pos\({field}:"(?!zzzzzz)', q) for field in self.missed
        )
        found = max(anchors - 1, 0) if failing else anchors
        return httpx.Response(200, json={"response": {"numFound": found, "docs": []}})


def run(index: FakeIndex) -> tuple[dict[str, tuple[str, str]], dict[str, Any], LiveTransport]:
    transport = LiveTransport(httpx.MockTransport(index), api_url=API)
    client = Client("token", config=Config(api_url=API, max_retries=0), transport=transport)
    live = Live(client, transport)
    full_records = live.search(Document.bibcode.in_(PINNED), rows=len(PINNED)).require("records")
    records = authors.records_of(full_records)
    checks = authors.items(records)
    verdicts = authors.check(live, checks)
    return authors.position_verdicts(checks, verdicts), verdicts, transport


def statuses(found: dict[str, tuple[str, str]]) -> dict[str, str]:
    return {field: status for field, (status, _) in found.items()}


def test_everything_holds_with_one_request() -> None:
    found, verdicts, transport = run(FakeIndex())
    assert statuses(found) == {
        "aff_id": "works",
        "aff": "works",
        "email": "works",
        "orcid": "works",
        "orcid_pub": "works",
        "orcid_user": "works",
        "orcid_other": "untested",
    }
    assert verdicts["aff_id_set"].status == verdicts["aff_id_several"].status == "pass"
    assert transport.total == 2  # the records (the shared full_records fixture), then one check request


def test_failures_are_told_apart_with_one_request_per_check() -> None:
    found, verdicts, transport = run(FakeIndex(rejected={"email"}, ignored={"orcid"}, missed={"aff"}))
    assert found["email"] == ("fails", "pos() rejected: 400 GET /search/query: pos() is not supported on field email")
    for field in ("orcid", "aff"):
        assert found[field] == (
            "fails",
            "pos() accepted, but it does not test the position (it must match at the author's position and not at another)",
        )
    assert [statuses(found)[f] for f in ("aff_id", "orcid_pub", "orcid_user", "orcid_other")] == [
        "works", "works", "works", "untested"
    ]
    assert verdicts["aff_id_set"].status == verdicts["aff_id_several"].status == "pass"
    assert transport.total == 2 + len(verdicts) == 1 + authors.BUDGET


@pytest.mark.parametrize(
    "index",
    [
        FakeIndex(rejected=set(authors.POSITION_FIELDS)),
        FakeIndex(missed=set(authors.POSITION_FIELDS)),
        FakeIndex(ignored=set(authors.POSITION_FIELDS)),
    ],
)
def test_every_field_gets_a_verdict_within_the_budget(index: FakeIndex) -> None:
    found, verdicts, transport = run(index)
    assert transport.total - 1 <= authors.BUDGET == 10
    assert all(verdict.status in ("pass", "fail") for verdict in verdicts.values())
    expected = "untested" if index.missed or index.ignored else "fails"
    assert statuses(found) == {field: "fails" for field in authors.POSITION_FIELDS} | {"orcid_other": expected}


def test_probe_clauses() -> None:
    checks = {item.name: item for item in authors.items([FIRST, SECOND])}
    clauses = {name: [(to_solr(p.clause), p.negate, p.anchor) for p in item.probes] for name, item in checks.items()}
    assert clauses["aff"] == [('pos(aff:"Inst Y; Inst Z", 2)', False, PINNED[0]), ('pos(aff:"Inst Y; Inst Z", 1)', True, PINNED[0])]
    assert clauses["aff_id"][0][0] == 'pos(aff_id:"A00002", 2)'
    assert clauses["orcid"][0][0] == 'pos(orcid:"0000-0001-0000-0001", 3)'
    assert clauses["email"][0][0] == 'pos(email:"bea@example.org", 2)'
    assert clauses["orcid_other"] == [(f'pos(orcid_other:"zzzzzz", 1) OR bibcode:"{PINNED[0]}"', False, PINNED[0])]
    assert not checks["orcid_other"].tested
    assert clauses["aff_id_set"] == [('pos(aff_id:("A00002" OR "Z99999"), 2)', False, PINNED[0])]
    assert clauses["aff_id_set"][0][0] == to_solr(Document.authors[1].aff_id.in_(["A00002", "Z99999"]))
    assert [c for c, _, _ in clauses["aff_id_several"]] == ['aff_id:"A00002"', 'aff_id:"A00003"']
    assert len(checks) == authors.BUDGET - 1


def test_position_case_prefers_a_later_author_and_another_without_the_value() -> None:
    case = authors.position_case([FIRST], "orcid")
    assert case == authors.PositionCase("orcid", PINNED[0], "0000-0001-0000-0001", 2, 0)
    assert authors.position_case([FIRST], "aff_id") == authors.PositionCase("aff_id", PINNED[0], "A00002", 1, 0)
    assert authors.position_case([FIRST], "orcid_other") is None
    assert authors.position_case([SECOND], "aff") is None


def test_alignment_and_placeholders() -> None:
    assert authors.alignment_problems([FIRST, SECOND]) == []
    short = {**FIRST, "email": ["-"], "aff": "Inst X"}
    assert authors.alignment_problems([short]) == [
        f"{PINNED[0]} aff: not a list ('Inst X')",
        f"{PINNED[0]} email: 1 entries for 3 authors",
    ]
    problems, dashes = authors.placeholder_problems([{**FIRST, "email": ["", None, "-"]}])
    assert problems == [
        f"{PINNED[0]} email[0] is '' (expected a value or '-')",
        f"{PINNED[0]} email[1] is None (expected a value or '-')",
    ]
    assert dashes == {"aff": 1, "aff_id": 1, "email": 1, "orcid_pub": 2, "orcid_user": 2, "orcid_other": 3}
    assert authors.delimiters([FIRST]) == (
        "aff: 1 entries contain ';'; e.g. 'Inst Y; Inst Z'\naff_id: 1 entries contain ';'; e.g. 'A00002; A00003'"
    )
    assert PINNED[0] in authors.presence([FIRST, SECOND])
    assert authors.several_ids([SECOND]) is None
