"""Reference data held locally: journals, affiliations and two controlled vocabularies.

Each dataset is read from the user's cache if :func:`refresh` has downloaded it,
otherwise from the snapshot packaged with this library (journals and
affiliations only). The Unified Astronomy Thesaurus and the planetary
nomenclature gazetteer are not packaged: run ``refresh("uat")`` or
``refresh("planetary_features")`` once to use them.

Nothing is downloaded implicitly. :func:`refresh` uses a plain HTTP client that
never sends the SciX API token, because the sources are third-party hosts.
Downloads are checked by the same parser the model uses before they replace
the data in use, and are written atomically.

The packaged data files are not covered by this package's MIT licence: the
affiliation files are GPL-3.0. :func:`notices` returns the details.

The cache directory is ``$SCIX_CACHE_DIR`` if set, else
``$XDG_CACHE_HOME/scixplorer``, else ``~/Library/Caches/scixplorer`` on macOS
and ``~/.cache/scixplorer`` elsewhere.
"""

from __future__ import annotations

import html
import importlib
import json
import os
import re
import sys
import tempfile
import warnings
from collections.abc import Callable, Mapping, Sized
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypeVar

import httpx

from .. import __version__
from ..exceptions import DataNotAvailable, DataWarning

__all__ = [
    "DATASETS",
    "Dataset",
    "DatasetStatus",
    "Reader",
    "RefreshResult",
    "RemoteFile",
    "cache_dir",
    "clear",
    "load",
    "notices",
    "read_text",
    "refresh",
    "register",
    "status",
]

#: Reads one file of a dataset by name and returns its text.
Reader = Callable[[str], str]

S = TypeVar("S", bound=Sized)

USER_AGENT = f"scixplorer/{__version__}"
_PACKAGE = __name__.rpartition(".")[0]


@dataclass(frozen=True)
class RemoteFile:
    """One file of a dataset: where it is published and its name in the cache.

    The cached file always has the packaged format. ``convert``, if given,
    turns the published text into that format.
    """

    url: str
    filename: str
    convert: Callable[[str], str] | None = None


@dataclass(frozen=True)
class Dataset:
    """A collection of reference data and where it comes from.

    ``files`` is empty if there is no public source to refresh it from.
    ``packaged`` says whether a snapshot ships with this library. ``licence``
    is the data's licence, as :func:`notices` states it.
    """

    name: str
    files: tuple[RemoteFile, ...]
    packaged: bool
    licence: str
    description: str


@dataclass(frozen=True)
class RefreshResult:
    """The outcome of refreshing one dataset. ``files`` are the cache files written."""

    name: str
    ok: bool
    files: tuple[Path, ...]
    message: str


@dataclass(frozen=True)
class DatasetStatus:
    """Which copy of a dataset is in use.

    ``source`` is ``"cache"`` (downloaded by :func:`refresh`), ``"packaged"``
    or ``"missing"``. ``modified`` (UTC) and ``size`` (bytes) cover all the
    files in ``paths``; they are ``None`` when nothing can be inspected. The
    cached files are not parsed here, so an unreadable cache still shows as
    ``"cache"`` (the models then fall back to the packaged snapshot, with a
    :class:`~scixplorer.exceptions.DataWarning`).
    """

    name: str
    source: Literal["cache", "packaged", "missing"]
    paths: tuple[Path, ...]
    modified: datetime | None
    size: int | None


_ROW = re.compile(r"""<a href="#" onClick="setfields\('[^']*'\);">([^<]*)</a>(.*)""")


def _journal_list_to_json(page: str) -> str:
    """Convert the ADS journal abbreviation page to ``journals.json`` (``{abbreviation: title}``).

    Each entry is a line ``<a ... setfields('ApJ..');">  ApJ  </a>  The Astrophysical Journal``;
    the abbreviation is the link text, as in the packaged snapshot. The first
    title wins if an abbreviation is listed twice.
    """
    titles: dict[str, str] = {}
    for line in page.splitlines():
        match = _ROW.search(line)
        if match is not None:
            abbreviation = html.unescape(match.group(1)).strip()
            title = html.unescape(match.group(2)).strip()
            if abbreviation and title:
                titles.setdefault(abbreviation, title)
    if not titles:
        raise ValueError("no journal entries found in the page")
    return json.dumps(titles, ensure_ascii=False, indent=0)


