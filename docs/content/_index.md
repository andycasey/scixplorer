---
title: scixplorer
weight: 1
layout: hextra-home
description: A typed Python client for SciX (Science Explorer), the successor to NASA/ADS, with searches written as Python expressions.
---

<div class="landing">

<div class="landing-hero">
<div class="landing-hero-text">

{{< hextra/hero-badge >}}Version 1.0.0{{< /hextra/hero-badge >}}

{{< hextra/hero-headline >}}Search SciX with Python{{< /hextra/hero-headline >}}

{{< hextra/hero-subtitle >}}A typed client for [SciX](https://scixplorer.org), the successor to NASA/ADS. Write searches as Python expressions: scixplorer checks them as you write them, sends the Solr query SciX expects, and gives back typed results.{{< /hextra/hero-subtitle >}}

<div class="landing-buttons">
{{< hextra/hero-button text="Get started" link="getting-started/install/" >}}
{{< hextra/hero-button text="Coming from `ads`" link="getting-started/coming-from-ads/" >}}
</div>

</div>
<div>

```python
from scixplorer import Document

papers = (
    Document
    .select(
        Document.bibcode,
        Document.title,
    )
    .where((Document.author[0] == "Kewley, L") & (Document.year >= 2005))
    .order_by(Document.citation_count.desc())
    .limit(5)
)
[paper.title[0] for paper in papers]
# -> The host galaxies and classification of active galactic nuclei
```

</div>
</div>

<div class="landing-row">
<div class="content">

### Queries are Python expressions

Build a query from a model with `select`, `where`, `order_by` and `limit`, and combine conditions with
`&`, `|` and `~`. Nothing is sent until you read the results, and `explain()` shows exactly what would be
sent. Raw Solr syntax is there when you need it.

[Your first search →]({{< ref "getting-started/first-search" >}})

</div>
<div>

```python
print(papers.explain())
# -> q = pos(author:"Kewley, L", 1) AND year:[2005 TO *]
# -> sort = citation_count desc,id asc
```

</div>
</div>

<div class="landing-row">
<div class="content">

### Typed fields, with autocomplete

Every field is a typed attribute, so your editor completes `Document.` with all 101 document fields and
offers only the operations each one supports. Type checkers such as mypy and pyright know the type of every
value, and flag a misspelt field or `Document.year.like(...)` before the code runs. Values are checked as
you build a condition: `Document.year == "2015"` raises `QueryError` at once.

[Results and types →]({{< ref "usage/results" >}})

</div>
<div>

```python
paper = Document.get(bibcode="2000ApJ...537..916S")
paper.pubdate
# -> datetime.date(2000, 7, 1)
paper.journal
# -> <Journal: abbreviation='ApJ'>
```

<!-- docs-test: raises QueryError -->
```python
Document.year == "2015"
```

</div>
</div>

<div class="landing-row">
<div class="content">

### Search by institution and country

SciX identifies the institutions in authors' affiliations. scixplorer packages that curated data (7382
institutions, with their hierarchy), so you can search by country or institution without looking up an
identifier, and with no request until the search itself.

[Affiliations →]({{< ref "usage/affiliations" >}})

</div>
<div>

```python
australian = (
    Document
    .select(Document.bibcode)
    .where(Document.affiliation.country_code == "AU")
)
[paper.bibcode for paper in australian]
# -> ['2001ApJ...556..121K', '2001ApJ...553...47F']
```

</div>
</div>

<div class="landing-row">
<div class="content">

### Several conditions on the same author

Everything inside one `Document.authors.any(...)` is about one author. This finds papers on which G. Kauffmann
lists a German institution, not papers by G. Kauffmann with some co-author in Germany. You can also ask about
the first author, or about every author.

[Authors →]({{< ref "usage/authors" >}})

</div>
<div>

```python
from scixplorer import Author

kauffmann_in_germany = (
    Document
    .select(Document.bibcode)
    .where(
        Document.authors.any(
            (Author.name == "Kauffmann, G") & (Author.affiliation.country_code == "DE")
        )
    )
)
[paper.bibcode for paper in kauffmann_in_germany]
# -> ['2006MNRAS.372..961K']
```

</div>
</div>

<div class="landing-row">
<div class="content">

### Async

Every way of running a query has an asynchronous form (`async for`, `aall()`, `acount()` and the rest),
so you can send several searches at once from `asyncio` code.

[Async →]({{< ref "usage/async" >}})

</div>
<div>

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


asyncio.run(counts([2000, 2003, 2015]))
# -> [1, 1, 1]
```

</div>
</div>

<div class="landing-section-title">

{{< hextra/hero-section >}}Also in scixplorer{{< /hextra/hero-section >}}

</div>

<div class="landing-more">

{{< hextra/feature-grid cols="3" >}}
  {{< hextra/feature-card icon="share" title="Citation networks" link="usage/operators/" subtitle="Citations, references, and trending, useful, review and similar papers." >}}
  {{< hextra/feature-card icon="globe-alt" title="Journals and vocabularies" link="usage/vocabularies/" subtitle="Search by journal title, UAT concept or planetary feature." >}}
  {{< hextra/feature-card icon="collection" title="Libraries and exports" link="usage/libraries/" subtitle="Manage your SciX libraries, and export BibTeX and other formats." >}}
{{< /hextra/feature-grid >}}

</div>

<div class="landing-footer content">

## Install

scixplorer needs Python 3.10 or later, and its only dependency is `httpx`. We recommend
[uv](https://docs.astral.sh/uv/):

```shell
uv add scixplorer
```

or, with pip, `pip install scixplorer`. Then [get an API token]({{< ref "getting-started/install#set-your-api-token" >}})
and run your [first search]({{< ref "getting-started/first-search" >}}).

</div>

<div class="landing-thanks content">

## A community project, with thanks

scixplorer is a community project, and it stands on the work of the NASA Astrophysics Data System team,
now the [SciX](https://scixplorer.org) team. For more than thirty years they have gathered, curated and
linked the scientific literature, and kept it open to everyone, so that any of us can find a paper,
everything it cites and everything that cites it, in seconds. It is easy to forget how remarkable that is.

The index, the API, the curated affiliations and the links between papers are all their work. scixplorer
only tries to make that work a little easier to reach from Python. If it helps your research, the credit
belongs to them.

The best place to give that credit is in your papers. If SciX helped your research, through scixplorer or
any other way, please say so in your acknowledgements. It is how the team can show what their work makes
possible. They suggest this wording:

> This research has made use of SciX, a scientific literature search and discovery platform, funded by
> NASA under Cooperative Agreement 80NSSC21M0056.

Thank you ❤️

</div>

</div>
