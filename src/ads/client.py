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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import TracebackType
from typing import Any

import httpx

from ads import __version__
from ads._config import TOKEN_ENV_VARS, TOKEN_FILES, Config
from ads._config import config as _module_config
from ads.exceptions import APIError, AuthenticationError, RateLimitExceeded

USER_AGENT = f"ads-python/{__version__}"

#: Statuses that indicate a transient upstream problem worth retrying.
RETRY_STATUSES = frozenset({502, 503, 504})

#: Longest error-body excerpt included in an exception message.
_MAX_DETAIL = 500

_MISSING_TOKEN_MESSAGE = (
    "No ADS API token found. Get one at https://ui.adsabs.harvard.edu/user/settings/token "
    "and then either set ads.config.token = '...', pass token= to Client(), set the "
    f"{' or '.join(TOKEN_ENV_VARS)} environment variable, or save it as the first line "
    f"of {' or '.join('~/' + f for f in TOKEN_FILES)}."
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
    """

    method: str
    path: str
    params: Mapping[str, Any] | None = None
    json: Any = None
    content: str | bytes | None = None
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.json is not None and self.content is not None:
            raise ValueError("a Request may have a json body or raw content, not both")
        _encode_params(self.params)


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


@dataclass(frozen=True)
class _Retry:
    """Outcome of an attempt that should be retried after ``delay`` seconds."""

    delay: float


class Client:
    """Executes :class:`Request` objects against the ADS API, sync or async.

    :param token: API token. If omitted, ``config.resolve_token()`` is used. The
        token is looked up on every request and never appears in ``repr`` or
        exception messages.
    :param config: settings to use; defaults to the module-level ``ads.config``
        (``ads._config.config``). Its ``api_url``, ``timeout``, ``max_retries``
        and ``retry_backoff`` are read on every request.
    :param transport: optional ``httpx`` transport for the sync client.
    :param async_transport: optional ``httpx`` transport for the async client.
    :param sleep: called with the delay between sync retries.
    :param async_sleep: awaited with the delay between async retries.

    The underlying ``httpx.Client`` and ``httpx.AsyncClient`` are created on
    first use. ``close()`` closes the sync one; ``aclose()`` closes both. A
    closed ``Client`` creates fresh ones if used again. Lazy creation is not
    thread-safe; share a ``Client`` across threads only after first use.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        config: Config | None = None,
        transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], object] = time.sleep,
        async_sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.config = config if config is not None else _module_config
        self.rate_limits: dict[str, RateLimit] = {}
        self._token = token
        self._transport = transport
        self._async_transport = async_transport
        self._sleep = sleep
        self._async_sleep = async_sleep
        self._client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        return f"<Client api_url={self.config.api_url!r}>"

    def send(self, request: Request) -> Response:
        """Execute ``request`` and return its response, retrying transient failures.

        Raises :class:`~ads.exceptions.AuthenticationError` if there is no token
        or ADS rejects it, :class:`~ads.exceptions.RateLimitExceeded` on HTTP 429,
        and :class:`~ads.exceptions.APIError` for any other failure.
        """
        prepared = self._prepare(request)
        attempt = 0
        while True:
            result: httpx.Response | httpx.TransportError
            try:
                result = self._http().send(prepared)
            except httpx.TransportError as exc:
                result = exc
            outcome = self._settle(request, result, attempt)
            if not isinstance(outcome, _Retry):
                return outcome
            self._sleep(outcome.delay)
            attempt += 1

    async def asend(self, request: Request) -> Response:
        """Asynchronous :meth:`send`; identical behaviour and exceptions."""
        prepared = self._prepare(request)
        attempt = 0
        while True:
            result: httpx.Response | httpx.TransportError
            try:
                result = await self._async_http().send(prepared)
            except httpx.TransportError as exc:
                result = exc
            outcome = self._settle(request, result, attempt)
            if not isinstance(outcome, _Retry):
                return outcome
            await self._async_sleep(outcome.delay)
            attempt += 1

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

    def _prepare(self, request: Request) -> httpx.Request:
        """Build the ``httpx.Request`` for ``request`` (shared by sync and async)."""
        token = (self._token or "").strip() or self.config.resolve_token()
        if not token:
            raise AuthenticationError(_MISSING_TOKEN_MESSAGE)

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
        request: Request,
        result: httpx.Response | httpx.TransportError,
        attempt: int,
    ) -> Response | _Retry:
        """Interpret one attempt (shared by sync and async).

        Returns the :class:`Response` on success, a :class:`_Retry` if the
        attempt failed transiently and retries remain, and otherwise raises.
        ``attempt`` counts from 0.
        """
        retries_left = attempt < self.config.max_retries
        if isinstance(result, httpx.TransportError):
            if retries_left:
                return _Retry(self._backoff(attempt))
            raise APIError(
                f"{_describe(request)}: {type(result).__name__}: {result}"
                f" (gave up after {attempt + 1} attempts)",
                status_code=None,
            ) from result

        response = _to_response(result)
        self._record_rate_limit(request.path, response.headers)
        if 200 <= response.status_code < 300:
            return response
        if response.status_code in RETRY_STATUSES and retries_left:
            return _Retry(self._backoff(attempt))
        raise _error_for(request, response)

    def _backoff(self, attempt: int) -> float:
        return self.config.retry_backoff * 2**attempt

    def _record_rate_limit(self, path: str, headers: Mapping[str, str]) -> None:
        service = service_for(path)
        limit = _parse_int(headers.get("x-ratelimit-limit"))
        remaining = _parse_int(headers.get("x-ratelimit-remaining"))
        reset = _parse_timestamp(headers.get("x-ratelimit-reset"))
        if service and limit is not None and remaining is not None and reset is not None:
            self.rate_limits[service] = RateLimit(limit, remaining, reset)


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


def _error_for(request: Request, response: Response) -> APIError:
    """Build the exception for a non-2xx response."""
    code = response.status_code
    message = f"{code} {_describe(request)}: {_error_detail(response)}"
    if code == 429:
        reset = _parse_timestamp(response.headers.get("x-ratelimit-reset"))
        if reset is not None:
            message += f" (rate limit resets at {reset.isoformat()})"
        return RateLimitExceeded(
            message,
            limit=_parse_int(response.headers.get("x-ratelimit-limit")),
            remaining=_parse_int(response.headers.get("x-ratelimit-remaining")),
            reset=reset,
            status_code=code,
            response=response,
        )
    error_class = AuthenticationError if code in (401, 403) else APIError
    return error_class(message, status_code=code, response=response)


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
