from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from scixplorer import __version__
from scixplorer.client import (
    Client,
    RateLimit,
    Request,
    Response,
    get_default_client,
    service_for,
    set_default_client,
)
from scixplorer._config import TOKEN_ENV_VARS, Config
from scixplorer._config import config as module_config
from scixplorer.exceptions import APIError, AuthenticationError, RateLimitExceeded

TOKEN = "s3cret-token-value"
SEARCH = Request("GET", "/search/query", params={"q": "star"})
RESET = 1_700_000_000
RATE_HEADERS = {
    "X-RateLimit-Limit": "5000",
    "X-RateLimit-Remaining": "4321",
    "X-RateLimit-Reset": str(RESET),
}


def reply(status: int = 200, body: Any = None, **kwargs: Any) -> httpx.Response:
    """An httpx response; ``body`` is JSON-encoded unless it is str/bytes."""
    if body is None or isinstance(body, (str, bytes)):
        return httpx.Response(status, content=body or b"", **kwargs)
    return httpx.Response(status, json=body, **kwargs)


class Harness:
    """A fake ADS server plus a way to drive a Client through it, sync or async.

    The ``mode`` decides whether :meth:`send` uses ``Client.send`` or
    ``Client.asend``, so one test body covers both code paths.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.replies: list[httpx.Response | Exception] = []
        self.requests: list[httpx.Request] = []
        self.delays: list[float] = []
        self.clock = datetime.fromtimestamp(RESET - 3600, tz=timezone.utc)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        next_reply = self.replies.pop(0)
        if isinstance(next_reply, Exception):
            raise next_reply
        return next_reply

    def client(
        self,
        *replies: httpx.Response | Exception,
        token: str | Iterable[str] | None = TOKEN,
        config: Config | None = None,
        **settings: Any,
    ) -> Client:
        self.replies.extend(replies)
        transport = httpx.MockTransport(self.handler)

        async def async_sleep(delay: float) -> None:
            self.delays.append(delay)

        return Client(
            token,
            config=config if config is not None else Config(**settings),
            transport=transport,
            async_transport=transport,
            sleep=self.delays.append,
            async_sleep=async_sleep,
            now=lambda: self.clock,
        )

    def add(self, *replies: httpx.Response | Exception) -> None:
        """Queue more replies for a client created earlier."""
        self.replies.extend(replies)

    def send(self, client: Client, request: Request = SEARCH) -> Response:
        if self.mode == "sync":
            with client:
                return client.send(request)

        async def go() -> Response:
            async with client:
                return await client.asend(request)

        return asyncio.run(go())

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def auths(self) -> list[str]:
        """The ``Authorization`` header of every request the server received."""
        return [r.headers["Authorization"] for r in self.requests]


@pytest.fixture(params=["sync", "async"])
def harness(request: pytest.FixtureRequest) -> Harness:
    return Harness(request.param)


@pytest.fixture
def no_token_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No token in the environment and an empty home directory."""
    for name in TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def reset_default_client():
    set_default_client(None)
    yield
    set_default_client(None)


# --- Request validation (pure, no I/O) -------------------------------------


@pytest.mark.parametrize("value", [["a", "b"], ("a",), {"a": 1}, b"bytes", object()])
def test_request_rejects_unsupported_param_types(value):
    with pytest.raises(TypeError, match="'fl'"):
        Request("GET", "/search/query", params={"fl": value})


def test_request_rejects_json_and_content_together():
    with pytest.raises(ValueError):
        Request("POST", "/x", json={"a": 1}, content="b")


def test_service_for():
    assert service_for("/search/query") == "search/query"
    assert service_for("search/bigquery") == "search/bigquery"
    assert service_for("/biblib/libraries/abc123") == "biblib/libraries"
    assert service_for("/resolver/2015ApJ...808...16N") == "resolver"
    assert service_for("/resolver/2015ApJ...808...16N/esource") == "resolver"
    assert service_for("/biblib/") == "biblib"


# --- Building the request ---------------------------------------------------


@pytest.mark.parametrize("base", ["https://ads.test/v1", "https://ads.test/v1/"])
@pytest.mark.parametrize("path", ["/search/query", "search/query"])
def test_url_joining(harness, base, path):
    harness.send(harness.client(reply(), api_url=base), Request("GET", path))
    assert str(harness.last.url) == "https://ads.test/v1/search/query"


