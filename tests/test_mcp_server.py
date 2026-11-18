"""The MCP server run by ``scix --mcp``: its tools against the offline sample API, and the protocol itself."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

pytest.importorskip("mcp.server.mcpserver", reason="the MCP server needs the mcp package, version 2 or later")

from mcp.client import Client as MCPClient  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import scixplorer.client  # noqa: E402
from scixplorer import data, mcp_server  # noqa: E402
from scixplorer.cli import EXPORT_FORMATS, main  # noqa: E402
from scixplorer.cli.output import SORTS  # noqa: E402
from scixplorer.mcp_server import ExportFormat, SortName, Tools, build  # noqa: E402

from docs_fakes import PLANETARY_FEATURES_CSV, UAT_JSON, DocsAPI  # noqa: E402

EMISSION = "Rk3bq7xQTgS0vVZ2mN8cWw"
READS = ["search", "explain_search", "get_document", "resolve", "export", "metrics", "list_libraries", "get_library"]
WRITES = ["create_library", "add_to_library", "combine_libraries", "edit_library", "remove_from_library", "delete_library"]


@pytest.fixture(autouse=True)
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The sample thesaurus and gazetteer in a cache of their own, and no default client.

    Every request must go through the client the tools were given: one that
    reached for the default client would be talking to the real API.
    """

    def fail() -> None:
        raise AssertionError("the tools must use the client they were given")

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("SCIX_CACHE_DIR", str(cache))
    data.clear()
    (cache / "uat.json").write_text(UAT_JSON, encoding="utf-8")
    (cache / "planetary_features.csv").write_text(PLANETARY_FEATURES_CSV, encoding="utf-8")
    monkeypatch.setattr(scixplorer.client, "get_default_client", fail)
    monkeypatch.setattr(mcp_server, "get_default_client", fail)
    yield
    data.clear()


@pytest.fixture
def api() -> DocsAPI:
    return DocsAPI()


@pytest.fixture
def tools(api: DocsAPI) -> Tools:
    return Tools(api.client())


def paths(api: DocsAPI) -> list[str]:
    return [f"{request.method} {request.url.path.removeprefix('/v1')}" for request in api.requests]


# Searching.


def test_search_says_how_it_read_the_query_and_returns_brief_documents(tools: Tools, api: DocsAPI) -> None:
    result = tools.search("Kewley 2006")
    assert result == {
        "read_as": "author Kewley · year 2006",
        "solr_query": 'author:"Kewley" AND year:2006',
        "sort": "most cited first",
        "total": 1,
        "offset": 0,
        "documents": [
            {
                "bibcode": "2006MNRAS.372..961K",
                "title": "The host galaxies and classification of active galactic nuclei",
                "authors": "Kewley, Lisa J.; Groves, Brent; Kauffmann, Guinevere; and 1 more",
                "year": 2006,
                "publication": "MNRAS 372, 961 (2006)",
                "citations": 2000,
            }
        ],
    }
    assert paths(api) == ["GET /search/query"]
    json.dumps(result)


def test_search_reads_concepts_journals_and_named_fields(tools: Tools) -> None:
    assert tools.search("exoplanets")["solr_query"] == "uat_id:498"
    assert tools.search("Seager ApJ 2000")["solr_query"] == 'author:"Seager" AND bibstem:"ApJ" AND year:2000'
    found = tools.search('first_author:"Kewley, L" year:2000-2010', sort="oldest")
    assert found["solr_query"] == 'first_author:"Kewley, L" AND year:[2000 TO 2010]' and found["sort"] == "oldest first"
    assert [document["year"] for document in found["documents"]] == [2001, 2006, 2008]


def test_search_pages_and_can_include_abstracts(tools: Tools, api: DocsAPI) -> None:
    first = tools.search("exoplanets", limit=2)
    second = tools.search("exoplanets", limit=2, offset=2, abstracts=True)
    assert first["total"] == second["total"] == 3
    assert len(first["documents"]) == 2 and len(second["documents"]) == 1
    assert "abstract" not in first["documents"][0] and "abstract" in second["documents"][0]
    assert second["offset"] == 2 and api.requests[-1].url.params["start"] == "2"


