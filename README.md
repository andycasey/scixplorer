# scixplorer

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
            (Author.name == "Hogg, D") & (Author.affiliation.country_code == "DE")
        )
        & Document.year.between(2010, 2020)
    )
    .order_by(Document.citation_count.desc())
    .limit(10)
)
for paper in papers:
    print(paper.year, paper.bibcode, paper.title[0])

print(export.bibtex(papers))
# -> @ARTICLE{2015ApJ...808...16N
```

This finds papers from 2010 to 2020 on which D. Hogg gave a German affiliation, and exports them as
BibTeX. `papers.explain()` shows the request without sending it.

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

## Documentation

The documentation is a [Hugo](https://gohugo.io) site in [`docs/`](docs/). To read it locally, install
Hugo (extended, 0.146 or later) and Go, then run:

```shell
hugo server --source docs
```

and open <http://localhost:1313/>. Every example in it is run by the test suite.

## Project status

Pre-release (1.0.0.dev0). The client is tested offline in detail, but it has **not yet been validated
against the live SciX API**; the opt-in live test suite (`uv run pytest --live`) does that.

## Licence

The code is MIT-licensed (see [LICENSE](LICENSE)). The reference data packaged with it is distributed
under its own terms (the affiliation data is GPL-3.0); see
[`src/scixplorer/data/NOTICE.md`](src/scixplorer/data/NOTICE.md).