def test_auth_and_user_agent_headers(harness):
    harness.send(harness.client(reply()))
    assert harness.last.headers["Authorization"] == f"Bearer {TOKEN}"
    assert harness.last.headers["User-Agent"] == f"scixplorer/{__version__}"


def test_caller_headers_override_defaults_but_not_authorization(harness):
    request = Request(
        "GET",
        "/x",
        headers={"user-agent": "custom/1", "authorization": "Bearer other", "X-Extra": "1"},
    )
    harness.send(harness.client(reply()), request)
    assert harness.last.headers.get_list("User-Agent") == ["custom/1"]
    assert harness.last.headers.get_list("Authorization") == [f"Bearer {TOKEN}"]
    assert harness.last.headers["X-Extra"] == "1"


def test_params_encoding(harness):
    params = {"q": "a b", "rows": 10, "frac": 0.5, "on": True, "off": False, "skip": None}
    harness.send(harness.client(reply()), Request("GET", "/x", params=params))
    assert harness.last.url.params.multi_items() == [
        ("q", "a b"),
        ("rows", "10"),
        ("frac", "0.5"),
        ("on", "true"),
        ("off", "false"),
    ]


def test_json_body(harness):
    body = {"bibcode": ["A", "B"], "format": "%R"}
    harness.send(harness.client(reply()), Request("POST", "/export/custom", json=body))
    assert harness.last.method == "POST"
    assert harness.last.headers["Content-Type"] == "application/json"
    assert json.loads(harness.last.content) == body


@pytest.mark.parametrize("content", ["bibcode\nA\nB", b"bibcode\nA\nB"])
def test_raw_content_body_with_custom_content_type(harness, content):
    request = Request(
        "POST", "/search/bigquery", content=content, headers={"Content-Type": "big-query/csv"}
    )
    harness.send(harness.client(reply()), request)
    assert harness.last.content == b"bibcode\nA\nB"
    assert harness.last.headers.get_list("Content-Type") == ["big-query/csv"]


def test_no_body_for_plain_get(harness):
    harness.send(harness.client(reply()))
    assert harness.last.content == b""
    assert "Content-Type" not in harness.last.headers


def test_timeout_taken_from_config(harness):
    harness.send(harness.client(reply(), timeout=7.5))
    assert harness.last.extensions["timeout"] == httpx.Timeout(7.5).as_dict()


def test_config_read_at_send_time(harness):
    config = Config(api_url="https://before.test")
    client = harness.client(reply(), config=config)
    config.api_url = "https://after.test/v1"
    harness.send(client)
    assert harness.last.url.host == "after.test"


def test_module_config_is_the_default(monkeypatch):
    client = Client(TOKEN)
    assert client.config is module_config
    monkeypatch.setattr(module_config, "api_url", "https://mutated.test")
    assert repr(client) == "<Client api_url='https://mutated.test'>"


# --- Parsing the response ---------------------------------------------------


def test_json_response_regardless_of_content_type(harness):
    response = harness.send(
        harness.client(reply(200, b'{"a": [1, 2]}', headers={"Content-Type": "text/plain"}))
    )
    assert response == Response(
        200, {"content-type": "text/plain", "content-length": "13"}, {"a": [1, 2]}
    )


def test_text_response(harness):
    response = harness.send(harness.client(reply(200, "@ARTICLE{...}")))
    assert response.data == "@ARTICLE{...}"


@pytest.mark.parametrize("body", [b"", b"  \n"])
def test_empty_response(harness, body):
    assert harness.send(harness.client(reply(200, body))).data is None


def test_headers_are_lower_cased(harness):
    response = harness.send(harness.client(reply(headers={"X-Custom": "v"})))
    assert response.headers["x-custom"] == "v"
    assert all(key == key.lower() for key in response.headers)