def test_search_with_solr_sends_the_query_as_written(tools: Tools) -> None:
    result = tools.search("author:Kewley year:2008", solr=True)
    assert result["read_as"] == "query author:Kewley year:2008" and result["solr_query"] == "author:Kewley year:2008"


def test_a_search_that_finds_nothing_is_widened_and_says_so(tools: Tools) -> None:
    # No author is called Metallicity, but titles hold the word.
    result = tools.search("Metallicity")
    assert result["read_as"] == "author or text Metallicity" and result["total"] == 3
    assert result["note"] == "Nothing matched author Metallicity, so the search was widened."
    nothing = tools.search('"Kewley, L" 1850')
    assert nothing["total"] == 0 and nothing["documents"] == [] and "note" not in nothing


def test_explain_search_sends_nothing(tools: Tools, api: DocsAPI) -> None:
    assert tools.explain_search("Kewley et al. 2008 ApJ") == {
        "read_as": [
            {"typed": "Kewley et al.", "as": "first author", "meaning": "Kewley"},
            {"typed": "2008", "as": "year", "meaning": "2008"},
            {"typed": "ApJ", "as": "journal", "meaning": "The Astrophysical Journal (ApJ)"},
        ],
        "solr_query": 'first_author:"Kewley" AND year:2008 AND bibstem:"ApJ"',
        "sort": "most cited first",
    }
    assert api.requests == []


def test_notes_are_passed_on(tools: Tools) -> None:
    data.clear("uat")
    assert "not downloaded" in tools.explain_search("spectra")["note"]
    assert "not downloaded" in tools.search("spectra")["note"]


def test_get_document(tools: Tools, api: DocsAPI) -> None:
    document = tools.get_document("2006MNRAS.372..961K")
    assert document["title"] == "The host galaxies and classification of active galactic nuclei"
    assert document["authors"] == ["Kewley, Lisa J.", "Groves, Brent", "Kauffmann, Guinevere", "Heckman, Tim"]
    assert (document["author_count"], document["year"], document["date"]) == (4, 2006, "2006-11-01")
    assert document["reference"] == "MNRAS 372, 961 (2006)" and document["refereed"] is True
    assert document["doi"] == "10.1111/j.1365-2966.2006.10859.x" and document["citations"] == 2000
    assert document["url"] == "https://scixplorer.org/abs/2006MNRAS.372..961K/abstract"
    assert api.requests[-1].url.params["q"] == 'bibcode:"2006MNRAS.372..961K"'
    json.dumps(document)


@pytest.mark.parametrize(
    "identifier, q",
    [
        ("10.1111/j.1365-2966.2006.10859.x", 'doi:"10.1111/j.1365-2966.2006.10859.x"'),
        ("https://scixplorer.org/abs/2006MNRAS.372..961K/abstract", 'bibcode:"2006MNRAS.372..961K"'),
        ("arXiv:astro-ph/0605681", 'arxiv:"astro-ph/0605681"'),
        ("something else", 'identifier:"something else"'),
    ],
)
def test_get_document_by_other_identifiers(tools: Tools, api: DocsAPI, identifier: str, q: str) -> None:
    try:
        tools.get_document(identifier)
    except ToolError:
        pass  # the sample has no such document; the query is what matters here
    assert api.requests[-1].url.params["q"] == q


# Resolving names, which needs no request.


def test_resolve_a_journal_by_title_or_abbreviation(tools: Tools, api: DocsAPI) -> None:
    result = tools.resolve("journal", "astrophysical journal", limit=2)
    assert result["matches"] == [
        {"abbreviation": "ApJ", "title": "The Astrophysical Journal", "search_with": 'journal:"ApJ"'},
        {"abbreviation": "ApJS", "title": "The Astrophysical Journal Supplement Series", "search_with": 'journal:"ApJS"'},
    ]
    assert tools.resolve("journal", "mnras")["matches"][0]["abbreviation"] == "MNRAS"
    assert tools.resolve("journal", "Nature")["matches"][0]["abbreviation"] == "Natur"
    assert tools.resolve("journal", "no journal has this title")["matches"] == []
    assert api.requests == []


