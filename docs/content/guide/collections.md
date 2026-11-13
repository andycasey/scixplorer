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
import scixplorer as scix

scix.config.collection = "astronomy"
scix.search.to_solr(scix.Document.year == 2020)
# -> year:2020 AND database:"astronomy"
```

Give a list for several collections:

```python
scix.config.collection = ["astronomy", "physics"]
scix.search.to_solr(scix.Document.year == 2020)
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
| names a collection itself | `scix.Document.database == "physics"`, `scix.Document.collection == "physics"`, or raw Solr containing `database:` or `collection:` | your own choice wins |
| asks for specific documents | a top-level `==` or `in_()` on `bibcode`, `scix_id`, `id`, `identifier`, `doi` or `alternate_bibcode` | you know which documents you want, whatever their collection |
| asks for a library's documents | a top-level `scix.Document.in_(library)` | the same |

```python
scix.config.collection = "astronomy"
scix.search.to_solr(scix.Document.bibcode == "2010GeoRL..3704201M")
# -> bibcode:"2010GeoRL..3704201M"
scix.search.to_solr(scix.Document.database == "earthscience")
# -> database:"earthscience"
```

"Top-level" means joined to the rest of the search with `&`. A bibcode inside `|` does not name the
documents of the whole search, so the collection still applies:

```python
scix.search.to_solr((scix.Document.bibcode == "2010GeoRL..3704201M") | (scix.Document.year == 2020))
# -> (bibcode:"2010GeoRL..3704201M" OR year:2020) AND database:"astronomy"
```

Because `scix.Document.get(bibcode=...)` and the requests that [lazy loading]({{< ref "guide/results#lazy-loading" >}})
sends name documents by identifier, they are never limited by the default collection.

The setting applies only to document searches. Journals, affiliations, vocabularies and libraries are not
affected.

`explain()` says whether the collection was applied, and why not:

```python
print(scix.Document.select().where(scix.Document.year == 2020).explain())
# -> limited to collection 'astronomy' (scixplorer.config.collection): database:"astronomy" is added
```

The collection does not count as narrowing a search for the purpose of the
[whole-index guard]({{< ref "guide/errors-and-limits#searches-that-would-download-the-whole-index" >}}):
a search that SciX would otherwise answer with every document is refused even with a collection set.