# --- Errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"error": {"msg": "undefined field foo", "code": 400}}, "undefined field foo"),
        ({"error": "bad query"}, "bad query"),
        ({"message": "from message"}, "from message"),
        ({"msg": "from msg"}, "from msg"),
        ({"error": "", "message": "skip empty error"}, "skip empty error"),
        ({"other": 1}, '{"other": 1}'),
        ("  plain\n  text  ", "plain text"),
        (None, "Bad Request"),
    ],
)
def test_error_message_extraction(harness, body, detail):
    with pytest.raises(APIError) as info:
        harness.send(harness.client(reply(400, body)))
    error = info.value
    assert type(error) is APIError
    assert str(error) == f"400 GET /search/query: {detail}"
    assert error.status_code == 400
    assert isinstance(error.response, Response)
    assert error.response.status_code == 400


def test_long_error_text_is_truncated(harness):
    with pytest.raises(APIError) as info:
        harness.send(harness.client(reply(404, "x" * 5000)))
    assert len(str(info.value)) < 600
    assert str(info.value).endswith("...")


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors(harness, status):
    with pytest.raises(AuthenticationError) as info:
        harness.send(harness.client(reply(status, {"error": "Unauthorized"})))
    assert info.value.status_code == status
    assert str(info.value).startswith(f"{status} GET /search/query:")


def test_not_found(harness):
    with pytest.raises(APIError) as info:
        harness.send(harness.client(reply(404, {"error": "no such library"})))
    assert type(info.value) is APIError
    assert info.value.status_code == 404


def test_rate_limit_exceeded_raised_immediately(harness):
    headers = {**RATE_HEADERS, "X-RateLimit-Remaining": "0"}
    client = harness.client(reply(429, "Too many requests", headers=headers))
    with pytest.raises(RateLimitExceeded) as info:
        harness.send(client)
    error = info.value
    assert error.status_code == 429
    assert (error.limit, error.remaining) == (5000, 0)
    assert error.reset == datetime.fromtimestamp(RESET, tz=timezone.utc)
    assert "resets at 2023-11-14T22:13:20+00:00" in str(error)
    assert len(harness.requests) == 1
    assert harness.delays == []


def test_rate_limit_exceeded_without_headers(harness):
    with pytest.raises(RateLimitExceeded) as info:
        harness.send(harness.client(reply(429)))
    assert (info.value.limit, info.value.remaining, info.value.reset) == (None, None, None)


@pytest.mark.parametrize("status", [400, 404, 500])
def test_non_transient_errors_not_retried(harness, status):
    with pytest.raises(APIError):
        harness.send(harness.client(reply(status), reply(200)))
    assert len(harness.requests) == 1
    assert harness.delays == []


# --- Retries ----------------------------------------------------------------


def test_retry_then_success(harness):
    client = harness.client(reply(503), reply(502), reply(200, {"ok": True}))
    assert harness.send(client).data == {"ok": True}
    assert len(harness.requests) == 3
    assert harness.delays == [1.0, 2.0]


def test_retry_exhaustion_raises_last_error_with_backoff(harness):
    client = harness.client(
        reply(502), reply(503), reply(503), reply(504, {"error": "gateway timeout"}),
        retry_backoff=0.5, max_retries=3,
    )
    with pytest.raises(APIError) as info:
        harness.send(client)
    assert info.value.status_code == 504
    assert str(info.value) == "504 GET /search/query: gateway timeout"
    assert len(harness.requests) == 4
    assert harness.delays == [0.5, 1.0, 2.0]


def test_transport_errors_retried(harness):
    client = harness.client(
        httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), reply(200, {"ok": 1})
    )
    assert harness.send(client).data == {"ok": 1}
    assert harness.delays == [1.0, 2.0]


def test_transport_error_exhaustion(harness):
    client = harness.client(
        httpx.ConnectError("refused"), httpx.ConnectTimeout("timed out"), max_retries=1
    )
    with pytest.raises(APIError) as info:
        harness.send(client)
    error = info.value
    assert type(error) is APIError
    assert error.status_code is None
    assert isinstance(error.__cause__, httpx.ConnectTimeout)
    assert str(error) == "GET /search/query: ConnectTimeout: timed out (gave up after 2 attempts)"
    assert harness.delays == [1.0]


def test_no_retries_when_max_retries_is_zero(harness):
    with pytest.raises(APIError):
        harness.send(harness.client(reply(503), reply(200), max_retries=0))
    assert len(harness.requests) == 1
    assert harness.delays == []


