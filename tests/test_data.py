from __future__ import annotations

import hashlib
import html
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import pytest

import ads
import ads._config
import ads.data as data
import ads.models.affiliation as affiliation_module
import ads.models.journal as journal_module
import ads.models.uat as uat_module
from ads.exceptions import ADSError, DataNotAvailable, DataWarning
from ads.models.affiliation import Affiliation
from ads.models.journal import Journal
from ads.models.planetary import PlanetaryFeature
from ads.models.uat import UATConcept
from ads.query import MemoryBackend

FIXTURES = Path(__file__).parent / "fixtures"
PACKAGED_JOURNALS: dict[str, str] = json.loads(
    (resources.files("ads") / "data" / "journals.json").read_text("utf-8")
)
URLS = {name: [remote.url for remote in dataset.files] for name, dataset in data.DATASETS.items()}

Route = str | bytes | httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


class Server:
    """A fake web server for ``refresh``: maps URLs to bodies, responses or exceptions."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(404, text="not found")
        if isinstance(route, Exception):
            raise route
        if isinstance(route, httpx.Response):
            return route
        if callable(route):
            return route(request)
        return httpx.Response(200, content=route.encode() if isinstance(route, str) else route)


def journal_page(titles: dict[str, str]) -> str:
    """A journal list page in the format of the ADS one."""
    rows = [
        f"""<a href="#" onClick="setfields('XXXXX');">    {html.escape(abbreviation)}    </a>    {html.escape(title)}"""
        for abbreviation, title in titles.items()
    ]
    return "<html><body><pre>\n" + "\n".join(rows) + "\n</pre></body></html>\n"


def packaged(name: str) -> bytes:
    return (resources.files("ads") / "data" / name).read_bytes()


def upstream_affiliations(extra: bool = True) -> tuple[str, str]:
    """The packaged affiliation files (the upstream ones), plus one new institution if ``extra``."""
    parent_child = packaged("affiliations.tsv").decode()
    countries = packaged("affiliations_country.tsv").decode()
    if extra:
        parent_child += "0\tZ00001\tTest Inst\tInstitute of Tests, Narnia\n"
        countries += "NA\tNarnia\t\tZ00001\tTest Inst\tInstitute of Tests, Narnia\n"
    return parent_child, countries


def good_routes() -> dict[str, Route]:
    parent_child, countries = upstream_affiliations()
    return {
        URLS["journals"][0]: journal_page({**PACKAGED_JOURNALS, "ZZTest": "Journal of Tests"}),
        URLS["affiliations"][0]: parent_child,
        URLS["affiliations"][1]: countries,
        URLS["uat"][0]: (FIXTURES / "uat.json").read_text("utf-8"),
        URLS["planetary_features"][0]: (FIXTURES / "planetary_features.csv").read_text("utf-8"),
    }


@pytest.fixture(autouse=True)
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty cache directory, and every model reset before and after the test."""
    directory = tmp_path / "cache"
    monkeypatch.setenv("SCIX_CACHE_DIR", str(directory))
    data.clear()
    yield directory
    data.clear()


def results_by_name(results: list[data.RefreshResult]) -> dict[str, data.RefreshResult]:
    return {result.name: result for result in results}


# cache_dir


def test_cache_dir_from_scix_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path / "mine"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert data.cache_dir() == tmp_path / "mine"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SCIX_CACHE_DIR", "~/scix")
    assert data.cache_dir() == tmp_path / "home" / "scix"


def test_cache_dir_from_xdg_cache_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SCIX_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data.cache_dir() == tmp_path / "xdg" / "scixplorer"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("darwin", ("Library", "Caches", "scixplorer")), ("linux", (".cache", "scixplorer"))],
)
def test_cache_dir_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, expected: tuple[str, ...]
) -> None:
    monkeypatch.setenv("SCIX_CACHE_DIR", "")
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "platform", platform)
    assert data.cache_dir() == tmp_path.joinpath("home", *expected)
    assert not (tmp_path / "home").exists()


def test_cache_dir_is_not_created_until_written(cache: Path) -> None:
    assert data.cache_dir() == cache
    data.status()
    Journal.select().count()
    assert not cache.exists()


