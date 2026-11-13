"""Package configuration: a plain dataclass and the module-level instance users mutate."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variables checked for an API token, in order.
TOKEN_ENV_VARS: tuple[str, ...] = ("ADS_API_TOKEN", "ADS_DEV_KEY")

#: Files (relative to the home directory) checked for an API token, in order.
TOKEN_FILES: tuple[str, ...] = (".ads/token", ".ads/dev_key")


@dataclass
class Config:
    """Settings for talking to the ADS API.

    The HTTP client reads these at send time, so changing an attribute of the
    module-level :data:`config` affects every later request.

    ``token`` is one API token (``str``) or an iterable of tokens, used in
    iteration order; an iterator such as a generator is consumed into a tuple
    when assigned. With several tokens, each request uses the first one that is
    not currently rate limited for the request's service. The token is
    excluded from ``repr`` so it never ends up in logs or tracebacks.
    """

    token: str | Iterable[str] | None = field(default=None, repr=False)
    api_url: str = "https://api.adsabs.harvard.edu/v1"
    timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 1.0
    page_size: int = 200
    max_inline_bibcodes: int = 50
    library_membership: str = "docs"

    def __setattr__(self, name: str, value: object) -> None:
        if name == "token" and isinstance(value, Iterator):
            value = tuple(value)
        super().__setattr__(name, value)

    def resolve_tokens(self) -> tuple[str, ...]:
        """Return the API tokens to use, in order; empty if none can be found.

        Sources are checked in order and the first that yields a token wins:
        :attr:`token` (see :func:`normalise_tokens`), the environment variables
        in :data:`TOKEN_ENV_VARS`, then the first line of each file in
        :data:`TOKEN_FILES` under the home directory. Each environment variable
        and file holds a single token. Values are whitespace-stripped and empty
        values are skipped. The environment and home directory are read on
        every call.
        """
        tokens = normalise_tokens(self.token)
        if tokens:
            return tokens
        for name in TOKEN_ENV_VARS:
            value = os.environ.get(name, "").strip()
            if value:
                return (value,)
        home = Path.home()
        for relative in TOKEN_FILES:
            value = _first_line(home / relative)
            if value:
                return (value,)
        return ()

    def resolve_token(self) -> str | None:
        """Return the first token from :meth:`resolve_tokens`, or ``None``."""
        tokens = self.resolve_tokens()
        return tokens[0] if tokens else None


def normalise_tokens(value: object) -> tuple[str, ...]:
    """Return the tokens in a token setting: ``None``, a ``str``, or an iterable of ``str``.

    Tokens are whitespace-stripped, blank ones dropped, and duplicates dropped
    keeping the first occurrence; order is the iteration order. Raises
    ``TypeError`` for ``bytes`` or any other non-``str`` value or element. Error
    messages name only types and positions, never the value.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        items: Iterable[object] = (value,)
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, memoryview)):
        items = value
    else:
        raise TypeError(
            "an API token setting must be a str or an iterable of str, "
            f"not {type(value).__name__}"
        )
    tokens: dict[str, None] = {}
    for position, item in enumerate(items, start=1):
        if not isinstance(item, str):
            raise TypeError(f"API token {position} must be a str, not {type(item).__name__}")
        if item.strip():
            tokens.setdefault(item.strip())
    return tuple(tokens)


def _first_line(path: Path) -> str:
    """Return the stripped first line of ``path``, or ``""`` if it cannot be read."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    return lines[0].strip() if lines else ""


#: The configuration used by every client that is not given its own.
config = Config()