_AFFILIATIONS = "https://raw.githubusercontent.com/adsabs/CanonicalAffiliations/master/"

DATASETS: Mapping[str, Dataset] = MappingProxyType(
    {
        "journals": Dataset(
            name="journals",
            files=(
                RemoteFile(
                    "https://adsabs.harvard.edu/abs_doc/journals.html",
                    "journals.json",
                    convert=_journal_list_to_json,
                ),
            ),
            packaged=True,
            licence="No licence stated (ADS bibliographic code list)",
            description="Journal abbreviations (bibstems) and titles, from the ADS journal list.",
        ),
        "affiliations": Dataset(
            name="affiliations",
            files=(
                RemoteFile(_AFFILIATIONS + "parent_child.tsv", "affiliations.tsv"),
                RemoteFile(_AFFILIATIONS + "country_parent_child.tsv", "affiliations_country.tsv"),
            ),
            packaged=True,
            licence="GPL-3.0 (adsabs/CanonicalAffiliations)",
            description="Canonical institutions, their hierarchy and countries, from adsabs/CanonicalAffiliations.",
        ),
        "uat": Dataset(
            name="uat",
            files=(
                RemoteFile(
                    "https://raw.githubusercontent.com/astrothesaurus/UAT/master/UAT_list.json",
                    "uat.json",
                ),
            ),
            packaged=False,
            licence="CC BY-SA 3.0 (American Astronomical Society)",
            description="The Unified Astronomy Thesaurus, from astrothesaurus/UAT (about 1.5 MB).",
        ),
        "planetary_features": Dataset(
            name="planetary_features",
            files=(
                RemoteFile(
                    "https://raw.githubusercontent.com/adsabs/ADSPlanetaryNamesPipeline/main/docs/"
                    "Gazetteer_of_Planetary_Nomenclature_Exported_Nov_7_2024.csv",
                    "planetary_features.csv",
                ),
            ),
            packaged=False,
            licence="Public domain (USGS Gazetteer of Planetary Nomenclature), via an MIT-licensed adsabs repository",
            description="Named planetary surface features (IAU/USGS gazetteer export used by ADS, about 0.9 MB).",
        ),
    }
)

#: The module that registers each dataset's parser, relative to this package's parent.
_OWNERS = {
    "journals": "models.journal",
    "affiliations": "models.affiliation",
    "uat": "models.uat",
    "planetary_features": "models.planetary",
}


@dataclass(frozen=True)
class _Registration:
    parse: Callable[[Reader], Sized]
    reset: Callable[[], None]


_registry: dict[str, _Registration] = {}


def register(name: str, parse: Callable[[Reader], Sized], reset: Callable[[], None]) -> None:
    """Register how a model reads dataset ``name``; models call this when imported.

    :param parse: builds the model's records from a :data:`Reader`; it raises on
        malformed data. :func:`load` and :func:`refresh` both use it.
    :param reset: forgets everything the model has cached from the data, so the
        next query reads it again. Called after :func:`refresh` or :func:`clear`.
    """
    _dataset(name)
    _registry[name] = _Registration(parse, reset)


def _registration(name: str) -> _Registration:
    if name not in _registry:
        importlib.import_module(f"{_PACKAGE}.{_OWNERS[name]}")
    return _registry[name]


def _dataset(name: str) -> Dataset:
    try:
        return DATASETS[name]
    except KeyError:
        raise ValueError(f"Unknown dataset {name!r}; datasets are: {', '.join(DATASETS)}") from None


def _call(function: str, name: str) -> str:
    return f'{__name__}.{function}("{name}")'