def test_what_resolve_gives_can_be_searched(tools: Tools) -> None:
    for kind, name in (("journal", "Astrophysical Journal"), ("concept", "exoplanets"), ("planetary_feature", "Gale")):
        match = tools.resolve(kind, name)["matches"][0]  # type: ignore[arg-type]
        assert tools.search(match["search_with"])["total"] >= 1, match
    harvard = tools.resolve("institution", "Harvard")["matches"][0]
    assert tools.explain_search(harvard["search_with"])["solr_query"] == 'inst:"Harvard U"'
    assert tools.explain_search(harvard["search_without_its_parts_with"])["solr_query"] == 'aff_id:"A00211"'


def test_resolve_an_institution(tools: Tools) -> None:
    first, second = tools.resolve("institution", "Harvard", limit=2)["matches"]
    assert first == {
        "id": "A00211",
        "abbreviation": "Harvard U",
        "name": "Harvard University, Massachusetts",
        "country": "United States",
        "part_of": None,
        "search_with": 'inst:"Harvard U"',
        "search_without_its_parts_with": "aff_id:A00211",
    }
    # A department is searched by its id: its abbreviation is not its alone.
    assert (second["part_of"], second["search_with"]) == ("Harvard U", "aff_id:A00212")
    assert "search_without_its_parts_with" not in second
    assert tools.resolve("institution", "A00211")["matches"][0]["abbreviation"] == "Harvard U"


def test_resolve_a_concept_and_a_planetary_feature(tools: Tools) -> None:
    concept, *others = tools.resolve("concept", "exoplanet")["matches"]
    assert (concept["id"], concept["name"], concept["search_with"]) == (498, "Exoplanets", "uat:498")
    assert {other["name"] for other in others} >= {"Exoplanet astronomy", "Exoplanet atmospheres"}
    assert concept["broader"] == ["Exoplanet astronomy"] and "Hot Jupiters" in concept["narrower"]
    assert tools.resolve("concept", "extrasolar planets")["matches"][0]["id"] == 498
    (feature,) = tools.resolve("planetary_feature", "Gale")["matches"]
    assert feature == {"id": 2071, "name": "Gale", "on": "Mars", "type": "Crater", "search_with": "planetary_feature_id:2071"}


def test_resolve_says_what_to_download(tools: Tools) -> None:
    data.clear()
    for kind in ("concept", "planetary_feature"):
        with pytest.raises(ToolError, match="refresh"):
            tools.resolve(kind, "anything")  # type: ignore[arg-type]


# Export and metrics.


def test_export(tools: Tools, api: DocsAPI) -> None:
    text = tools.export(["2006MNRAS.372..961K", " 2006MNRAS.372..961K "])
    assert text.startswith("@ARTICLE{2006MNRAS.372..961K,") and text.count("@ARTICLE") == 1
    assert paths(api) == ["POST /export/bibtex"]
    tools.export(["2006MNRAS.372..961K"], format="ris")
    assert paths(api)[-1] == "POST /export/ris"


def test_metrics_of_bibcodes_or_of_a_search(tools: Tools, api: DocsAPI) -> None:
    result = tools.metrics(bibcodes=["2006MNRAS.372..961K", "2008ApJ...681.1183K", "1900Fake.....0....0X"])
    assert result == {
        "documents": 3,
        "skipped": ["1900Fake.....0....0X"],
        "basic": {"number of papers": 2},
        "citations": {"total number of citations": 3350},
        "indicators": {"h": 2},
        "refereed": {
            "basic": {"number of papers": 2},
            "citations": {"total number of citations": 3350},
            "indicators": {"h": 2},
        },
    }
    assert json.loads(api.requests[-1].content)["types"] == ["basic", "citations", "indicators"]
    api.requests.clear()
    by_author = tools.metrics(query="Kewley", per_year=True)
    assert by_author["documents"] == 3 and by_author["indicators"] == {"h": 3} and "per_year" in by_author
    assert paths(api) == ["GET /search/query", "POST /metrics"]
    assert api.requests[0].url.params["fl"].split(",")[0] == "bibcode" and "abstract" not in api.requests[0].url.params["fl"]


