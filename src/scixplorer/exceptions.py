"""Exceptions and warnings raised by the ``scixplorer`` package."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class SciXError(Exception):
    """Base class for every error raised by this package."""


class QueryError(SciXError):
    """A query expression is malformed or cannot be executed."""


class UnsupportedQueryError(QueryError):
    """The expression is valid, but no execution plan exists for it."""


class NotLocallyEvaluable(QueryError):
    """The expression can only be evaluated by the ADS search engine, not in Python."""


class DoesNotExist(SciXError, LookupError):
    """No object matched a query that required exactly one result."""


class APIError(SciXError):
    """The ADS API returned an error response."""

    def __init__(self, message: str, *, status_code: int | None = None, response: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response = response


class AuthenticationError(APIError):
    """No API token was found, or ADS rejected the one supplied."""


class RateLimitExceeded(APIError):
    """The rate limit for an ADS service has been reached."""

    def __init__(
        self,
        message: str,
        *,
        limit: int | None = None,
        remaining: int | None = None,
        reset: datetime | None = None,
        **kwargs: Any,
    ):
        super().__init__(message, **kwargs)
        self.limit = limit
        self.remaining = remaining
        self.reset = reset


class DataNotAvailable(SciXError):
    """Reference data that is not packaged has not been downloaded yet; the message says how."""


class LazyLoadWarning(UserWarning):
    """A field that was not requested by the query is being fetched on demand."""


class DataWarning(UserWarning):
    """A value returned by ADS did not have the type its field declares."""