# DATASETS, status, read_text


def test_datasets() -> None:
    assert list(data.DATASETS) == ["journals", "affiliations", "uat", "planetary_features"]
    assert {name for name, d in data.DATASETS.items() if d.packaged} == {"journals", "affiliations"}
    filenames = [remote.filename for d in data.DATASETS.values() for remote in d.files]
    assert len(filenames) == len(set(filenames)) == 5
    for name, dataset in data.DATASETS.items():
        assert dataset.name == name and dataset.licence and dataset.description
        assert dataset.files and all(remote.url.startswith("https://") for remote in dataset.files)
    with pytest.raises(TypeError):
        data.DATASETS["other"] = data.DATASETS["uat"]  # type: ignore[index]


def test_status_without_a_cache() -> None:
    found = {s.name: s for s in data.status()}
    assert [s.source for s in found.values()] == ["packaged", "packaged", "missing", "missing"]
    journals = found["journals"]
    assert [p.name for p in journals.paths] == ["journals.json"]
    assert journals.size == journals.paths[0].stat().st_size > 100_000
    assert journals.modified is not None and journals.modified.tzinfo is timezone.utc
    affiliations = found["affiliations"]
    assert [p.name for p in affiliations.paths] == ["affiliations.tsv", "affiliations_country.tsv"]
    assert affiliations.size == sum(p.stat().st_size for p in affiliations.paths)
    assert found["uat"] == data.DatasetStatus("uat", "missing", (), None, None)


def test_status_with_a_cache(cache: Path) -> None:
    data.refresh("uat", transport=Server(good_routes()).transport)
    found = {s.name: s for s in data.status()}
    uat = found["uat"]
    assert (uat.source, uat.paths) == ("cache", (cache / "uat.json",))
    assert uat.size == (cache / "uat.json").stat().st_size == len((FIXTURES / "uat.json").read_bytes())
    assert uat.modified is not None and abs(datetime.now(timezone.utc) - uat.modified) < timedelta(minutes=5)
    assert found["journals"].source == "packaged" and found["planetary_features"].source == "missing"


def test_status_needs_every_file_of_a_dataset(cache: Path) -> None:
    cache.mkdir()
    (cache / "affiliations.tsv").write_text("Parent\tChild\tAbbrev\tCanonical Name\n")
    assert {s.name: s.source for s in data.status()}["affiliations"] == "packaged"
    assert Affiliation.select().count() == 7382
    assert data.read_text("affiliations", "affiliations.tsv") == packaged("affiliations.tsv").decode()


def test_read_text(cache: Path) -> None:
    assert json.loads(data.read_text("journals", "journals.json")) == PACKAGED_JOURNALS
    with pytest.raises(DataNotAvailable, match=r'data\.refresh\("uat"\)'):
        data.read_text("uat", "uat.json")
    cache.mkdir()
    (cache / "uat.json").write_text("cached text")
    assert data.read_text("uat", "uat.json") == "cached text"
    with pytest.raises(ValueError, match="its files are: uat.json"):
        data.read_text("uat", "journals.json")


def test_unknown_dataset_names() -> None:
    server = Server(good_routes())
    for call in (
        lambda: data.refresh("journals", "nope", transport=server.transport),
        lambda: data.clear("nope"),
        lambda: data.read_text("nope", "x"),
        lambda: data.load("nope", len),
        lambda: data.register("nope", len, lambda: None),
    ):
        with pytest.raises(ValueError, match="Unknown dataset 'nope'; datasets are: journals, affiliations, uat"):
            call()
    assert server.requests == []


# refresh


def test_refresh_switches_journals_and_clear_switches_back(cache: Path) -> None:
    assert Journal.lookup("ZZTest") is None
    assert Journal.select().count() == 4502
    server = Server(good_routes())
    [result] = data.refresh("journals", transport=server.transport)
    assert result == data.RefreshResult("journals", True, (cache / "journals.json",), "4503 records (was 4502)")
    assert [str(request.url) for request in server.requests] == URLS["journals"]

    assert Journal.lookup("ZZTest").title == "Journal of Tests"
    assert Journal.select().count() == 4503
    assert [j.title for j in Journal.select().where(Journal.abbreviation == "ZZTest")] == ["Journal of Tests"]
    assert json.loads((cache / "journals.json").read_text("utf-8"))["A&A"] == PACKAGED_JOURNALS["A&A"]
    assert {s.name: s.source for s in data.status()}["journals"] == "cache"

    data.clear("journals")
    assert not (cache / "journals.json").exists()
    assert Journal.lookup("ZZTest") is None
    assert Journal.select().count() == 4502
    assert {s.name: s.source for s in data.status()}["journals"] == "packaged"


