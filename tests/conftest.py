"""Command-line options and markers for the opt-in live tests (``tests/live``), a network guard, and an empty data cache.

Without ``--live`` every live test is skipped, and the whole run is guarded:
any attempt to resolve a host name or connect a network socket raises
:class:`NetworkBlocked`, so a test that reaches for the network fails.

Every run (with or without ``--live``) reads reference data from an empty
temporary cache directory (``SCIX_CACHE_DIR``), so data a developer has
refreshed locally cannot change the results; see :func:`isolated_cache`.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

LIVE_DIR = Path(__file__).parent / "live"

CACHE_ENV = "SCIX_CACHE_DIR"

_INET = (socket.AF_INET, socket.AF_INET6)
_originals: dict[str, Any] = {}
_cache: dict[str, Any] = {}


class NetworkBlocked(RuntimeError):
    """Raised by any network access while the live tests are not enabled."""


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("live", "tests against the real ADS / SciX API (see docs/content/development/live-tests.md)")
    group.addoption(
        "--live",
        action="store_true",
        help="run the live tests in tests/live (needs network access; most need an ADS API token)",
    )
    group.addoption(
        "--live-write",
        action="store_true",
        help="with --live, also run the tests that create, change and delete (their own) ADS libraries",
    )
    group.addoption(
        "--live-library",
        metavar="ID",
        default=None,
        help="id of a public ADS library for the library tests (or set SCIX_LIVE_LIBRARY)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live_write: creates, changes and deletes ADS libraries (enable with --live --live-write)"
    )
    if config.getoption("live_write") and not config.getoption("live"):
        raise pytest.UsageError("--live-write only works together with --live")
    if not config.getoption("live"):
        _block_network()
    _isolate_cache()


def pytest_unconfigure(config: pytest.Config) -> None:
    _restore_network()
    _restore_cache()


@pytest.fixture(scope="session", autouse=True)
def isolated_cache() -> Iterator[Path]:
    """The empty data cache of the whole run: ``SCIX_CACHE_DIR`` points at it unless a test sets its own.

    A test that sets ``SCIX_CACHE_DIR`` with ``monkeypatch.setenv`` gets it for
    that test only, and this directory back afterwards. The variable is first
    set in ``pytest_configure``, before test modules are imported, because some
    of them read reference data (affiliation ids) at import time.
    """
    directory = _cache["directory"]
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(CACHE_ENV, str(directory))
        yield directory


def _isolate_cache() -> None:
    """Point ``SCIX_CACHE_DIR`` at a new empty directory for this run."""
    if _cache:
        return
    _cache["previous"] = os.environ.get(CACHE_ENV)
    _cache["directory"] = Path(tempfile.mkdtemp(prefix="scix-test-cache-"))
    os.environ[CACHE_ENV] = str(_cache["directory"])


def _restore_cache() -> None:
    if not _cache:
        return
    previous = _cache.pop("previous")
    if previous is None:
        os.environ.pop(CACHE_ENV, None)
    else:
        os.environ[CACHE_ENV] = previous
    shutil.rmtree(_cache.pop("directory"), ignore_errors=True)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live = config.getoption("live")
    write = config.getoption("live_write")
    skip_live = pytest.mark.skip(reason="live test: run with --live (needs network access and an ADS API token)")
    skip_write = pytest.mark.skip(reason="changes ADS libraries: run with --live --live-write")
    for item in items:
        if not live and (item.get_closest_marker("live") or LIVE_DIR in Path(item.path).parents):
            item.add_marker(skip_live)
        elif item.get_closest_marker("live_write") and not write:
            item.add_marker(skip_write)


def _refuse(*args: Any, **kwargs: Any) -> Any:
    raise NetworkBlocked("network access is blocked unless the tests run with --live")


def _guarded(name: str) -> Any:
    original = getattr(socket.socket, name)

    def connect(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
        if self.family in _INET:
            _refuse()
        return original(self, *args, **kwargs)

    return connect


def _block_network() -> None:
    if _originals:
        return
    for name in ("connect", "connect_ex"):
        _originals[name] = getattr(socket.socket, name)
        setattr(socket.socket, name, _guarded(name))
    _originals["getaddrinfo"] = socket.getaddrinfo
    socket.getaddrinfo = _refuse


def _restore_network() -> None:
    for name in ("connect", "connect_ex"):
        if name in _originals:
            setattr(socket.socket, name, _originals.pop(name))
    if "getaddrinfo" in _originals:
        socket.getaddrinfo = _originals.pop("getaddrinfo")
