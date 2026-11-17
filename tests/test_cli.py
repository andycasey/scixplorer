"""The ``scix`` command, run in-process against a fake API."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import scixplorer
from scixplorer import Document, config, data
from scixplorer.cli import Interpretation, Reading, default_sort, interpret, main
from scixplorer.cli.output import Style, fit, listing, pdf_url, reference, short_authors, wrap
from scixplorer.cli.session import Session

from cli_fakes import FakeAPI, documents, install_thesaurus

ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def thesaurus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("COLUMNS", "80")
    install_thesaurus()
    yield
    data.clear("uat")


def run(*argv: str, api: FakeAPI | None = None) -> tuple[int, str, str, FakeAPI]:
    """Run the command; ``(exit status, standard output, standard error, the API it talked to)``."""
    api = api or FakeAPI()
    out, err = io.StringIO(), io.StringIO()
    status = main(list(argv), client=api.client(), stdin=io.StringIO(), stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue(), api


def empty_then(api: FakeAPI) -> None:
    """Make the API find nothing for the first search and everything for the next."""
    calls = iter([0])

    def reply(request: httpx.Request) -> httpx.Response:
        if next(calls, None) == 0:
            return httpx.Response(200, json={"response": {"numFound": 0, "start": 0, "docs": []}})
        api.reply = None
        try:
            return api(request)
        finally:
            api.requests.pop()

    api.reply = reply


# Printing results.


def test_the_list_says_how_the_search_was_read_and_shows_each_document() -> None:
    status, out, err, api = run("Casey", "2005")
    assert status == 0 and err == ""
    assert api.params()["q"] == 'author:"Casey" AND year:2005'
    lines = out.splitlines()
    assert lines[0] == "author Casey · year 2005"
    assert lines[1] == "3 documents, most cited first"
    assert lines[3:7] == [
        "1  Paper 0 about 2 things & more",
        "   Casey · ApJ 880, 100 (2019) · 100 citations",
        "   2019ApJ...880.0000C",
        "",
    ]
    assert "\x1b" not in out  # no colour when the output is not a terminal


def test_one_request_fetches_the_documents_and_their_number() -> None:
    api = FakeAPI(documents(40))
    status, out, _, _ = run("exoplanets", "-n", "5", api=api)
    assert status == 0
    assert out.splitlines()[:2] == ["concept Exoplanets (UAT 498)", "40 documents, newest first; showing 5"]
    assert len(api.searches) == 1
    params = api.params()
    assert (params["q"], params["rows"], params["sort"]) == ("uat_id:498", "5", "date desc,id asc")
    assert set(params["fl"].split(",")) >= {"bibcode", "title", "author", "abstract", "citation_count"}


def test_limit_all_fetches_everything() -> None:
    api = FakeAPI(documents(25))
    _, out, _, _ = run("Casey", "--limit", "all", "--bibcodes", api=api)
    assert len(out.splitlines()) == 25


@pytest.mark.parametrize(
    "argv, sort",
    [
        (["Casey"], "date desc,id asc"),
        (["Casey", "2005"], "citation_count desc,id asc"),
        (["Casey", "since", "2020"], "citation_count desc,id asc"),
        (["Casey", "2005", "-s", "date"], "date desc,id asc"),
        (["Casey", "--sort", "citations"], "citation_count desc,id asc"),
        (["Casey", "-s", "reads"], "read_count desc,id asc"),
        (["Casey", "-s", "relevance"], "score desc,id asc"),
        (["Casey", "-s", "oldest"], "date asc,id asc"),
        (["Casey", "-s", "author"], "first_author asc,id asc"),
    ],
)
def test_sort(argv: list[str], sort: str) -> None:
    _, _, _, api = run(*argv)
    assert api.params()["sort"] == sort


def test_default_sort() -> None:
    assert default_sort(interpret("Casey")) == "date"
    assert default_sort(interpret("Casey 2005")) == "citations"


def test_options_may_follow_the_words() -> None:
    status, out, _, api = run("Casey", "2005", "--bibcodes", "-n", "2")
    assert status == 0 and out.splitlines() == ["2019ApJ...880.0000C", "2019ApJ...880.0001C"]
    assert api.params()["rows"] == "2"


def test_other_formats_keep_standard_output_for_the_data() -> None:
    status, out, err, _ = run("Casey", "--bibcodes")
    assert status == 0
    assert out.splitlines() == ["2019ApJ...880.0000C", "2019ApJ...880.0001C", "2019ApJ...880.0002C"]
    assert err.splitlines() == ["author Casey", "3 documents, newest first"]


def test_json() -> None:
    status, out, _, _ = run("Casey", "--json")
    rows = json.loads(out)
    assert status == 0 and [row["bibcode"] for row in rows] == ["2019ApJ...880.0000C", "2019ApJ...880.0001C", "2019ApJ...880.0002C"]
    assert rows[0]["author"] == ["Casey, Andrew R."] and rows[0]["citation_count"] == 100


@pytest.mark.parametrize("argv", [["--bibtex"], ["--format", "bibtex"], ["-f", "bibtex"]])
def test_bibtex_comes_from_the_export_service(argv: list[str]) -> None:
    status, out, _, api = run("Casey", "-n", "2", *argv)
    assert status == 0
    assert api.exported == [["2019ApJ...880.0000C", "2019ApJ...880.0001C"]]
    assert out.startswith("@ARTICLE{2019ApJ...880.0000C,") and out.endswith("}\n")


def test_no_documents_is_exit_status_1() -> None:
    api = FakeAPI([])
    status, out, _, _ = run("Casey, A.", "2005", api=api)
    assert status == 1
    assert out.splitlines() == ["author Casey, A. · year 2005", "No documents."]
    assert len(api.searches) == 1  # nothing to widen


def test_a_search_that_finds_nothing_is_widened() -> None:
    api = FakeAPI()
    empty_then(api)
    status, out, _, _ = run("Hubble", "tension", api=api)
    assert status == 0
    assert api.queries == [
        'author:"Hubble" AND (author:"tension" OR abs:"tension")',
        '(author:"Hubble" OR abs:"Hubble") AND (author:"tension" OR abs:"tension")',
    ]
    assert out.splitlines()[:3] == [
        "author Hubble · author or text tension",
        "Nothing matched, so searched for: author or text Hubble · author or text tension",
        "3 documents, newest first",
    ]


def test_refereed_and_collection() -> None:
    try:
        _, out, _, api = run("Casey", "--refereed", "--collection", "astronomy")
        assert api.params()["q"] == 'author:"Casey" AND property:"refereed" AND database:"astronomy"'
        assert out.splitlines()[0] == "author Casey · property refereed"
    finally:
        config.collection = None


def test_query_sends_the_words_as_solr() -> None:
    _, out, _, api = run("-q", "author:Casey", "year:2005")
    assert api.params()["q"] == "author:Casey year:2005"
    assert out.splitlines()[0] == "query author:Casey year:2005"


def test_open_shows_the_first_result_in_the_web_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    status, out, _, api = run("ApJ", "880", "100", "--open")
    assert status == 0
    assert opened == ["https://scixplorer.org/abs/2019ApJ...880.0000C/abstract"]
    assert opened[0] in out and api.params()["rows"] == "1"


# Explaining.


def test_explain_sends_nothing() -> None:
    status, out, err, api = run("--explain", "Casey", "et", "al.", "2019", "ApJ", "exoplanets")
    assert status == 0 and err == "" and api.requests == []
    assert out.splitlines() == [
        "Casey et al.  first author  Casey",
        "2019          year          2019",
        "ApJ           journal       The Astrophysical Journal (ApJ)",
        "exoplanets    concept       Exoplanets (UAT 498)",
        "",
        'query  first_author:"Casey" AND year:2019 AND bibstem:"ApJ" AND uat_id:498',
        "sort   most cited first",
    ]


def test_notes_go_to_standard_error() -> None:
    data.clear("uat")
    status, out, err, _ = run("spectra", "--bibcodes")
    assert status == 0 and "scix --refresh-data" in err and "refresh" not in out
    status, out, _, _ = run("--explain", "spectra")
    assert "scix --refresh-data" in out


# Errors.


def test_no_words_is_a_usage_error() -> None:
    status, out, err, api = run()
    assert status == 2 and out == "" and api.requests == []
    assert "usage: scix" in err and "scix Casey 2005" in err


def test_an_api_error_is_reported_without_a_traceback() -> None:
    api = FakeAPI()
    api.reply = lambda request: httpx.Response(401, json={"error": "Unauthorized"})
    status, out, err, _ = run("Casey", "--bibcodes", api=api)
    assert status == 2 and out == ""
    assert err.splitlines()[-1].startswith("scix: ") and "401" in err


def test_bad_options_exit_with_status_2(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["Casey", "-n", "0"], ["Casey", "-n", "many"], ["Casey", "--sort", "size"], ["Casey", "-f", "pdf"]):
        with pytest.raises(SystemExit) as exit_:
            main(argv)
        assert exit_.value.code == 2
    assert "expected a number or 'all'" in capsys.readouterr().err


def test_refresh_data(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [data.RefreshResult("journals", True, (), ""), data.RefreshResult("uat", False, (), "no network")]
    monkeypatch.setattr(data, "refresh", lambda: results)
    status, out, _, api = run("--refresh-data")
    assert status == 2 and api.requests == []
    assert out.splitlines() == ["journals: updated", "uat: failed - no network"]
    results.pop()
    assert run("--refresh-data")[0] == 0


# The entry points.


def test_the_scix_script_is_declared() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["scripts"] == {"scix": "scixplorer.cli:main"}


def test_python_m_scixplorer_runs_the_command() -> None:
    result = subprocess.run([sys.executable, "-m", "scixplorer", "--version"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == f"scix {scixplorer.__version__}"
    result = subprocess.run([sys.executable, "-m", "scixplorer", "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "how words are read" in result.stdout


def test_importing_the_package_does_not_import_the_command() -> None:
    script = "import sys, scixplorer; assert not any(name.split('.')[:2] == ['scixplorer', 'cli'] for name in sys.modules)"
    assert subprocess.run([sys.executable, "-c", script]).returncode == 0


# The session.


def test_session_fetch_pages_with_offset_and_knows_the_total() -> None:
    api = FakeAPI(documents(30))
    session = Session(api.client())
    interpretation = interpret("Casey")
    found, total = session.fetch(interpretation, "date", limit=10, offset=25)
    assert [document.bibcode for document in found] == [f"2019ApJ...880.{i:04d}C" for i in range(25, 30)]
    assert total == 30 and api.params()["start"] == "25"
    with pytest.raises(ValueError, match="nothing to search for"):
        session.query(interpret(""), "date")


def test_a_search_that_can_never_match_sends_nothing() -> None:
    api = FakeAPI()
    never = Reading("journal", "x", "x", Document.journal.title == "The Journal of Nothing At All")
    found = Session(api.client()).find(Interpretation("x", (never,)), "date", limit=5)
    assert (found.total, found.documents, found.widened, api.requests) == (0, [], False, [])


# Text helpers.


def doc(**values: Any) -> Document:
    return Document(**values)


def test_short_authors_and_reference() -> None:
    assert short_authors(doc(author=["Casey, A."])) == "Casey"
    assert short_authors(doc(author=["Casey, A.", "Ji, A."])) == "Casey & Ji"
    assert short_authors(doc(author=["Casey, A.", "Ji, A.", "Hogg, D."])) == "Casey et al."
    assert short_authors(doc(author=None)) == ""
    assert reference(doc(bibcode="2019ApJ...880..125C", volume="880", page=["125"], year=2019)) == "ApJ 880, 125 (2019)"
    # A preprint's bibcode has no journal abbreviation: the publication's name is used.
    assert reference(doc(bibcode="2026arXiv260916720O", pub="arXiv e-prints", year=2026)) == "arXiv (2026)"
    assert reference(doc(bibcode="2005physics..12113J", pub="arXiv e-prints", year=2005)) == "arXiv e-prints (2005)"
    assert reference(doc(bibcode=None, pub=None)) == ""


def test_pdf_url_prefers_the_publisher() -> None:
    paper = doc(bibcode="2019A&A...880..125C", esources=["EPRINT_PDF", "PUB_PDF"])
    assert pdf_url(paper) == "https://scixplorer.org/link_gateway/2019A&A...880..125C/PUB_PDF"
    assert pdf_url(doc(bibcode="2019A&A...880..125C", esources=["PUB_HTML"])) is None


def test_fit_and_wrap_count_terminal_columns() -> None:
    assert fit("abcdef", 4) == "abc…" and fit("abc", 5, pad=True) == "abc  " and fit("abc", 5, pad=True, right=True) == "  abc"
    assert fit("日本語のタイトル", 7) == "日本語…" and fit("anything", 0) == ""
    assert wrap("one two three four", 9) == ["one two", "three", "four"]
    assert wrap("abcdefghijklmnop", 8) == ["abcdefgh", "ijklmnop"]
    assert wrap("", 10) == [""]


def test_listing_styles_only_when_asked() -> None:
    papers = [doc(**row) for row in documents(2)]
    plain = listing(papers, width=60, style=Style(False), start=9)
    assert plain[0] == " 9  Paper 0 about 2 things & more" and plain[4].startswith("10  ")
    styled = listing(papers, width=60, style=Style(True))
    assert "\x1b[1mPaper 0 about 2 things & more\x1b[0m" in styled[0]