def test_metrics_leaves_out_the_list_of_self_citations(tools: Tools, api: DocsAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {"citation stats": {"number of self-citations": 2, "self-citations": ["A", "B"]}}
    monkeypatch.setattr(mcp_server, "metrics_service", lambda *args, **kwargs: answer)
    assert tools.metrics(bibcodes=["A"])["citations"] == {"number of self-citations": 2}


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({}, "either bibcodes or query"),
        ({"bibcodes": ["A"], "query": "Kewley"}, "either bibcodes or query"),
        ({"bibcodes": [" "]}, "No bibcodes were given"),
        ({"query": '"Kewley, L" 1850'}, "matched no documents"),
        ({"bibcodes": ["1900Fake.....0....0X"]}, "No data available"),
    ],
)
def test_metrics_errors_say_what_to_do(tools: Tools, arguments: dict[str, Any], message: str) -> None:
    with pytest.raises(ToolError, match=message):
        tools.metrics(**arguments)


# Libraries.


def test_list_libraries(tools: Tools, api: DocsAPI) -> None:
    libraries = tools.list_libraries()["libraries"]
    assert [library["name"] for library in libraries] == ["Emission-line galaxies", "Exoplanet transits"]
    assert libraries[0] == {
        "id": EMISSION,
        "name": "Emission-line galaxies",
        "description": "Classifying galaxies by their emission lines",
        "documents": 2,
        "public": False,
        "permission": "owner",
        "owner": "example_user",
        "last_modified": "2024-01-17T16:40:00+00:00",
    }
    assert paths(api) == ["GET /biblib/libraries"]


@pytest.mark.parametrize("library", ["Emission-line galaxies", "emission-line GALAXIES", EMISSION, f" {EMISSION} "])
def test_get_library_by_name_or_id(tools: Tools, library: str) -> None:
    result = tools.get_library(library, sort="oldest")
    assert result["library"]["id"] == EMISSION and result["total"] == 2 and result["offset"] == 0
    assert [document["bibcode"] for document in result["documents"]] == ["2006MNRAS.372..961K", "2008ApJ...681.1183K"]


def test_an_unknown_library_lists_the_ones_there_are(tools: Tools) -> None:
    with pytest.raises(ToolError, match="No library has the id or name 'Nope'.*'Emission-line galaxies', 'Exoplanet transits'"):
        tools.get_library("Nope")
    with pytest.raises(ToolError, match="No library has the id or name 'NotALibraryId0123456789'"):
        tools.get_library("NotALibraryId0123456789")
    with pytest.raises(ToolError, match="give its id or its name"):
        tools.get_library(" ")


def test_two_libraries_with_one_name_need_an_id(tools: Tools, api: DocsAPI) -> None:
    tools.create_library("Twin")
    tools.create_library("Twin")
    with pytest.raises(ToolError, match="2 libraries are named 'Twin'; give the id of one of them: NewLibrary01"):
        tools.get_library("Twin")
    assert tools.get_library("NewLibrary02xQ7vT2mKp9w")["library"]["name"] == "Twin"


def test_create_a_library_empty_from_bibcodes_or_from_a_search(tools: Tools, api: DocsAPI) -> None:
    assert tools.create_library("Empty")["created"]["documents"] == 0
    created = tools.create_library("Transits", description="About transits", public=True, query="exoplanets", limit=2)["created"]
    assert (created["name"], created["description"], created["public"], created["documents"]) == ("Transits", "About transits", True, 2)
    listed = tools.create_library("Listed", bibcodes=["2006MNRAS.372..961K"])["created"]
    assert api.libraries[listed["id"]]["documents"] == ["2006MNRAS.372..961K"]
    with pytest.raises(ToolError, match="either bibcodes or query"):
        tools.create_library("Both", bibcodes=["A"], query="Kewley")