def cache_dir() -> Path:
    """The directory holding downloaded data. It is created only when something is written."""
    explicit = os.environ.get("SCIX_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "scixplorer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "scixplorer"
    return Path.home() / ".cache" / "scixplorer"


def _cached_paths(dataset: Dataset, directory: Path) -> tuple[Path, ...] | None:
    """The dataset's files in ``directory``, or ``None`` unless every one exists."""
    paths = tuple(directory / remote.filename for remote in dataset.files)
    if paths and all(path.is_file() for path in paths):
        return paths
    return None


def _read_packaged(filename: str) -> str:
    return (resources.files(__name__) / filename).read_text(encoding="utf-8")


def notices() -> str:
    """The licence notice for the data files packaged with this library (``NOTICE.md``).

    It says where each file comes from and under which licence; the packaged
    data is not covered by the package's MIT licence. The GPL-3.0 text it
    refers to is the package resource ``LICENSE-GPL-3.0.txt`` beside it.
    """
    return _read_packaged("NOTICE.md")


def read_text(dataset: str, filename: str) -> str:
    """The text of one file of a dataset: the cached copy if every file of the
    dataset is cached (as :func:`load` and :func:`status` decide), else the packaged one.

    Raises :class:`~scixplorer.exceptions.DataNotAvailable` if neither exists. The
    text is not checked; :func:`load` checks it and falls back if it is bad.
    """
    found = _dataset(dataset)
    if filename not in {remote.filename for remote in found.files}:
        names = ", ".join(remote.filename for remote in found.files)
        raise ValueError(f"{dataset!r} has no file {filename!r}; its files are: {names}")
    directory = cache_dir()
    if _cached_paths(found, directory) is not None:
        return (directory / filename).read_text(encoding="utf-8")
    if found.packaged:
        return _read_packaged(filename)
    raise _not_available(found)


def _not_available(dataset: Dataset, problem: str = "") -> DataNotAvailable:
    reason = problem or f"The {dataset.name!r} data has not been downloaded"
    return DataNotAvailable(
        f"{reason}. {dataset.description} Download it once with {_call('refresh', dataset.name)}; "
        f"it is stored in {cache_dir()}."
    )


class _TrackingReader:
    """A :data:`Reader` over one directory that remembers the last file it read."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.last: Path | None = None

    def __call__(self, filename: str) -> str:
        self.last = self.directory / filename
        return self.last.read_text(encoding="utf-8")


def _parse_cache(dataset: Dataset, parse: Callable[[Reader], S]) -> tuple[S | None, str | None]:
    """Parse the cached copy: ``(records, None)``, ``(None, None)`` if there is none,
    or ``(None, problem)`` if it cannot be read."""
    directory = cache_dir()
    paths = _cached_paths(dataset, directory)
    if paths is None:
        return None, None
    reader = _TrackingReader(directory)
    try:
        records = parse(reader)
        if len(records) == 0:
            raise ValueError("it holds no records")
    # The cache is outside our control: any failure to parse it means "unusable",
    # whatever exception the parser raised.
    except Exception as exc:
        culprit = reader.last or paths[0]
        return None, f"The cached file {culprit} could not be used ({type(exc).__name__}: {exc})"
    return records, None


def load(name: str, parse: Callable[[Reader], S]) -> S:
    """Parse dataset ``name`` with ``parse``: the cached copy if usable, else the packaged one.

    An unusable cached copy is skipped with a :class:`~scixplorer.exceptions.DataWarning`.
    Raises :class:`~scixplorer.exceptions.DataNotAvailable` if there is no usable copy
    and nothing is packaged.
    """
    dataset = _dataset(name)
    records, problem = _parse_cache(dataset, parse)
    if records is not None:
        return records
    if problem is not None:
        fallback = "using the packaged snapshot instead" if dataset.packaged else "it is ignored"
        warnings.warn(
            f"{problem}; {fallback}. Download a fresh copy with {_call('refresh', name)} "
            f"or delete it with {_call('clear', name)}.",
            DataWarning,
            stacklevel=2,
        )
    if dataset.packaged:
        return parse(_read_packaged)
    raise _not_available(dataset, problem or "")


def _records_in_use(dataset: Dataset, registration: _Registration) -> int:
    """How many records the data currently in use holds (0 if there is none)."""
    records, _ = _parse_cache(dataset, registration.parse)
    if records is not None:
        return len(records)
    return len(registration.parse(_read_packaged)) if dataset.packaged else 0


def refresh(
    *names: str, transport: httpx.BaseTransport | None = None, timeout: float = 60.0
) -> list[RefreshResult]:
    """Download datasets into the cache, replacing the copies in use if the downloads are good.

    With no names, every dataset with a public source is refreshed. Each
    download is parsed by its model before anything is written, and rejected
    (leaving the data in use untouched) if it does not parse, is empty, or has
    fewer than half as many records as the data in use. A dataset's files are
    replaced together or not at all. One dataset failing does not stop the
    others: check ``ok`` on each result.

    Requests carry only a generic User-Agent; the SciX API token is never sent.

    :param transport: an ``httpx`` transport, e.g. for testing or a proxy setup.
    :param timeout: seconds allowed for each request.
    """
    datasets = [_dataset(name) for name in names] or [d for d in DATASETS.values() if d.files]
    with httpx.Client(
        transport=transport,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        return [_refresh_one(client, dataset) for dataset in datasets]


def _refresh_one(client: httpx.Client, dataset: Dataset) -> RefreshResult:
    def failed(message: str) -> RefreshResult:
        return RefreshResult(dataset.name, False, (), f"{message}; the data in use is unchanged")

    if not dataset.files:
        return failed("no public source")
    registration = _registration(dataset.name)
    texts: dict[str, str] = {}
    for remote in dataset.files:
        try:
            response = client.get(remote.url)
        except httpx.HTTPError as exc:
            return failed(f"could not download {remote.url} ({type(exc).__name__}: {exc})")
        if response.status_code != 200:
            return failed(f"{remote.url} returned HTTP {response.status_code}")
        text = response.text.removeprefix("﻿")
        try:
            texts[remote.filename] = remote.convert(text) if remote.convert else text
        except ValueError as exc:
            return failed(f"{remote.url} is not in the expected format ({exc})")

    try:
        count = len(registration.parse(texts.__getitem__))
    # Downloads are untrusted input: any parser failure means "reject".
    except Exception as exc:
        return failed(f"the download could not be parsed ({type(exc).__name__}: {exc})")
    in_use = _records_in_use(dataset, registration)
    if count == 0:
        return failed("the download holds no records")
    if count * 2 < in_use:
        return failed(f"the download holds {count} records, fewer than half of the {in_use} in use")

    try:
        paths = _write(cache_dir(), texts)
    except OSError as exc:
        return failed(f"could not write to the cache ({exc})")
    registration.reset()
    change = f" (was {in_use})" if in_use else ""
    return RefreshResult(dataset.name, True, paths, f"{count} records{change}")


def _write(directory: Path, texts: Mapping[str, str]) -> tuple[Path, ...]:
    """Write every file atomically, replacing all of them only once all are staged."""
    directory.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for filename, text in texts.items():
            handle, name = tempfile.mkstemp(dir=directory, prefix=f".{filename}.", suffix=".tmp")
            staged.append((Path(name), directory / filename))
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    return tuple(target for _, target in staged)


def clear(*names: str) -> None:
    """Delete the cached copies of datasets (all if no names); packaged snapshots are kept.

    Models then read their packaged snapshot, or raise
    :class:`~scixplorer.exceptions.DataNotAvailable` if there is none.
    """
    datasets = [_dataset(name) for name in names] or list(DATASETS.values())
    directory = cache_dir()
    for dataset in datasets:
        for remote in dataset.files:
            (directory / remote.filename).unlink(missing_ok=True)
        registration = _registry.get(dataset.name)
        if registration is not None:
            registration.reset()


def status() -> list[DatasetStatus]:
    """Which copy of each dataset is in use, where it is, when it was modified and its size."""
    directory = cache_dir()
    result = []
    for dataset in DATASETS.values():
        cached = _cached_paths(dataset, directory)
        if cached is not None:
            result.append(_describe(dataset.name, "cache", cached))
        elif dataset.packaged:
            packaged = [resources.files(__name__) / remote.filename for remote in dataset.files]
            paths = tuple(path for path in packaged if isinstance(path, Path))
            result.append(_describe(dataset.name, "packaged", paths))
        else:
            result.append(DatasetStatus(dataset.name, "missing", (), None, None))
    return result


def _describe(name: str, source: Literal["cache", "packaged"], paths: tuple[Path, ...]) -> DatasetStatus:
    if not paths:
        return DatasetStatus(name, source, paths, None, None)
    stats = [path.stat() for path in paths]
    modified = datetime.fromtimestamp(max(stat.st_mtime for stat in stats), tz=timezone.utc)
    return DatasetStatus(name, source, paths, modified, sum(stat.st_size for stat in stats))
