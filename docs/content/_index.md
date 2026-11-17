---
title: scixplorer
weight: 1
description: A Python client for SciX (Science Explorer), the successor to NASA/ADS, with searches written as Python expressions.
---

scixplorer is a Python client for [SciX](https://scixplorer.org) (Science Explorer), the successor to the
NASA Astrophysics Data System (ADS). You write searches as Python expressions on typed model classes, and
scixplorer checks them as you write them, turns them into the Solr query that SciX expects, and gives you
typed results.

```python
from scixplorer import *

papers = (
    Document
    .select(
        Document.bibcode,
        Document.title,
    )
    .where((Document.author[0] == "Ness, M") & (Document.year >= 2015))
    .order_by(Document.citation_count.desc())
    .limit(5)
)
[paper.title[0] for paper in papers]
# -> The Cannon: A data-driven approach to Stellar Label Determination from Spectra
```

`papers.explain()` shows the request that this sends, without sending it:

```python
print(papers.explain())
# -> q = pos(author:"Ness, M", 1) AND year:[2015 TO *]
# -> sort = citation_count desc,id asc
```

## Features

{{< hextra/feature-grid cols="3" >}}
  {{< hextra/feature-card icon="code" title="Python queries" link="getting-started/first-search/" subtitle="Build queries with `select`, `where`, `order_by` and `limit`, combine conditions with `&`, `|` and `~`, and see the exact request with `explain()`. Raw Solr is there when you need it." >}}
  {{< hextra/feature-card icon="badge-check" title="Typed fields" link="guide/results/" subtitle="Every field is a typed attribute. Your editor completes field names and the operations each field allows, and values arrive as `int`, `date`, `list[str]` and so on." >}}
  {{< hextra/feature-card icon="office-building" title="Affiliations" link="guide/affiliations/" subtitle="Search by institution or country with SciX's curated affiliations: 7382 institutions, with their hierarchy, packaged with scixplorer." >}}
  {{< hextra/feature-card icon="user-group" title="Per-author search" link="guide/authors/" subtitle="Say several things about the same author (a name and an affiliation), about the first author, or about every author." >}}
  {{< hextra/feature-card icon="newspaper" title="Journals" link="guide/journals/" subtitle="Search by journal title instead of looking up abbreviations: a condition on `Journal.title` becomes a search on the matching bibstems." >}}
  {{< hextra/feature-card icon="globe-alt" title="Vocabularies" link="guide/vocabularies/" subtitle="Search and read the Unified Astronomy Thesaurus concepts and planetary features assigned to each document, including narrower concepts." >}}
  {{< hextra/feature-card icon="share" title="Citations" link="guide/operators/" subtitle="Follow citations and references, and ask SciX for trending, useful, review and similar papers on a topic." >}}
  {{< hextra/feature-card icon="lightning-bolt" title="Async" link="guide/async/" subtitle="Run any query from `asyncio` code with `async for`, `aall()` and `acount()`, and send several at once." >}}
  {{< hextra/feature-card icon="collection" title="Libraries" link="guide/libraries/" subtitle="Create, change, share and combine your SciX libraries, and search within them." >}}
  {{< hextra/feature-card icon="document-download" title="Exports" link="guide/services/" subtitle="Export BibTeX, AASTeX, RIS and the other SciX formats, find full text and data links, and get citation suggestions." >}}
  {{< hextra/feature-card icon="shield-check" title="Robust" link="guide/errors-and-limits/" subtitle="Paging, retries, rate-limit tracking and long bibcode lists are handled for you, and every error derives from one exception class." >}}
  {{< hextra/feature-card icon="switch-horizontal" title="From ads" link="getting-started/coming-from-ads/" subtitle="The same query shape as the `ads` package, with a list of every change that could alter a result." >}}
{{< /hextra/feature-grid >}}

### Typed fields

Each field is a typed attribute of its model, so your editor completes `Document.` with the 101 document
fields and offers only the operations a field supports. A type checker such as mypy or pyright flags
`Document.year.like("19*")` or a misspelt field name before the code runs, and knows the type of every
value you read:

```python
paper = Document.get(bibcode="1995Natur.378..355M")
paper.pubdate
# -> datetime.date(1995, 11, 1)
paper.journal
# -> <Journal: abbreviation='Natur'>
```

Values are checked when you build a condition, not when SciX rejects the search:

<!-- docs-test: raises QueryError -->
```python
Document.year == "2015"
```

This raises `QueryError` with the message `Document.year needs an int, not '2015'`.

### Institutions and countries

SciX assigns curated identifiers to the institutions in authors' affiliations. scixplorer packages that
data, so you can search by country or institution without looking up an identifier:

```python
australian = (
    Document
    .select(Document.bibcode)
    .where(Document.affiliation.country_code == "AU")
)
[paper.bibcode for paper in australian]
# -> ['1998AJ....116.1009R']
```

### Several conditions on the same author

Everything inside one `Document.authors.any(...)` is about one author. This finds papers on which Hogg, D lists
a German institution, not papers by Hogg, D with some co-author in Germany:

```python
hogg_in_germany = (
    Document
    .select(Document.bibcode)
    .where(
        Document.authors.any(
            (Author.name == "Hogg, D") & (Author.affiliation.country_code == "DE")
        )
    )
)
[paper.bibcode for paper in hogg_in_germany]
# -> ['2015ApJ...808...16N']
```

### Citation networks

Operators such as `citations`, `references`, `trending` and `similar` take a condition and return one, so
they combine with everything else:

```python
emcee = Document.bibcode == "2013PASP..125..306F"
citing = (
    Document
    .select(Document.bibcode)
    .where(Document.citations(emcee))
)
[paper.bibcode for paper in citing]
# -> ['2015ApJ...808...16N', '2016arXiv160303040C']
```

### Async

Every way of running a query has an asynchronous form, so you can send several searches at once:

```python
import asyncio


async def counts(years):
    queries = [
        Document
        .select()
        .where(Document.year == year)
        for year in years
    ]
    return await asyncio.gather(*(query.acount() for query in queries))


asyncio.run(counts([1995, 1998, 2015]))
# -> [1, 1, 1]
```

## Install

scixplorer needs Python 3.10 or later, and its only dependency is `httpx`. It is not yet published on
PyPI, so install it from a copy of the source:

```shell
pip install /path/to/scixplorer
```

Then [get an API token]({{< ref "getting-started/install" >}}) and run your
[first search]({{< ref "getting-started/first-search" >}}).

## Where to go next

| If you want to | Read |
|---|---|
| Install, set your token and search | [Getting started]({{< ref "getting-started" >}}) |
| Move code from the `ads` package | [Coming from `ads`]({{< ref "getting-started/coming-from-ads" >}}) |
| Build searches and read results | [Guide]({{< ref "guide" >}}) |
| Look up a field, model or function | [Reference]({{< ref "reference" >}}) |
| Work on scixplorer itself | [Development]({{< ref "development" >}}) |

## Project status

scixplorer is pre-release software (version 1.0.0.dev0).

- **It has not yet been run against the live SciX API.** Every part of it is tested offline against
  recorded and simulated responses, but no valid API token was available while it was written. Some
  behaviour rests on assumptions about the server that only the
  [live test suite]({{< ref "development/live-tests" >}}) can confirm. Where a documented behaviour depends on
  one of those assumptions in a way you could trip over, the page says so.
- **The outputs on this site come from a sample.** Every example on these pages is run by the test suite
  against a small offline set of real papers. Counts, citation numbers, affiliations and other values in
  the outputs are illustrative, not current SciX data.
- The package is MIT-licensed. The reference data packaged with it is not: see
  [Reference data]({{< ref "guide/reference-data" >}}).