def test_add_to_and_remove_from_a_library(tools: Tools, api: DocsAPI) -> None:
    added = tools.add_to_library("Emission-line galaxies", bibcodes=["2001ApJ...556..121K", "2006MNRAS.372..961K"])
    assert (added["added"], added["already_there"], added["library"]["documents"]) == (1, 1, 3)
    by_search = tools.add_to_library(EMISSION, query="exoplanets", limit=1)
    assert by_search["added"] == 1 and len(api.libraries[EMISSION]["documents"]) == 4
    removed = tools.remove_from_library("Emission-line galaxies", ["2001ApJ...556..121K"])
    assert (removed["removed"], removed["library"]["documents"]) == (1, 3)
    assert "2001ApJ...556..121K" not in api.libraries[EMISSION]["documents"]


def test_edit_a_library(tools: Tools, api: DocsAPI) -> None:
    edited = tools.edit_library("Emission-line galaxies", name="BPT", public=True)["library"]
    assert (edited["name"], edited["public"], edited["description"]) == ("BPT", True, "Classifying galaxies by their emission lines")
    assert api.libraries[EMISSION]["name"] == "BPT"
    with pytest.raises(ToolError, match="at least one of name, description or public"):
        tools.edit_library("BPT")


@pytest.mark.parametrize("operation, size", [("union", 4), ("intersection", 0), ("difference", 2)])
def test_combine_libraries_makes_a_new_one(tools: Tools, api: DocsAPI, operation: str, size: int) -> None:
    created = tools.combine_libraries(operation, "Emission-line galaxies", ["Exoplanet transits"], name="Combined")["created"]  # type: ignore[arg-type]
    assert (created["name"], created["documents"]) == ("Combined", size)
    assert len(api.libraries) == 3 and len(api.libraries[EMISSION]["documents"]) == 2


def test_delete_a_library(tools: Tools, api: DocsAPI) -> None:
    deleted = tools.delete_library("Exoplanet transits")["deleted"]
    assert deleted["name"] == "Exoplanet transits" and deleted["documents"] == 2
    assert list(api.libraries) == [EMISSION]
    with pytest.raises(ToolError, match="No library has the id or name 'Exoplanet transits'"):
        tools.delete_library("Exoplanet transits")


# Errors.


def test_an_empty_query_or_name_is_an_error_with_a_message(tools: Tools, api: DocsAPI) -> None:
    for call in (
        lambda: tools.search("  "),
        lambda: tools.explain_search(""),
        lambda: tools.get_document(" "),
        lambda: tools.resolve("journal", " "),
    ):
        with pytest.raises(ToolError, match="empty"):
            call()
    with pytest.raises(ToolError, match="no document with the identifier '1900Fake.....0....0X'"):
        tools.get_document("1900Fake.....0....0X")
    assert all(request.url.path.endswith("/search/query") for request in api.requests)


