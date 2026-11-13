"""The HTTP client and the single-purpose services: rate limits, authentication, export, resolver, vis, citation helper (items 16 and 17).

The vis and citation-helper services have much lower rate limits than search;
each test here makes one request.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from scixplorer import _config
from scixplorer.client import Client, Request
from scixplorer.exceptions import APIError, AuthenticationError
from scixplorer.search import QUERY_PATH
from scixplorer.services import citation_helper, export, resolver, vis

from .harness import PINNED, Outcome
from .support import Findings, LiveTransport, redact

pytestmark = pytest.mark.live

#: Two pinned records with few authors, for the network and citation-helper services.
PAIR = PINNED[:2]


def call(live_transport: LiveTransport, what: str, action: Any) -> Any:
    """``action()``, turning an APIError into an AssertionError that says which call failed and why."""
    try:
        return action()
    except APIError as exc:
        raise AssertionError(f"{what} failed: {redact(str(exc), live_transport.hidden)}") from None


def test_rate_limit_headers_are_parsed(live_client: Client, live_transport: LiveTransport, pinned_outcome: Outcome) -> None:
    """Search responses carry X-RateLimit-Limit/Remaining/Reset, recorded in client.rate_limits["search/query"].

    Depends on it: Client._observe and the token rotation that relies on it.
    """
    exchange = live_transport.last("search/query")
    assert exchange is not None and len(exchange.rate_headers) == 3, (
        f"rate-limit headers of the last search: {exchange.rate_headers if exchange else None}"
    )
    rate = live_client.rate_limits.get("search/query")
    now = datetime.now(timezone.utc)
    assert rate is not None and rate.limit > 0 and 0 <= rate.remaining <= rate.limit, rate
    assert now - timedelta(minutes=5) <= rate.reset <= now + timedelta(days=2), rate


def test_invalid_token_raises_authentication_error(
    live_tokens: tuple[str, ...], live_transport: LiveTransport
) -> None:
    """A request with an invalid token is answered 401 or 403, which the client raises as AuthenticationError.

    Depends on it: client._error_for. One request, with a deliberately bogus
    token (it runs only when a real token is configured, like every API test).
    """
    client = Client(
        "scixplorer-live-test-invalid-token",
        config=dataclasses.replace(_config.config, max_retries=0),
        transport=live_transport,
    )
    request = Request("GET", QUERY_PATH, params={"q": f'bibcode:"{PINNED[0]}"', "fl": "bibcode", "rows": 0})
    with pytest.raises(AuthenticationError):
        client.send(request)


def test_export_bibtex(live_client: Client, live_transport: LiveTransport, lead: dict[str, Any]) -> None:
    """POST /export/bibtex answers {"export": text}, a BibTeX entry keyed by the bibcode.

    Depends on it: services.export._export reading response["export"].
    """
    bibcode = str(lead["bibcode"])
    text = call(live_transport, "export.bibtex", lambda: export.bibtex(bibcode, client=live_client))
    assert isinstance(text, str) and "@" in text and bibcode in text, text[:500]


def test_resolver_lists_external_resources(
    live_client: Client, live_transport: LiveTransport, findings: Findings, lead: dict[str, Any]
) -> None:
    """GET /resolver/<bibcode> answers {"links": {"records": [...]}}: a list of dicts with a type or link_type.

    Depends on it: services.resolver.external_resources.
    """
    bibcode = str(lead["bibcode"])
    records = call(live_transport, "resolver.external_resources",
                   lambda: resolver.external_resources(bibcode, client=live_client))
    findings.add("resolver.external_resources", f"{len(records)} record(s); keys of the first: "
                 f"{sorted(records[0]) if records and isinstance(records[0], dict) else records[:1]}")
    assert isinstance(records, list) and records and all(isinstance(r, dict) for r in records), records


def test_resolver_answers_one_link_type_with_json(
    live_client: Client, live_transport: LiveTransport, findings: Findings, lead: dict[str, Any]
) -> None:
    """GET /resolver/<bibcode>/esource answers HTTP 200 with a JSON object, not a redirect.

    Depends on it: services.resolver.external_resource, whose client does not follow redirects.
    """
    bibcode = str(lead["bibcode"])
    try:
        answer = resolver.external_resource(bibcode, "esource", client=live_client)
    except APIError as exc:
        location = exc.response.headers.get("location") if exc.response is not None else None
        findings.add("resolver esource", f"HTTP {exc.status_code}, Location: {location}")
        raise AssertionError(
            f"the resolver answered HTTP {exc.status_code} (Location: {location}), not JSON; "
            f"the client does not follow redirects: {redact(str(exc), live_transport.hidden)}"
        ) from None
    findings.add("resolver esource", f"HTTP 200 JSON with keys {sorted(answer)}")
    assert isinstance(answer, dict) and ("link" in answer or "links" in answer), answer


def test_vis_author_network(live_client: Client, live_transport: LiveTransport, findings: Findings) -> None:
    """POST /vis/author-network with {"bibcodes": [...]} answers {"data": {...}}, a non-empty object.

    Depends on it: services.vis._request sending "bibcodes" (not "bibcode") and _network reading "data".
    """
    data = call(live_transport, "vis.author_network", lambda: vis.author_network(*PAIR, client=live_client))
    findings.add("vis.author_network", f"keys of data: {sorted(data) if isinstance(data, dict) else type(data)}")
    assert isinstance(data, dict) and data, data


def test_citation_helper(live_client: Client, live_transport: LiveTransport) -> None:
    """POST /citation_helper answers a list of {"bibcode": str, "score": int, "title": str, "author": str}.

    Depends on it: the shape documented by services.citation_helper (title and author are single strings).
    """
    suggestions = call(live_transport, "citation_helper", lambda: citation_helper(*PAIR, client=live_client))
    expected = {"bibcode": str, "score": int, "title": str, "author": str}
    bad = [s for s in suggestions if not all(isinstance(s.get(k), t) for k, t in expected.items())]
    assert isinstance(suggestions, list) and not bad, f"suggestions not of the documented shape: {bad[:3]}"