def test_refresh_everything_with_no_names(cache: Path) -> None:
    server = Server(good_routes())
    results = data.refresh(transport=server.transport)
    assert [(r.name, r.ok) for r in results] == [(name, True) for name in data.DATASETS]
    assert sorted(str(request.url) for request in server.requests) == sorted(sum(URLS.values(), []))
    assert results[2].message == "9 records"
    assert sorted(path.name for path in cache.iterdir()) == [
        "affiliations.tsv",
        "affiliations_country.tsv",
        "journals.json",
        "planetary_features.csv",
        "uat.json",
    ]
    assert UATConcept.lookup(2).name == "Stars"
    assert PlanetaryFeature.lookup(2071).name == "Gale"
    assert Affiliation.lookup("Z00001").country_code == "NA"
    data.clear()
    assert list(cache.iterdir()) == []
    assert Affiliation.lookup("Z00001") is None
    with pytest.raises(DataNotAvailable):
        UATConcept.lookup(2)


def test_refresh_sends_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "secret-scix-token"
    monkeypatch.setenv("ADS_API_TOKEN", secret)
    monkeypatch.setenv("ADS_DEV_KEY", secret)
    monkeypatch.setattr(ads._config.config, "token", secret)
    server = Server(good_routes())
    assert all(result.ok for result in data.refresh(transport=server.transport))
    assert len(server.requests) == 5
    for request in server.requests:
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert all(secret not in value for value in request.headers.values())
        assert request.headers["user-agent"] == f"scixplorer/{ads.__version__}"
        assert request.method == "GET" and request.content == b""


def test_refresh_follows_redirects() -> None:
    routes = good_routes()
    moved = "https://example.org/moved/uat.json"
    routes[moved] = routes.pop(URLS["uat"][0])
    routes[URLS["uat"][0]] = httpx.Response(302, headers={"Location": moved})
    server = Server(routes)
    [result] = data.refresh("uat", transport=server.transport)
    assert result.ok
    assert [str(request.url) for request in server.requests] == [URLS["uat"][0], moved]


def test_packaged_affiliations_are_what_refresh_writes(cache: Path) -> None:
    before = {(a.id, a.country, a.country_code, a.parent_ids) for a in Affiliation.select()}
    names = ("affiliations.tsv", "affiliations_country.tsv")
    upstream: dict[str, Route] = {url: packaged(name) for url, name in zip(URLS["affiliations"], names)}
    [result] = data.refresh("affiliations", transport=Server(upstream).transport)
    assert result.ok and result.message == "7382 records (was 7382)"
    for path, name in zip(result.files, names):
        assert path.read_bytes() == packaged(name)
    assert {(a.id, a.country, a.country_code, a.parent_ids) for a in Affiliation.select()} == before