# --- Idempotency -----------------------------------------------------------

CREATE = Request("POST", "/biblib/libraries", json={"name": "new"})


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", True),
        ("HEAD", True),
        ("PUT", True),
        ("DELETE", True),
        ("delete", True),
        ("POST", False),
        ("PATCH", False),
    ],
)
def test_idempotency_inferred_from_method(method, expected):
    assert Request(method, "/x").is_idempotent is expected


def test_idempotency_can_be_set_explicitly():
    assert Request("POST", "/search/bigquery", idempotent=True).is_idempotent is True
    assert Request("GET", "/x", idempotent=False).is_idempotent is False


@pytest.mark.parametrize(
    "error",
    [
        lambda: httpx.ReadTimeout("slow"),
        lambda: httpx.WriteTimeout("slow"),
        lambda: httpx.ReadError("reset"),
        lambda: httpx.RemoteProtocolError("closed"),
    ],
    ids=["ReadTimeout", "WriteTimeout", "ReadError", "RemoteProtocolError"],
)
def test_post_not_retried_after_request_may_have_been_sent(harness, error):
    client = harness.client(error(), reply(200))
    with pytest.raises(APIError) as info:
        harness.send(client, CREATE)
    assert info.value.status_code is None
    assert isinstance(info.value.__cause__, httpx.TransportError)
    assert str(info.value).startswith("POST /biblib/libraries: ")
    assert "may or may not have been applied" in str(info.value)
    assert len(harness.requests) == 1
    assert harness.delays == []


@pytest.mark.parametrize(
    "error",
    [lambda: httpx.ConnectError("refused"), lambda: httpx.ConnectTimeout("timed out")],
    ids=["ConnectError", "ConnectTimeout"],
)
def test_post_retried_when_never_sent(harness, error):
    client = harness.client(error(), error(), reply(200, {"id": "abc"}))
    assert harness.send(client, CREATE).data == {"id": "abc"}
    assert len(harness.requests) == 3
    assert harness.delays == [1.0, 2.0]


def test_post_connect_error_exhaustion(harness):
    client = harness.client(httpx.ConnectError("a"), httpx.ConnectError("b"), max_retries=1)
    with pytest.raises(APIError) as info:
        harness.send(client, CREATE)
    assert str(info.value) == "POST /biblib/libraries: ConnectError: b (gave up after 2 attempts)"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_post_not_retried_on_transient_status(harness, status):
    with pytest.raises(APIError) as info:
        harness.send(harness.client(reply(status), reply(200)), CREATE)
    assert info.value.status_code == status
    assert len(harness.requests) == 1
    assert harness.delays == []


@pytest.mark.parametrize(
    "request_",
    [
        Request("POST", "/search/bigquery", content="bibcode\nA", idempotent=True),
        Request("GET", "/search/query"),
        Request("PUT", "/biblib/documents/abc", json={"name": "renamed"}),
        Request("DELETE", "/biblib/documents/abc"),
    ],
    ids=["POST-idempotent", "GET", "PUT", "DELETE"],
)
def test_idempotent_requests_retried_on_transient_status(harness, request_):
    assert harness.send(harness.client(reply(503), reply(200)), request_).status_code == 200
    assert len(harness.requests) == 2
    assert harness.delays == [1.0]


def test_explicitly_idempotent_post_retried_after_read_timeout(harness):
    request = Request("POST", "/search/bigquery", content="bibcode", idempotent=True)
    client = harness.client(httpx.ReadTimeout("slow"), reply(200))
    assert harness.send(client, request).status_code == 200
    assert harness.delays == [1.0]


def test_explicitly_non_idempotent_get_not_retried(harness):
    with pytest.raises(APIError):
        harness.send(harness.client(reply(503), reply(200)), Request("GET", "/x", idempotent=False))
    assert len(harness.requests) == 1


# --- Rate limits ------------------------------------------------------------


def test_rate_limits_recorded_per_service(harness):
    client = harness.client(
        reply(headers=RATE_HEADERS),
        reply(headers={**RATE_HEADERS, "X-RateLimit-Remaining": "99"}),
    )
    harness.send(client, Request("GET", "/search/query"))
    harness.send(client, Request("GET", "/biblib/libraries/abc123"))
    reset = datetime.fromtimestamp(RESET, tz=timezone.utc)
    assert client.rate_limits == {
        "search/query": RateLimit(5000, 4321, reset),
        "biblib/libraries": RateLimit(5000, 99, reset),
    }


