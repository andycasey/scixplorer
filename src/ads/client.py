"""HTTP layer: the ``Request``/``Response`` values exchanged with backends, and ``Client``.

Backends never perform I/O; they yield :class:`Request` objects and receive
:class:`Response` objects. :class:`Client` executes them over ``httpx``, either
synchronously (:meth:`Client.send`) or asynchronously (:meth:`Client.asend`).
Both methods share every step except the network call and the sleep between
retries: :meth:`Client._prepare` builds the ``httpx.Request`` and
:meth:`Client._settle` decides what a single attempt's outcome means.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import Any

import httpx

from ads import __version__
from ads._config import TOKEN_ENV_VARS, TOKEN_FILES, Config, normalise_tokens
from ads._config import config as _module_config
from ads.exceptions import APIError, AuthenticationError, RateLimitExceeded

USER_AGENT = f"ads-python/{__version__}"

#: Statuses that indicate a transient upstream problem worth retrying.
RETRY_STATUSES = frozenset({502, 503, 504})

#: Methods that are idempotent by definition (RFC 9110, section 9.2.2).
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})

#: Transport errors raised before the request reached the server, so a retry
#: cannot repeat its effect. Safe to retry even for non-idempotent requests.
_UNSENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)

#: Longest error-body excerpt included in an exception message.
_MAX_DETAIL = 500

#: How long a token is left unused for a service after an HTTP 429 that did
#: not report when the limit resets.
_ASSUMED_RATE_LIMIT_WAIT = timedelta(seconds=60)

_MISSING_TOKEN_MESSAGE = (
    "No API token found. Get one at https://scixplorer.org/user/settings/token "
    "and then either set ads.config.token = '...', pass token= to Client(), set one of the "
    f"environment variables {', '.join(TOKEN_ENV_VARS)} (checked in that order), or save it "
    f"as the first line of one of {', '.join('~/' + f for f in TOKEN_FILES)} (checked in that order)."
)


@dataclass(frozen=True)
class Request:
    """One HTTP request to the ADS API, described without performing it.

    ``path`` is relative to ``Config.api_url`` (a leading slash is optional).
    ``params`` values may be ``str``, ``int``, ``float``, ``bool`` or ``None``;
    ``None`` values are dropped and booleans are sent as ``true``/``false``.
    At most one of ``json`` (a JSON body) and ``content`` (a raw body, sent with
    the caller's ``Content-Type`` header) may be given; ``json=None`` means no
    JSON body. Construction raises ``TypeError`` for an unsupported parameter
    value and ``ValueError`` if both bodies are given.

    ``idempotent`` says whether sending the request twice has the same effect as
    sending it once, which decides how it may be retried (see :meth:`Client.send`).
    ``None`` infers it from the method (see :attr:`is_idempotent`); set it to
    ``True`` for a POST that only reads, such as ``/search/bigquery``.
    """

    method: str
    path: str
    params: Mapping[str, Any] | None = None
    json: Any = None
    content: str | bytes | None = None
    headers: Mapping[str, str] | None = None
    idempotent: bool | None = None

    def __post_init__(self) -> None:
        if self.json is not None and self.content is not None:
            raise ValueError("a Request may have a json body or raw content, not both")
        _encode_params(self.params)

    @property
    def is_idempotent(self) -> bool:
        """``idempotent`` if set, else whether the method is in :data:`IDEMPOTENT_METHODS`.

        So GET, HEAD, PUT and DELETE are idempotent by default; POST and PATCH are not.
        """
        if self.idempotent is not None:
            return self.idempotent
        return self.method.upper() in IDEMPOTENT_METHODS


@dataclass(frozen=True)
class Response:
    """The parts of an HTTP response that backends need.

    ``headers`` is a plain ``dict`` with lower-cased keys (repeated headers are
    joined with ``", "``), so look headers up in lower case. ``data`` is the
    parsed JSON body if the body is valid JSON (whatever the declared content
    type), otherwise the body text, or ``None`` if the body is empty.
    """

    status_code: int
    headers: Mapping[str, str]
    data: Any


@dataclass(frozen=True)
class RateLimit:
    """The rate-limit state ADS last reported for one service."""

    limit: int
    remaining: int
    reset: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _Retry:
    """Outcome of an attempt that should be sent again.

    ``delay`` is the back-off in seconds before a retry that counts towards
    ``max_retries``. ``None`` means send again at once with another token
    (after HTTP 429), which does not count as a retry.
    """

    delay: float | None


@dataclass
class _Exchange:
    """The state of one ``send``/``asend`` call, shared by both.

    ``tokens`` are resolved once per call and ``token`` is the one the current
    attempt uses. ``retries`` counts back-off retries (not token changes).
    ``rejected`` holds the tokens that got HTTP 429 during this call; they are
    not tried again in it, even if their reset time has already passed.
    """

    request: Request
    tokens: tuple[str, ...] = field(repr=False)
    token: str = field(default="", repr=False)
    retries: int = 0
    rejected: set[str] = field(default_factory=set, repr=False)

    @property
    def service(self) -> str:
        return service_for(self.request.path)

    @property
    def token_label(self) -> str | None:
        """The current token's position, e.g. ``token 2 of 3``; ``None`` if there is one token."""
        if len(self.tokens) == 1:
            return None
        return f"token {self.tokens.index(self.token) + 1} of {len(self.tokens)}"


@dataclass(frozen=True)
class _Exhaustion:
    """A token that is not used for a service before ``until``.

    ``reset`` is the reset time ADS reported, or ``None`` if it reported none,
    in which case ``until`` is an assumed wait.
    """

    until: datetime
    reset: datetime | None


class Client:
    """Executes :class:`Request` objects against the ADS API, sync or async.

    :param token: an API token (``str``) or an iterable of tokens, used in
        iteration order. If omitted or blank, ``config.resolve_tokens()`` is
        used. Tokens are resolved on every request and never appear in
        ``repr`` or exception messages. With several tokens, each request uses
        the first one that is not currently rate limited for the request's
        service (after HTTP 429, or a response reporting no remaining
        requests, until the reported reset time).
    :param config: settings to use; defaults to the module-level ``ads.config``
        (``ads._config.config``). Its ``api_url``, ``timeout``, ``max_retries``
        and ``retry_backoff`` are read on every request.
    :param transport: optional ``httpx`` transport for the sync client.
    :param async_transport: optional ``httpx`` transport for the async client.
    :param sleep: called with the delay between sync retries.
    :param async_sleep: awaited with the delay between async retries.
    :param now: returns the current time as an aware UTC datetime; compared
        with rate-limit reset times.

    The underlying ``httpx.Client`` and ``httpx.AsyncClient`` are created on
    first use. ``close()`` closes the sync one; ``aclose()`` closes both. A
    closed ``Client`` creates fresh ones if used again. A ``Client`` is not
    thread-safe: lazy creation and the rate-limit bookkeeping are unsynchronised.
    """

    def __init__(
        self,
        token: str | Iterable[str] | None = None,
        *,
        config: Config | None = None,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], object] = time.sleep,
        async_sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.config = config if config is not None else _module_config
        self.rate_limits: dict[str, RateLimit] = {}
        self._token = tuple(token) if isinstance(token, Iterator) else token
        self._transport = transport
        self._async_transport = async_transport
        self._sleep = sleep
        self._async_sleep = async_sleep
        self._now = now
        self._client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None
        self._exhausted: dict[tuple[str, str], _Exhaustion] = {}

    def __repr__(self) -> str:
        return f"<Client api_url={self.config.api_url!r}>"

    def send(self, request: Request) -> Response:
        """Execute ``request`` and return its response, retrying transient failures.

        Idempotent requests are retried after connection errors, timeouts and
        HTTP 502/503/504. Non-idempotent requests (see :attr:`Request.is_idempotent`)
        are retried only if the connection could not be made, since otherwise
        the server may already have applied them. After HTTP 429 the request is
        sent again at once if another token is available for its service.

        Raises :class:`~ads.exceptions.AuthenticationError` if there is no token
        or ADS rejects it, :class:`~ads.exceptions.RateLimitExceeded` when every
        token is rate limited for the service (without sending the request if
        that is already known), and :class:`~ads.exceptions.APIError` for any
        other failure.
        """
        exchange = _Exchange(request, self._resolve_tokens())
        while True:
            prepared = self._prepare(exchange)
            result: httpx.Response | httpx.TransportError
            try:
                result = self._http().send(prepared)
            except httpx.TransportError as exc:
                result = exc
            outcome = self._settle(exchange, result)
            if not isinstance(outcome, _Retry):
                return outcome
            if outcome.delay is not None:
                self._sleep(outcome.delay)

    async def asend(self, request: Request) -> Response:
        """Asynchronous :meth:`send`; identical behaviour and exceptions."""
        exchange = _Exchange(request, self._resolve_tokens())
        while True:
            prepared = self._prepare(exchange)
            result: httpx.Response | httpx.TransportError
            try:
                result = await self._async_http().send(prepared)
            except httpx.TransportError as exc:
                result = exc
            outcome = self._settle(exchange, result)
            if not isinstance(outcome, _Retry):
                return outcome
            if outcome.delay is not None:
                await self._async_sleep(outcome.delay)

    def close(self) -> None:
        """Close the underlying sync ``httpx.Client``, if one was created."""
        if self._client is not None:
            self._client.close()
            self._client = None

    async def aclose(self) -> None:
        """Close the underlying ``httpx`` clients (async and sync), if created."""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        self.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(transport=self._transport)
        return self._client

    def _async_http(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(transport=self._async_transport)
        return self._async_client

    def _resolve_tokens(self) -> tuple[str, ...]:
        tokens = normalise_tokens(self._token) or self.config.resolve_tokens()
        if not tokens:
            raise AuthenticationError(_MISSING_TOKEN_MESSAGE)
        return tokens

    def _prepare(self, exchange: _Exchange) -> httpx.Request:
        """Choose this attempt's token and build its ``httpx.Request`` (shared by sync and async).

        Raises :class:`RateLimitExceeded`, without sending anything, if every
        token is already known to be rate limited for the request's service.
        """
        token = self._usable_token(exchange)
        if token is None:
            raise self._rate_limit_error(exchange, None)
        exchange.token = token
        request = exchange.request

        headers = httpx.Headers({"User-Agent": USER_AGENT})
        body: bytes | None = None
        if request.json is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(request.json).encode("utf-8")
        elif request.content is not None:
            content = request.content
            body = content.encode("utf-8") if isinstance(content, str) else content
        headers.update(request.headers or {})
        headers["Authorization"] = f"Bearer {token}"

        return httpx.Request(
            request.method,
            _join_url(self.config.api_url, request.path),
            params=_encode_params(request.params),
            headers=headers,
            content=body,
            # Requests built directly (not via httpx.Client.build_request) carry no
            # timeout unless one is set here; this is what build_request would do.
            extensions={"timeout": httpx.Timeout(self.config.timeout).as_dict()},
        )

    def _settle(
        self,
        exchange: _Exchange,
        result: httpx.Response | httpx.TransportError,
    ) -> Response | _Retry:
        """Interpret one attempt (shared by sync and async).

        Returns the :class:`Response` on success, a :class:`_Retry` if the
        request should be sent again, and otherwise raises.

        Idempotent requests are retried after any transport error and after
        502/503/504. Non-idempotent requests are retried only after errors in
        :data:`_UNSENT_ERRORS`, because otherwise the server may already have
        applied them. Any request that gets HTTP 429 is sent again at once with
        the next usable token, if there is one: ADS did not apply it.
        """
        request = exchange.request
        retries_left = exchange.retries < self.config.max_retries
        if isinstance(result, httpx.TransportError):
            retryable = request.is_idempotent or isinstance(result, _UNSENT_ERRORS)
            if retryable and retries_left:
                return self._backoff(exchange)
            outcome = (
                f"gave up after {exchange.retries + 1} attempts"
                if retryable
                else "not retried: the request is not idempotent and may or may not "
                "have been applied"
            )
            raise APIError(
                f"{_describe(request)}: {type(result).__name__}: {result} ({outcome})",
                status_code=None,
            ) from result

        response = _to_response(result)
        self._observe(exchange, response)
        code = response.status_code
        if 200 <= code < 300:
            return response
        if code == 429:
            exchange.rejected.add(exchange.token)
            if self._usable_token(exchange) is not None:
                return _Retry(None)
            raise self._rate_limit_error(exchange, response)
        if code in RETRY_STATUSES and retries_left and request.is_idempotent:
            return self._backoff(exchange)
        raise _error_for(request, response, exchange.token_label)

    def _backoff(self, exchange: _Exchange) -> _Retry:
        delay = self.config.retry_backoff * 2**exchange.retries
        exchange.retries += 1
        return _Retry(delay)

    def _observe(self, exchange: _Exchange, response: Response) -> None:
        """Record the rate limit a response reports, and whether its token is now exhausted.

        A token is exhausted for the service after HTTP 429 (until the reported
        reset, or for :data:`_ASSUMED_RATE_LIMIT_WAIT` if none was reported) and
        after any response reporting zero remaining requests and a reset time.
        """
        service = exchange.service
        limit = _parse_int(response.headers.get("x-ratelimit-limit"))
        remaining = _parse_int(response.headers.get("x-ratelimit-remaining"))
        reset = _parse_timestamp(response.headers.get("x-ratelimit-reset"))
        if service and limit is not None and remaining is not None and reset is not None:
            self.rate_limits[service] = RateLimit(limit, remaining, reset)

        key = (exchange.token, service)
        if response.status_code == 429:
            until = reset if reset is not None else self._now() + _ASSUMED_RATE_LIMIT_WAIT
            self._exhausted[key] = _Exhaustion(until, reset)
        elif remaining == 0 and reset is not None:
            self._exhausted[key] = _Exhaustion(reset, reset)

    def _usable_token(self, exchange: _Exchange) -> str | None:
        """The first token, in order, not rejected in this call nor exhausted for its service."""
        now = self._now()
        for token in exchange.tokens:
            if token in exchange.rejected:
                continue
            key = (token, exchange.service)
            exhaustion = self._exhausted.get(key)
            if exhaustion is None:
                return token
            if exhaustion.until <= now:
                del self._exhausted[key]
                return token
        return None

    def _rate_limit_error(
        self, exchange: _Exchange, response: Response | None
    ) -> RateLimitExceeded:
        """The error for when every token is rate limited for the request's service.

        ``reset`` is the earliest reset time ADS reported for any of the tokens,
        or ``None`` if it reported none. ``limit`` and ``remaining`` come from
        ``response``; with ``response=None`` the request was not sent, ``limit``
        is the last one recorded for the service and ``remaining`` is 0.
        """
        request = exchange.request
        service = exchange.service
        exhaustions = [self._exhausted.get((token, service)) for token in exchange.tokens]
        reported = [e.reset for e in exhaustions if e is not None and e.reset is not None]
        reset = min(reported, default=None)

        if response is None:
            message = f"429 {_describe(request)}: rate limit for {service} exhausted; not sent"
            known = self.rate_limits.get(service)
            limit = known.limit if known is not None else None
            remaining: int | None = 0
        else:
            message = f"429 {_describe(request)}: {_error_detail(response)}"
            limit = _parse_int(response.headers.get("x-ratelimit-limit"))
            remaining = _parse_int(response.headers.get("x-ratelimit-remaining"))
        if reset is not None:
            message += f" (rate limit resets at {reset.isoformat()})"
        return RateLimitExceeded(
            message,
            limit=limit,
            remaining=remaining,
            reset=reset,
            status_code=429,
            response=response,
        )


def service_for(path: str) -> str:
    """Return the rate-limit service of an API path: its first two segments.

    ``/search/query`` -> ``search/query``; ``/biblib/libraries/abc`` -> ``biblib/libraries``.
    """
    return "/".join([segment for segment in path.split("/") if segment][:2])


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _describe(request: Request) -> str:
    return f"{request.method.upper()} /{request.path.lstrip('/')}"


def _encode_params(params: Mapping[str, Any] | None) -> dict[str, str]:
    """Encode query parameters as strings, dropping ``None`` values.

    Raises ``TypeError`` for values other than ``str``, ``int``, ``float``,
    ``bool`` or ``None`` (in particular lists: join them before building the
    Request).
    """
    encoded: dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded[key] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            encoded[key] = str(value)
        else:
            raise TypeError(
                f"query parameter {key!r} must be str, int, float, bool or None, "
                f"not {type(value).__name__}"
            )
    return encoded


def _to_response(http_response: httpx.Response) -> Response:
    headers = {key.lower(): value for key, value in http_response.headers.items()}
    return Response(http_response.status_code, headers, _parse_body(http_response))


def _parse_body(http_response: httpx.Response) -> Any:
    """Parsed JSON if the body is JSON, else its text; ``None`` if blank."""
    if not http_response.content.strip():
        return None
    try:
        return json.loads(http_response.content)
    except ValueError:
        return http_response.text


def _error_for(request: Request, response: Response, token_label: str | None) -> APIError:
    """Build the exception for a non-2xx response other than 429.

    ``token_label`` (e.g. ``token 2 of 3``) is added to authentication errors.
    """
    code = response.status_code
    message = f"{code} {_describe(request)}: {_error_detail(response)}"
    if code in (401, 403):
        if token_label is not None:
            message += f" ({token_label})"
        return AuthenticationError(message, status_code=code, response=response)
    return APIError(message, status_code=code, response=response)


def _error_detail(response: Response) -> str:
    """The most useful human-readable explanation in an error response.

    For a JSON object, the first non-empty string among ``error``, ``message``
    and ``msg`` (where ``error`` may be a Solr-style dict holding ``msg``);
    otherwise the body text, whitespace-collapsed and truncated; otherwise the
    standard reason phrase.
    """
    data = response.data
    if isinstance(data, dict):
        for key in ("error", "message", "msg"):
            value = data.get(key)
            if isinstance(value, dict):
                value = value.get("msg")
            if isinstance(value, str) and value.strip():
                return _trim(value)
    if data is None:
        text = ""
    elif isinstance(data, str):
        text = data
    else:
        text = json.dumps(data)
    return _trim(text) or httpx.codes.get_reason_phrase(response.status_code) or "no details"


def _trim(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 3] + "..."


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse a unix timestamp header value as an aware UTC datetime."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


_default_client: Client | None = None


def get_default_client() -> Client:
    """Return the shared default :class:`Client`, creating it on first use.

    The default client uses the module-level ``ads.config`` (``ads._config.config``).
    """
    global _default_client
    if _default_client is None:
        _default_client = Client()
    return _default_client


def set_default_client(client: Client | None) -> None:
    """Replace the shared default client; ``None`` makes the next
    :func:`get_default_client` call create a fresh one. The previous client is
    not closed."""
    global _default_client
    _default_client = client
