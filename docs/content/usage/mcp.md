---
title: MCP server
weight: 17
description: Let an AI assistant search SciX, read documents, compute metrics and manage your libraries, through the Model Context Protocol server that scix --mcp runs.
---

After this page you can connect an AI assistant such as Claude to SciX through scixplorer, know which
tools it then has, and limit it to reading.

`scix --mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server: a program an
assistant starts and talks to, which gives it tools. The assistant can then search SciX the way the
[`scix` command]({{< ref "usage/command-line" >}}) does, in plain words, and is told how each word was read.
Journals, institutions and Unified Astronomy Thesaurus concepts are resolved from the
[reference data]({{< ref "usage/reference-data" >}}) on your machine, so the assistant does not have to
guess that The Astrophysical Journal is `ApJ` or that hot Jupiters are concept 753.

## Install

The server needs the `mcp` package, which is not installed with scixplorer. Ask for it by name:

```shell
uv tool install "scixplorer[mcp]"
```

or, with pip, `pip install "scixplorer[mcp]"`. Without installing anything first,
`uvx --from "scixplorer[mcp]" scix --mcp` downloads and runs it.

The server uses the same [API token]({{< ref "getting-started/token-management" >}}) as the rest of
scixplorer. An application you start from the desktop does not see the environment variables of your
shell, so for those put the token in the file `~/.scix/token`, or give it in the application's
configuration as below.

## Connect an assistant

For Claude Code:

```shell
claude mcp add scix -- uvx --from "scixplorer[mcp]" scix --mcp
```

For Claude Desktop and other applications configured with a JSON file, add the server to `mcpServers`:

```json
{
  "mcpServers": {
    "scix": {
      "command": "uvx",
      "args": ["--from", "scixplorer[mcp]", "scix", "--mcp"],
      "env": {"SCIX_API_TOKEN": "your-token"}
    }
  }
}
```

Leave out `env` if your token is in `~/.scix/token`. The server talks over standard input and output, and
runs until the assistant closes it; there is nothing to see if you run `scix --mcp` in a terminal yourself.

Then ask in your own words:

```text
Find the most cited refereed papers on hot Jupiters since 2020.
What is the h-index of the papers in my library "Emission-line galaxies"?
Add the papers that cite 2019ApJ...880...16K to a new library called "ISM pressure".
```

## The tools

| Tool | What it does |
|---|---|
| `search` | search in plain words or with named fields; returns how the query was read, the Solr query sent, the total, and the documents |
| `explain_search` | show how a query would be read and the Solr query it becomes, without searching |
| `get_document` | one document in full, by bibcode, DOI or arXiv identifier: authors, abstract, keywords, links |
| `resolve` | find the identifier of a journal, institution, UAT concept or planetary feature by name, without a request |
| `export` | BibTeX, AASTeX, RIS, EndNote and the other formats of the [export service]({{< ref "usage/services" >}}) |
| `metrics` | [citation metrics]({{< ref "usage/services#metrics" >}}) for a list of bibcodes, or for the results of a search |
| `list_libraries`, `get_library` | your [libraries]({{< ref "usage/libraries" >}}), and the documents in one |
| `create_library`, `add_to_library`, `combine_libraries` | make a library, add documents (bibcodes, or the results of a search), or make a new library from the union, intersection or difference of others |
| `edit_library`, `remove_from_library`, `delete_library` | rename or describe a library or make it public, take documents out, or delete it |

`search` reads a query by [the rules of the command]({{< ref "usage/command-line#how-words-are-read" >}}),
and its description tells the assistant those rules. If a search matches nothing it is widened once, and
the result says so. The search operators are part of the query: `citations:2019ApJ...880...16K` finds the
documents that cite one, and `references:`, `similar:`, `trending:`, `reviews:` and `useful:` work the same
way.

The tools that take bibcodes accept up to 2000. Where a tool takes a `query` instead, the first results of
that search are used.

## Limit it to reading

The last six tools change your SciX account. Each is marked for the assistant as one that changes things,
and `delete_library` as one that cannot be undone, so an application that asks before such a call will
ask. To leave them out altogether, add `--read-only`:

```shell
claude mcp add scix -- uvx --from "scixplorer[mcp]" scix --mcp --read-only
```

The assistant then has the first eight tools only. Sharing a library, changing who may use it and
transferring it to someone else are not offered as tools at all: do those with
[`Library`]({{< ref "usage/libraries" >}}) or on the SciX website.

## Concepts need the thesaurus

`search` and `resolve` recognise UAT concepts and planetary features once their data is downloaded. Run
`scix --refresh-data` once; see [Reference data]({{< ref "usage/reference-data" >}}). Until then `search`
looks for topics as words and says so in its result, and `resolve` says what to run.

## The SciX team's server

SciX publishes its own MCP server, [scix-mcp](https://github.com/adsabs/scix-mcp), written in TypeScript. It
covers more of the API: facets, library permissions and notes, and a search of the SciX help pages. Its
search takes Solr syntax as written. scixplorer's server is smaller, and adds the reading of plain words
and the resolving of names described above. The two can be installed side by side; give them different
names in your assistant's configuration.
