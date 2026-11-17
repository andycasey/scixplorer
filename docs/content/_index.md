---
title: scixplorer
weight: 1
description: A Python client for SciX (Science Explorer), the successor to NASA/ADS, with searches written as Python expressions.
---

scixplorer is a Python client for [SciX](https://scixplorer.org) (Science Explorer), the successor to the
NASA Astrophysics Data System (ADS). You write searches as Python expressions on model classes. The client
checks them when you build them, turns them into the Solr query that SciX expects, and gives you typed
results. It also reads and writes your SciX libraries, exports citations, and knows the journals,
institutions and controlled vocabularies that SciX uses.

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