def test_an_api_error_reaches_the_caller_with_its_message(tools: Tools, api: DocsAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_route", lambda request, path: httpx.Response(401, json={"error": "Unauthorized"}))
    for call in (lambda: tools.search("Kewley"), tools.list_libraries, lambda: tools.export(["A"]), lambda: tools.metrics(bibcodes=["A"])):
        with pytest.raises(ToolError, match="401"):
            call()


def test_a_fault_in_the_server_is_not_dressed_up_as_a_tool_error(tools: Tools, monkeypatch: pytest.MonkeyPatch) -> None:
    def fault(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(tools.session, "find", fault)
    with pytest.raises(RuntimeError):
        tools.search("Kewley")


# The server.


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_the_server_offers_every_tool_with_a_description(api: DocsAPI) -> None:
    server = build(api.client())
    listed = run(server.list_tools())
    assert [tool.name for tool in listed] == READS + WRITES
    assert (server.name, server.instructions) == ("scixplorer", mcp_server.INSTRUCTIONS)
    for tool in listed:
        assert tool.description and tool.title and tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is (tool.name in READS), tool.name
        for name, schema in tool.input_schema["properties"].items():
            assert schema.get("description"), f"{tool.name}.{name} has no description"
    by_name = {tool.name: tool for tool in listed}
    assert by_name["delete_library"].annotations.destructive_hint is True
    assert by_name["create_library"].annotations.destructive_hint is False
    assert by_name["explain_search"].annotations.open_world_hint is False
    search = by_name["search"].input_schema
    assert search["required"] == ["query"] and search["properties"]["limit"] == {
        "default": 10, "description": "How many documents to return.", "maximum": 100, "minimum": 1, "title": "Limit", "type": "integer",
    }  # fmt: skip


def test_read_only_leaves_out_the_tools_that_change_libraries(api: DocsAPI) -> None:
    listed = run(build(api.client(), read_only=True).list_tools())
    assert [tool.name for tool in listed] == READS


def test_the_formats_and_orders_offered_are_the_ones_the_command_has() -> None:
    assert get_args(ExportFormat) == EXPORT_FORMATS
    assert get_args(SortName) == tuple(SORTS)


def test_a_client_can_call_the_tools_over_the_protocol(api: DocsAPI) -> None:
    async def session() -> None:
        async with MCPClient(build(api.client())) as client:
            listed = await client.list_tools()
            assert len(listed.tools) == 14
            found = await client.call_tool("search", {"query": "Kewley 2006", "limit": 5})
            assert not found.is_error and found.structured_content["total"] == 1
            assert json.loads(found.content[0].text)["documents"][0]["bibcode"] == "2006MNRAS.372..961K"
            exported = await client.call_tool("export", {"bibcodes": ["2006MNRAS.372..961K"]})
            assert exported.content[0].text.startswith("@ARTICLE{2006MNRAS.372..961K,")
            # An error the caller can act on arrives with its message.
            missing = await client.call_tool("get_library", {"library": "Nope"})
            assert missing.is_error and "No library has the id or name 'Nope'" in missing.content[0].text
            # Arguments are checked against the schema before the tool runs.
            invalid = await client.call_tool("search", {"query": "Kewley", "limit": 1000})
            assert invalid.is_error and "less than or equal to 100" in invalid.content[0].text

    run(session())


def test_scix_mcp_serves_over_standard_input_and_output(tmp_path: Path) -> None:
    """The real thing: the command as a subprocess, spoken to as an MCP client would."""
    environment = {**os.environ, "SCIX_CACHE_DIR": os.environ["SCIX_CACHE_DIR"], "SCIX_API_TOKEN": "not-a-real-token"}
    server = StdioServerParameters(command=sys.executable, args=["-m", "scixplorer", "--mcp", "--read-only"], env=environment)

    async def session() -> None:
        async with MCPClient(server) as client:
            listed = await client.list_tools()
            assert [tool.name for tool in listed.tools] == READS
            # Two tools that need no request, so no network.
            explained = await client.call_tool("explain_search", {"query": "Kewley et al. 2019 ApJ exoplanets"})
            assert explained.structured_content["solr_query"] == 'first_author:"Kewley" AND year:2019 AND bibstem:"ApJ" AND uat_id:498'
            resolved = await client.call_tool("resolve", {"kind": "journal", "name": "Astrophysical Journal", "limit": 1})
            assert resolved.structured_content["matches"][0]["abbreviation"] == "ApJ"

    run(session())


# The command.


def test_the_command_runs_the_server(monkeypatch: pytest.MonkeyPatch, api: DocsAPI) -> None:
    served: list[tuple[Any, bool]] = []
    monkeypatch.setattr(mcp_server, "serve", lambda client, read_only: served.append((client, read_only)))
    client = api.client()
    assert main(["--mcp"], client=client, stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert main(["--mcp", "--read-only"], client=client, stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert served == [(client, False), (client, True)]


def test_read_only_goes_with_mcp(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["Kewley", "--read-only"])
    assert stopped.value.code == 2 and "--read-only goes with --mcp" in capsys.readouterr().err


def test_without_the_mcp_package_the_command_says_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "scixplorer.mcp_server", None)
    monkeypatch.delattr(scixplorer, "mcp_server")
    err = io.StringIO()
    assert main(["--mcp"], stdout=io.StringIO(), stderr=err) == 2
    assert "install scixplorer[mcp]" in err.getvalue()


def test_importing_the_package_or_the_command_does_not_import_mcp() -> None:
    import subprocess

    script = "import sys, scixplorer, scixplorer.cli; assert 'mcp' not in sys.modules and 'pydantic' not in sys.modules"
    assert subprocess.run([sys.executable, "-c", script]).returncode == 0
