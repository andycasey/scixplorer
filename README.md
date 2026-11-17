# scixplorer

[![Documentation](https://github.com/andycasey/scixplorer/actions/workflows/docs.yml/badge.svg?branch=main)](https://andycasey.github.io/scixplorer/)
[![Tests](https://github.com/andycasey/scixplorer/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/andycasey/scixplorer/actions/workflows/tests.yml)
[![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fandycasey.github.io%2Fscixplorer%2Fcoverage%2Fbadge.json)](https://andycasey.github.io/scixplorer/coverage/)
[![PyPI](https://img.shields.io/pypi/v/scixplorer)](https://pypi.org/project/scixplorer/)

A Python client for [SciX](https://scixplorer.org) (Science Explorer), the successor to the NASA
Astrophysics Data System. You write searches as Python expressions; scixplorer checks them, sends the
Solr query SciX expects, and returns typed results.

## Install

scixplorer needs Python 3.10 or later; its only dependency is `httpx`. We recommend
[uv](https://docs.astral.sh/uv/):

```shell
uv add scixplorer
```

or, with pip, `pip install scixplorer`.

Get an API token at <https://scixplorer.org/user/settings/token> and put it in the environment variable
`SCIX_API_TOKEN`, or on the first line of `~/.scix/token`.

> [!NOTE]
> **Coming from ADS?** You don't need to change anything. scixplorer picks up your existing API key from
> wherever the `ads` package read it: the environment variable `ADS_API_TOKEN` or `ADS_DEV_KEY`, or the
> file `~/.ads/token` or `~/.ads/dev_key`.

## Example

```python
from scixplorer import Author, Document, export

papers = (
    Document
    .select(
        Document.bibcode,
        Document.title,
        Document.year,
    )
    .where(
        Document.authors.any(
            (Author.name == "Kauffmann, G") & (Author.affiliation.country_code == "DE")
        )
        & Document.year.between(2000, 2010)
    )
    .order_by(Document.citation_count.desc())
    .limit(10)
)
for paper in papers:
    print(paper.year, paper.bibcode, paper.title[0])

print(export.bibtex(papers))
# -> @ARTICLE{2006MNRAS.372..961K
```

This finds papers from 2000 to 2010 on which G. Kauffmann gave a German affiliation, and exports them as
BibTeX. `papers.explain()` shows the request without sending it.

Coming from the `ads` package? This is how a search changes
([more in the documentation](https://andycasey.github.io/scixplorer/getting-started/coming-from-ads/)):

```python
# With ads:
# papers = ads.SearchQuery(q='author:"Kewley, L" year:2008')

# The simplest change: keep your query string.
papers = (
    Document
    .select()
    .where(Document.raw('author:"Kewley, L" year:2008'))
)

# The recommended change: write the search in Python.
papers = (
    Document
    .select()
    .where((Document.author == "Kewley, L") & (Document.year == 2008))
)
```

## From the terminal

Installing scixplorer also installs the command `scix`, which reads plain words:

```shell
scix Casey 2005            # an author and a year
scix exoplanets 2026       # a Unified Astronomy Thesaurus concept and a year
scix ApJ 608 119           # a journal, a volume and a page
scix Casey et al. 2019 ApJ --bibtex >> references.bib
```

In a terminal the results open in a browser you move through with the keyboard: read abstracts, follow
citations and references, open the full text, copy BibTeX. `scix --explain <words>` shows how a search
is read without sending it. See
[the documentation](https://andycasey.github.io/scixplorer/usage/command-line/).

## Features

- Searches as Python expressions on 101 typed document fields, checked when you write them.
- Several conditions about the same author (`Document.authors.any(...)`), or the author at a
  position.
- Search by journal title, institution, country, Unified Astronomy Thesaurus concept or planetary
  feature: resolved locally against packaged or downloaded reference data.
- Your SciX libraries: list, create, change, combine and share them, and search within them.
- Citation and reference networks and SciX's second-order operators (`trending`, `useful`, `reviews`,
  `similar`, `top_n`).
- Export in BibTeX and every other format SciX offers; resolver links; network data; citation
  suggestions.
- Synchronous and `asyncio` use with the same queries; retries, rate-limit tracking, and paging handled
  for you.
- A command, `scix`, that searches from the terminal in plain words.
