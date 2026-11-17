---
title: Collections
weight: 10
description: Limit open searches to one or more SciX collections (disciplines), and know exactly when that limit is and is not applied.
---

After this page you can make your searches stay within a SciX collection, such as astronomy, without adding
the condition every time, and you will know which searches it leaves alone.

SciX covers several disciplines, which it calls collections (the `database` field): `astronomy`,
`physics`, `earthscience` and others. By default scixplorer searches all of them.

## Set a default collection

```python
from scixplorer import Document, config, search

config.collection = "astronomy"
search.to_solr(Document.year == 2020)
# -> year:2020 AND database:"astronomy"
```

Give a list for several collections:

```python
config.collection = ["astronomy", "physics"]
search.to_solr(Document.year == 2020)
# -> year:2020 AND database:("astronomy" OR "physics")
```

`None` (the default), or a blank name, means all collections. Names are not checked against the
collections SciX has. A value that is neither a string nor a sequence of strings raises `TypeError` when a
search is planned.

## When it is applied

The collection is added as a final top-level "and" condition to every open document search: one that does
not already say which collection it wants, or which documents it wants. It is not applied when the search:

| The search... | Example | Because |
|---|---|---|
| names a collection itself | `Document.database == "physics"`, `Document.collection == "physics"`, or raw Solr containing `database:` or `collection:` | your own choice wins |
| asks for specific documents | a top-level `==` or `in_()` on `bibcode`, `scix_id`, `id`, `identifier`, `doi` or `alternate_bibcode` | you know which documents you want, whatever their collection |
| asks for a library's documents | a top-level `Document.in_(library)` | the same |

```python
config.collection = "astronomy"
search.to_solr(Document.bibcode == "2010GeoRL..3704201M")
# -> bibcode:"2010GeoRL..3704201M"
search.to_solr(Document.database == "earthscience")
# -> database:"earthscience"
```

"Top-level" means joined to the rest of the search with `&`. A bibcode inside `|` does not name the
documents of the whole search, so the collection still applies:

```python
search.to_solr((Document.bibcode == "2010GeoRL..3704201M") | (Document.year == 2020))
# -> (bibcode:"2010GeoRL..3704201M" OR year:2020) AND database:"astronomy"
```

Because `Document.get(bibcode=...)` and the requests that [lazy loading]({{< ref "usage/results#lazy-loading" >}})
sends name documents by identifier, they are never limited by the default collection.

The setting applies only to document searches. Journals, affiliations, vocabularies and libraries are not
affected.

`explain()` says whether the collection was applied, and why not:

```python
query = (
    Document
    .select()
    .where(Document.year == 2020)
)
print(query.explain())
# -> limited to collection 'astronomy' (scixplorer.config.collection): database:"astronomy" is added
```

The collection does not count as narrowing a search for the purpose of the
[whole-index guard]({{< ref "usage/errors-and-limits#searches-that-would-download-the-whole-index" >}}):
a search that SciX would otherwise answer with every document is refused even with a collection set.