def test_refresh_writes_atomically(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replaced: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def replace(source: Any, target: Any) -> None:
        replaced.append((Path(source), Path(target)))
        assert Path(source).read_text("utf-8")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", replace)
    [result] = data.refresh("affiliations", transport=Server(good_routes()).transport)
    assert result.ok
    assert [target for _, target in replaced] == [cache / "affiliations.tsv", cache / "affiliations_country.tsv"]
    for source, target in replaced:
        assert source.parent == cache
        assert source.name.startswith(f".{target.name}.") and source.name.endswith(".tmp")
    assert sorted(path.name for path in cache.iterdir()) == ["affiliations.tsv", "affiliations_country.tsv"]


def test_unparseable_download_is_rejected(cache: Path) -> None:
    server = Server({**good_routes(), URLS["uat"][0]: "{not json", URLS["journals"][0]: "<html>moved</html>"})
    journals, uat = data.refresh("journals", "uat", transport=server.transport)
    assert not journals.ok and journals.files == ()
    assert journals.message.startswith(f"{URLS['journals'][0]} is not in the expected format (no journal entries")
    assert not uat.ok
    assert uat.message.startswith("the download could not be parsed (JSONDecodeError")
    assert uat.message.endswith("; the data in use is unchanged")
    assert not cache.exists()
    assert Journal.select().count() == 4502


def test_wrong_shape_is_rejected() -> None:
    routes = {**good_routes(), URLS["uat"][0]: '[{"uri": "http://example.org/1", "name": "x"}]'}
    [result] = data.refresh("uat", transport=Server(routes).transport)
    assert not result.ok and "not a UAT concept URI" in result.message


def test_empty_download_is_rejected(cache: Path) -> None:
    [result] = data.refresh("uat", transport=Server({URLS["uat"][0]: "[]"}).transport)
    assert result == data.RefreshResult("uat", False, (), "the download holds no records; the data in use is unchanged")
    assert {s.name: s.source for s in data.status()}["uat"] == "missing"


def test_download_with_less_than_half_the_records_is_rejected(cache: Path) -> None:
    few = dict(list(PACKAGED_JOURNALS.items())[:2250])
    [result] = data.refresh("journals", transport=Server({URLS["journals"][0]: journal_page(few)}).transport)
    assert not result.ok
    assert result.message.startswith("the download holds 2250 records, fewer than half of the 4502 in use")
    assert Journal.select().count() == 4502 and not cache.exists()
    half = dict(list(PACKAGED_JOURNALS.items())[:2251])
    [result] = data.refresh("journals", transport=Server({URLS["journals"][0]: journal_page(half)}).transport)
    assert result.ok and Journal.select().count() == 2251


def test_rejection_leaves_the_cached_copy_untouched(cache: Path) -> None:
    data.refresh("uat", transport=Server(good_routes()).transport)
    before = (cache / "uat.json").read_bytes()
    concepts = json.loads(before)
    shrunk = json.dumps(concepts[:4])
    [result] = data.refresh("uat", transport=Server({URLS["uat"][0]: shrunk}).transport)
    assert not result.ok and "holds 4 records, fewer than half of the 9 in use" in result.message
    assert (cache / "uat.json").read_bytes() == before
    assert UATConcept.select().count() == 9
    [result] = data.refresh("uat", transport=Server({URLS["uat"][0]: json.dumps(concepts[:5])}).transport)
    assert result.ok and result.message == "5 records (was 9)"
    assert UATConcept.select().count() == 5


def test_one_failure_does_not_stop_the_others(cache: Path) -> None:
    routes = {
        **good_routes(),
        URLS["journals"][0]: httpx.Response(500, text="oops"),
        URLS["planetary_features"][0]: httpx.ConnectError("no route to host"),
    }
    results = results_by_name(data.refresh(transport=Server(routes).transport))
    assert not results["journals"].ok
    assert results["journals"].message == f"{URLS['journals'][0]} returned HTTP 500; the data in use is unchanged"
    assert not results["planetary_features"].ok
    assert results["planetary_features"].message.startswith(
        f"could not download {URLS['planetary_features'][0]} (ConnectError: no route to host)"
    )
    assert results["affiliations"].ok and results["uat"].ok
    assert sorted(path.name for path in cache.iterdir()) == ["affiliations.tsv", "affiliations_country.tsv", "uat.json"]


def test_several_files_are_replaced_all_or_nothing(cache: Path) -> None:
    routes = good_routes()
    data.refresh("affiliations", transport=Server(routes).transport)
    before = {path.name: path.read_bytes() for path in cache.iterdir()}
    parent_child, _ = upstream_affiliations(extra=False)

    # The second file is missing upstream: neither is replaced.
    missing = {URLS["affiliations"][0]: parent_child}
    [result] = data.refresh("affiliations", transport=Server(missing).transport)
    assert not result.ok and "returned HTTP 404" in result.message
    # Both download but the second does not parse: neither is replaced.
    broken = {URLS["affiliations"][0]: parent_child, URLS["affiliations"][1]: "Pays\tParent\tChild\n"}
    [result] = data.refresh("affiliations", transport=Server(broken).transport)
    assert not result.ok and "has no Country column" in result.message
    assert {path.name: path.read_bytes() for path in cache.iterdir()} == before
    assert Affiliation.lookup("Z00001") is not None


def test_failed_write_replaces_nothing(cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_mkstemp = tempfile.mkstemp
    calls = []

    def mkstemp(**kwargs: Any) -> tuple[int, str]:
        calls.append(kwargs)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real_mkstemp(**kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    [result] = data.refresh("affiliations", transport=Server(good_routes()).transport)
    assert not result.ok
    assert result.message.startswith("could not write to the cache ([Errno 28] No space left on device)")
    assert list(cache.iterdir()) == []
    assert Affiliation.lookup("Z00001") is None


def test_refresh_without_a_public_source(monkeypatch: pytest.MonkeyPatch) -> None:
    journals = data.DATASETS["journals"]
    without = data.Dataset("journals", (), True, journals.licence, journals.description)
    monkeypatch.setattr(data, "DATASETS", {**data.DATASETS, "journals": without})
    server = Server(good_routes())
    [result] = data.refresh("journals", transport=server.transport)
    assert result == data.RefreshResult(
        "journals", False, (), "no public source; the data in use is unchanged"
    )
    assert [r.name for r in data.refresh(transport=server.transport)] == ["affiliations", "uat", "planetary_features"]
    assert URLS["journals"][0] not in [str(request.url) for request in server.requests]


# Unusable cached copies


def test_corrupt_cache_falls_back_to_the_packaged_snapshot(cache: Path) -> None:
    cache.mkdir()
    for corrupt in (b"{not json", b'["a list"]', b'{"ApJ": 1}', b"\xff\xfe not utf-8", b"{}"):
        (cache / "journals.json").write_bytes(corrupt)
        journal_module._reset()
        with pytest.warns(DataWarning) as caught:
            assert Journal.select().count() == 4502
        [warning] = caught
        message = str(warning.message)
        assert message.startswith(f"The cached file {cache / 'journals.json'} could not be used (")
        assert "using the packaged snapshot instead" in message
        assert 'data.refresh("journals")' in message and 'data.clear("journals")' in message


def test_corrupt_cache_names_the_file_that_failed(cache: Path) -> None:
    data.refresh("affiliations", transport=Server(good_routes()).transport)
    (cache / "affiliations.tsv").write_text("Parent\tChild\n0\tA1\n")
    affiliation_module._reset()
    with pytest.warns(DataWarning, match=f"The cached file {cache / 'affiliations.tsv'} could not be used"):
        assert Affiliation.lookup("Z00001") is None
    assert Affiliation.select().count() == 7382


def test_corrupt_cache_without_a_snapshot(cache: Path) -> None:
    cache.mkdir()
    (cache / "uat.json").write_text("{")
    with pytest.warns(DataWarning, match="could not be used .*; it is ignored"):
        with pytest.raises(DataNotAvailable, match=r'could not be used.*data\.refresh\("uat"\)'):
            UATConcept.select().all()


# DataNotAvailable


@pytest.mark.parametrize(("model", "name"), [(UATConcept, "uat"), (PlanetaryFeature, "planetary_features")])
def test_vocabularies_need_downloading(model: Any, name: str, cache: Path) -> None:
    query = model.select().where(model.name == "x").order_by(model.id).limit(3)
    for run in (query.all, query.count, query.first, query.explain, lambda: model.lookup(1)):
        with pytest.raises(DataNotAvailable) as info:
            run()
        assert isinstance(info.value, ADSError)
        message = str(info.value)
        assert message.startswith(f"The {name!r} data has not been downloaded. ")
        assert f'Download it once with ads.data.refresh("{name}"); it is stored in {cache}.' in message
    assert not cache.exists()


# Registration and in-memory state


def test_refresh_imports_the_model_that_parses_a_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(data._registry, "uat")
    monkeypatch.delitem(sys.modules, "ads.models.uat")
    [result] = data.refresh("uat", transport=Server(good_routes()).transport)
    assert result.ok and "uat" in data._registry and "ads.models.uat" in sys.modules


def test_memory_backend_clear_drops_the_index() -> None:
    rows = [Journal(abbreviation="A", title="First")]
    backend = MemoryBackend(lambda: rows, key="abbreviation")
    assert backend.lookup("A") is rows[0]
    rows[:] = [Journal(abbreviation="A", title="Second")]
    assert backend.lookup("A").title == "First"
    backend.clear()
    assert backend.lookup("A").title == "Second"


def test_uat_reset_drops_every_cache() -> None:
    data.refresh("uat", transport=Server(good_routes()).transport)
    assert [c.id for c in UATConcept.lookup(1).descendants][:2] == [2, 3]
    assert uat_module._load.cache_info().currsize == uat_module._children.cache_info().currsize == 1
    data.clear("uat")
    assert uat_module._load.cache_info().currsize == uat_module._children.cache_info().currsize == 0


# Licence notice


def test_notice_and_gpl_text_are_package_resources() -> None:
    folder = resources.files("ads.data")
    for name in ("NOTICE.md", "LICENSE-GPL-3.0.txt", "journals.json", "affiliations.tsv", "affiliations_country.tsv"):
        assert (folder / name).is_file(), name
    gpl = (folder / "LICENSE-GPL-3.0.txt").read_bytes()
    assert gpl.startswith(b"                    GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007")
    # The standard text, as published at https://www.gnu.org/licenses/gpl-3.0.txt.
    assert hashlib.sha256(gpl).hexdigest() == "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"


def test_notices() -> None:
    text = data.notices()
    assert text == (resources.files("ads.data") / "NOTICE.md").read_text("utf-8")
    flat = " ".join(text.split())
    assert "not covered by the MIT licence" in flat
    for dataset in data.DATASETS.values():
        assert f"Licence: {dataset.licence}" in flat, dataset.name
    for name in ("affiliations.tsv", "affiliations_country.tsv"):
        assert hashlib.sha256(packaged(name)).hexdigest() in flat, name
    assert "LICENSE-GPL-3.0.txt" in flat and "42b44d1a6f6b9e391170622f2b4ff2e5faca7bb7" in flat


# The journal list converter


def test_journal_list_to_json() -> None:
    converted = json.loads(data._journal_list_to_json((FIXTURES / "journals.html").read_text("utf-8")))
    assert converted == {
        "A&A": "Astronomy and Astrophysics",
        "ApJ": "The Astrophysical Journal",
        "asd  soft": "Astrophysics Source Code Library (asd)",
        "ATTTJ": "AT&T Technical Journal",
        "Zoot": "Zootaxa",
    }
    assert data._journal_list_to_json(journal_page(PACKAGED_JOURNALS)) == json.dumps(
        PACKAGED_JOURNALS, ensure_ascii=False, indent=0
    )
    with pytest.raises(ValueError, match="no journal entries"):
        data._journal_list_to_json("<html></html>")


# No implicit I/O


def test_imports_and_queries_read_no_data_and_open_no_sockets(cache: Path, tmp_path: Path) -> None:
    cache.mkdir()
    (cache / "uat.json").write_bytes((FIXTURES / "uat.json").read_bytes())
    script = f"""
import os, sys
cache = {str(cache)!r}
events = []
def hook(event, args):
    if event.startswith("socket."):
        events.append(event)
    elif event == "open" and not isinstance(args[0], int):
        path = os.fsdecode(os.fspath(args[0]))
        if path.startswith(cache) or path.endswith((".json", ".tsv", ".csv", ".html")):
            events.append(os.path.basename(path))
sys.addaudithook(hook)
import ads.data, ads.exceptions, ads.models.uat, ads.models.planetary, ads.models.journal, ads.models.affiliation
from ads.models.uat import UATConcept
from ads.models.planetary import PlanetaryFeature
query = UATConcept.select().where(UATConcept.name == "stars")
PlanetaryFeature.select().where(PlanetaryFeature.target == "Mars")
ads.data.status()
assert events == [], events
assert [c.id for c in query] == [2, 4, 6, 9]
UATConcept.lookup(2).descendants
assert events == ["uat.json"], events
try:
    PlanetaryFeature.lookup(1)
except ads.exceptions.DataNotAvailable:
    pass
assert not [e for e in events if e.startswith("socket.")], events
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