@pytest.mark.parametrize("status", [404, 429, 500])
def test_rate_limits_recorded_on_error_responses(harness, status):
    client = harness.client(reply(status, headers=RATE_HEADERS))
    with pytest.raises(APIError):
        harness.send(client)
    assert client.rate_limits["search/query"].remaining == 4321


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "1"},
        {**RATE_HEADERS, "X-RateLimit-Limit": "lots"},
        {**RATE_HEADERS, "X-RateLimit-Reset": "soon"},
        {**RATE_HEADERS, "X-RateLimit-Reset": "1e300"},
    ],
)
def test_missing_or_bad_rate_limit_headers_ignored(harness, headers):
    client = harness.client(reply(headers=headers))
    harness.send(client)
    assert client.rate_limits == {}


# --- Tokens -----------------------------------------------------------------


def test_missing_token_raises_before_any_request(harness, no_token_env):
    client = harness.client(reply(), token=None)
    with pytest.raises(AuthenticationError) as info:
        harness.send(client)
    assert info.value.status_code is None
    message = str(info.value)
    assert "SCIX_API_TOKEN, ADS_API_TOKEN, ADS_DEV_KEY (checked in that order)" in message
    assert "~/.scix/token, ~/.ads/token, ~/.ads/dev_key (checked in that order)" in message
    assert "scixplorer.config.token" in message
    assert harness.requests == []


def test_token_argument_beats_config(harness, no_token_env):
    harness.send(harness.client(reply(), token="from-arg", config=Config(token="from-config")))
    assert harness.last.headers["Authorization"] == "Bearer from-arg"


def test_token_from_config(harness, no_token_env):
    harness.send(harness.client(reply(), token=None, config=Config(token="from-config")))
    assert harness.last.headers["Authorization"] == "Bearer from-config"


def test_token_resolved_at_send_time(harness, no_token_env, monkeypatch):
    client = harness.client(reply(), token=None)
    monkeypatch.setenv("ADS_DEV_KEY", "from-env")
    (no_token_env / ".ads").mkdir()
    (no_token_env / ".ads" / "token").write_text("from-file\n")
    harness.send(client)
    assert harness.last.headers["Authorization"] == "Bearer from-env"


def test_token_from_file(harness, no_token_env):
    (no_token_env / ".ads").mkdir()
    (no_token_env / ".ads" / "dev_key").write_text("from-file\n")
    harness.send(harness.client(reply(), token=None))
    assert harness.last.headers["Authorization"] == "Bearer from-file"


@pytest.mark.parametrize(
    "outcome",
    [
        lambda: reply(401, {"error": "Unauthorized"}),
        lambda: reply(429, headers=RATE_HEADERS),
        lambda: reply(500, "Internal Server Error"),
        lambda: httpx.ConnectError("refused"),
    ],
    ids=["401", "429", "500", "transport"],
)
def test_token_never_in_repr_or_messages(harness, outcome):
    client = harness.client(outcome(), token=None, config=Config(token=TOKEN, max_retries=0))
    assert TOKEN not in repr(client)
    assert TOKEN not in repr(client.config)
    with pytest.raises(APIError) as info:
        harness.send(client)
    assert TOKEN not in str(info.value)
    assert TOKEN not in repr(info.value)


# --- Several tokens ---------------------------------------------------------

T1, T2, T3 = "s3cret-one", "s3cret-two", "s3cret-three"
EXPORT = Request("GET", "/export/bibtex/2020ApJ...1A")


def bearer(*tokens: str) -> list[str]:
    return [f"Bearer {token}" for token in tokens]


