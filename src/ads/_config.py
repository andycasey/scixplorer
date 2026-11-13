"""Package configuration: a plain dataclass and the module-level instance users mutate."""

from __future__ import annotations

import os
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
    module-level :data:`config` affects every later request. The token is
    excluded from ``repr`` so it never ends up in logs or tracebacks.
    """

    token: str | None = field(default=None, repr=False)
    api_url: str = "https://api.adsabs.harvard.edu/v1"
    timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 1.0
    page_size: int = 200
    max_inline_bibcodes: int = 50
    library_membership: str = "docs"

    def resolve_token(self) -> str | None:
        """Return the API token to use, or ``None`` if none can be found.

        Checked in order: :attr:`token`, the environment variables in
        :data:`TOKEN_ENV_VARS`, then the first line of each file in
        :data:`TOKEN_FILES` under the home directory. Values are
        whitespace-stripped and empty values are skipped. The environment and
        home directory are read on every call.
        """
        if self.token and self.token.strip():
            return self.token.strip()
        for name in TOKEN_ENV_VARS:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        home = Path.home()
        for relative in TOKEN_FILES:
            value = _first_line(home / relative)
            if value:
                return value
        return None


def _first_line(path: Path) -> str:
    """Return the stripped first line of ``path``, or ``""`` if it cannot be read."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    return lines[0].strip() if lines else ""


#: The configuration used by every client that is not given its own.
config = Config()