def limited(reset: int | None = RESET) -> httpx.Response:
    """An HTTP 429, reporting ``reset`` unless it is ``None`` (then no rate-limit headers)."""
    if reset is None:
        return reply(429, {"error": "Too many requests"})
    headers = {**RATE_HEADERS, "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)}
    return reply(429, {"error": "Too many requests"}, headers=headers)


def at(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def assert_no_token_text(client: Client, error: BaseException | None = None) -> None:
    texts = [repr(client), repr(client.config)]
    if error is not None:
        texts += [str(error), repr(error), repr(getattr(error, "response", None))]
    assert not any("s3cret" in text for text in texts)


def test_token_iterable_stripped_deduplicated_blanks_dropped(harness):
    client = harness.client(limited(), reply(), token=["", f" {T2} ", T2, "  ", T1])
    harness.send(client)
    assert harness.auths == bearer(T2, T1)


def test_token_generator_consumed_once(harness):
    client = harness.client(reply(), reply(), token=(t for t in [T1, T2]))
    harness.send(client)
    harness.send(client)
    assert harness.auths == bearer(T1, T1)


@pytest.mark.parametrize("token", [b"s3cret-bytes", [T1, b"s3cret-bytes"], [T1, 12345]])
def test_bad_token_types_raise_type_error_before_sending(harness, token):
    client = harness.client(reply(), token=token)
    with pytest.raises(TypeError) as info:
        harness.send(client)
    assert harness.requests == []
    assert "12345" not in str(info.value)
    assert_no_token_text(client, info.value)


def test_429_rotates_to_next_token_without_sleeping(harness):
    second = {**RATE_HEADERS, "X-RateLimit-Remaining": "4999"}
    client = harness.client(limited(), reply(200, {"ok": 1}, headers=second), token=[T1, T2])
    response = harness.send(client)
    assert response.data == {"ok": 1}
    assert harness.auths == bearer(T1, T2)
    assert harness.delays == []
    assert client.rate_limits["search/query"].remaining == 4999


def test_post_rotates_on_429(harness):
    client = harness.client(limited(), reply(200, {"id": "abc"}), token=[T1, T2])
    assert harness.send(client, Request("POST", "/biblib/libraries", json={})).data == {"id": "abc"}
    assert harness.auths == bearer(T1, T2)


def test_exhaustion_is_per_service(harness):
    client = harness.client(limited(), reply(), reply(), reply(), token=[T1, T2])
    harness.send(client)
    harness.send(client, EXPORT)
    harness.send(client)
    assert harness.auths == bearer(T1, T2, T1, T2)


def test_exhaustion_expires_at_reset(harness):
    client = harness.client(limited(RESET), reply(), reply(), reply(), token=[T1, T2])
    harness.send(client)
    harness.clock = at(RESET - 1)
    harness.send(client)
    harness.clock = at(RESET)
    harness.send(client)
    assert harness.auths == bearer(T1, T2, T2, T1)


def test_missing_reset_header_assumes_sixty_seconds(harness):
    start = harness.clock
    client = harness.client(limited(None), reply(), reply(), reply(), token=[T1, T2])
    harness.send(client)
    harness.clock = start + timedelta(seconds=59)
    harness.send(client)
    harness.clock = start + timedelta(seconds=60)
    harness.send(client)
    assert harness.auths == bearer(T1, T2, T2, T1)


def test_all_tokens_exhausted(harness):
    client = harness.client(limited(RESET + 100), limited(RESET + 50), token=[T1, T2])
    with pytest.raises(RateLimitExceeded) as info:
        harness.send(client)
    error = info.value
    assert error.status_code == 429
    assert error.response is not None and error.response.status_code == 429
    assert (error.limit, error.remaining, error.reset) == (5000, 0, at(RESET + 50))
    assert harness.auths == bearer(T1, T2)
    assert_no_token_text(client, error)

    with pytest.raises(RateLimitExceeded) as info:
        harness.send(client)
    error = info.value
    assert len(harness.requests) == 2
    assert error.response is None
    assert error.status_code == 429
    assert (error.limit, error.remaining, error.reset) == (5000, 0, at(RESET + 50))
    assert str(error).startswith("429 GET /search/query: ")
    assert_no_token_text(client, error)

    harness.clock = at(RESET + 50)
    harness.add(reply())
    harness.send(client)
    assert harness.auths == bearer(T1, T2, T2)



def test_429_with_past_reset_does_not_loop(harness):
    """A reset already passed by the local clock (skew) must not cause endless re-sends."""
    past = RESET - 7200
    client = harness.client(limited(past), limited(past), token=[T1, T2])
    with pytest.raises(RateLimitExceeded) as info:
        harness.send(client)
    assert harness.auths == bearer(T1, T2)
    assert info.value.reset == at(past)

def test_single_exhausted_token_not_sent_until_reset(harness):
    client = harness.client(limited(RESET), token=T1)
    with pytest.raises(RateLimitExceeded):
        harness.send(client)
    with pytest.raises(RateLimitExceeded) as info:
        harness.send(client)
    assert info.value.response is None
    assert info.value.reset == at(RESET)
    assert len(harness.requests) == 1
    harness.clock = at(RESET)
    harness.add(reply())
    harness.send(client)
    assert len(harness.requests) == 2


def test_zero_remaining_rotates_proactively(harness):
    last_call = {**RATE_HEADERS, "X-RateLimit-Remaining": "0"}
    client = harness.client(reply(headers=last_call), reply(), token=[T1, T2])
    harness.send(client)
    harness.send(client)
    assert harness.auths == bearer(T1, T2)


def test_rotation_does_not_consume_retries(harness):
    client = harness.client(
        reply(503), limited(), reply(503), reply(200, {"ok": 1}), token=[T1, T2], max_retries=2
    )
    assert harness.send(client).data == {"ok": 1}
    assert harness.auths == bearer(T1, T1, T2, T2)
    assert harness.delays == [1.0, 2.0]


def test_retries_still_bounded_after_rotation(harness):
    client = harness.client(
        reply(503), limited(), reply(503), reply(503), token=[T1, T2], max_retries=2
    )
    with pytest.raises(APIError) as info:
        harness.send(client)
    assert info.value.status_code == 503
    assert len(harness.requests) == 4
    assert harness.delays == [1.0, 2.0]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_not_rotated_past(harness, status):
    client = harness.client(
        limited(), reply(status, {"error": "Unauthorized"}), reply(), token=[T1, T2, T3]
    )
    with pytest.raises(AuthenticationError) as info:
        harness.send(client)
    assert harness.auths == bearer(T1, T2)
    assert str(info.value) == f"{status} GET /search/query: Unauthorized (token 2 of 3)"
    assert_no_token_text(client, info.value)


def test_single_token_auth_error_has_no_position(harness):
    with pytest.raises(AuthenticationError) as info:
        harness.send(harness.client(reply(401, {"error": "Unauthorized"}), token=[T1]))
    assert str(info.value) == "401 GET /search/query: Unauthorized"


# --- Lifecycle --------------------------------------------------------------


def test_sync_context_manager_only_creates_sync_client():
    harness = Harness("sync")
    with harness.client(reply()) as client:
        client.send(SEARCH)
        http = client._client
        assert http is not None
        assert client._async_client is None
    assert http.is_closed
    assert client._client is None


def test_async_context_manager_only_creates_async_client():
    harness = Harness("async")

    async def go() -> tuple[Client, httpx.AsyncClient]:
        async with harness.client(reply()) as client:
            await client.asend(SEARCH)
            http = client._async_client
            assert http is not None
            assert client._client is None
        return client, http

    client, http = asyncio.run(go())
    assert http.is_closed
    assert client._async_client is None


def test_clients_created_lazily_and_reused():
    harness = Harness("sync")
    client = harness.client(reply(), reply())
    assert client._client is None
    client.send(SEARCH)
    first = client._client
    client.send(SEARCH)
    assert client._client is first
    client.close()
    client.close()
    assert first.is_closed


def test_aclose_also_closes_sync_client():
    harness = Harness("sync")
    client = harness.client(reply())
    client.send(SEARCH)
    http = client._client
    asyncio.run(client.aclose())
    assert http.is_closed


# --- Default client ---------------------------------------------------------


def test_default_client_created_once(reset_default_client):
    client = get_default_client()
    assert isinstance(client, Client)
    assert client.config is module_config
    assert get_default_client() is client


def test_set_and_reset_default_client(reset_default_client):
    original = get_default_client()
    custom = Client(TOKEN)
    set_default_client(custom)
    assert get_default_client() is custom
    set_default_client(None)
    fresh = get_default_client()
    assert fresh is not custom and fresh is not original
