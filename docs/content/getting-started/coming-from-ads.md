---
title: Coming from ads
weight: 3
description: What changed for users of the old ads package, and how to update code written for it.
---

After this page you can move code written for the `ads` package (version 1, and the older 0.12) to
scixplorer, and you will know every change that could silently alter a result.

## At a glance

| In `ads` | In scixplorer |
|---|---|
| `import ads`, `from ads import Document` | `import scixplorer as scix`, then `scix.Document` |
| `ads.Document.select().where(...)` | `scix.Document.select().where(...)`: the same shape |
| `Document.get(Document.bibcode == "...")` | `scix.Document.get(bibcode="...")` (the expression form also works) |
| `ads.SearchQuery(q="...", fl=[...])` | `scix.SearchQuery(q="...", fl=[...])`, which returns an ordinary query |
| `not (Document.year == 2020)` | `~(scix.Document.year == 2020)` |
| `Document.title.like("JWST")` (prefix match) | `scix.Document.title.like("JWST*")` or `scix.Document.title.startswith("JWST")` |
| `Library(name=...)` then `library.save()` | `scix.Library.create(name=...)` |
| `library += documents`, `library.save()` | `library.add(*documents)` |
| `library.name = "New"`, `library.save()` | `library.update(name="New")` |
| `ads-setup` after installing | nothing: the data is packaged, or downloaded on request |
| `from ads.services import export` | `scix.export` |
| the exceptions in `ads.exceptions` | `scix.exceptions.SciXError` and its subclasses |

## Breaking changes

### The package is `scixplorer`

The package and its import name are `scixplorer`. These pages import it as `scix`:

```python
import scixplorer as scix
```

The default API host is `https://api.scixplorer.org/v1` (it was `https://api.adsabs.harvard.edu/v1`). It is
`scix.config.api_url`, and you can change it:

```python
scix.config.api_url
# -> https://api.scixplorer.org/v1
```

Your token is still found where `ads` looked for it (`ADS_API_TOKEN`, `ADS_DEV_KEY`, `~/.ads/token`,
`~/.ads/dev_key`), after the new places `SCIX_API_TOKEN` and `~/.scix/token`. See
[Install and set your token]({{< ref "getting-started/install" >}}).

### There is no ORM and no `save()`

`ads` was built on the peewee ORM. scixplorer has its own small expression language and no ORM, so
peewee's methods and its `ModelSelect` are gone. Queries are `scixplorer.query.Query` objects with
`where`, `order_by`, `limit`, `offset`, `count`, `first`, `get`, `exists`, `all` and `explain`.

`Library` has no `save()`, and nothing is saved implicitly. Every change is an explicit method that sends one
request: `create`, `update`, `add`, `remove`, `empty`, `delete`, `union`, `intersection`, `difference`,
`copy_to`, `set_permissions` and `transfer`. Assigning to an attribute changes only the local object. See
[Libraries]({{< ref "guide/libraries" >}}).

```python
library = scix.Library.get(name="Data-driven spectroscopy")
library.add("2013PASP..125..306F")
# -> 1
library.update(description="The Cannon and the tools it uses")
```

### `not`, `and`, `or` and `in` raise an error

Python cannot overload these keywords, so they never worked in queries, and in `ads` a mistake could
fail silently. In scixplorer an expression has no truth value, so they raise a `TypeError` that says what
to write instead:

<!-- docs-test: raises TypeError -->
```python
not (scix.Document.year == 2020)
```

Write `~` for not, `&` for and, `|` for or, and `.in_()` and `.not_in()` for membership. For "not one of
these documents", write `scix.Document.not_in(...)`:

```python
recent = scix.Document.year >= 2015
scix.search.to_solr(recent & scix.Document.not_in(["2015ApJ...808...16N"]))
# -> year:[2015 TO *] AND -bibcode:"2015ApJ...808...16N"
```

### `.like()` no longer adds a wildcard

In `ads`, `Document.title.like("JWST")` became the prefix search `title:JWST*`. In scixplorer the pattern
is used as written. Add `*` yourself, or use `startswith()`:

```python
scix.search.to_solr(scix.Document.title.like("JWST"))
# -> title:JWST
scix.search.to_solr(scix.Document.title.like("JWST*"))
# -> title:JWST*
scix.search.to_solr(scix.Document.title.startswith("JWST"))
# -> title:JWST*
```

### Slices are Python slices

`scix.Document.author[0]` is the first author and `scix.Document.author[0:3]` the first three, as for a
Python list. Check any slice carried over from `ads`, whose positions were converted differently.

```python
scix.search.to_solr(scix.Document.author[0:3] == "Ness, M")
# -> pos(author:"Ness, M", 1, 3)
```

Negative and open-ended slices raise `QueryError`, because SciX cannot search by position from the end.

### Field types changed

Field values now have exactly the type the field declares, or `None`. Several fields changed type:

| Field | In `ads` | In scixplorer |
|---|---|---|
| `id` | `int` | `str` of digits. Queries accept an `int`: `scix.Document.id == 1406395` |
| `volume` | `int` | `str`, because some volumes are not numbers (`conf` in `2020svos.conf...11E`) |
| `cite_read_boost` | `int` | `float` |
| `keyword`, `alternate_title`, `arxiv_class`, `bibgroup`, `data`, `grant`, `isbn`, `links_data`, `vizier` | `str` | a list of `str` |
| `body`, `ack` | search-only | readable, but [deferred]({{< ref "guide/results#deferred-fields" >}}) |
| `uat`, `planetary_feature` | text | relations to [`UATConcept` and `PlanetaryFeature`]({{< ref "guide/vocabularies" >}}); the text is now `uat_name` and `planetary_feature_name` |

```python
scix.search.to_solr(scix.Document.id == 1406395)
# -> id:"1406395"
paper = scix.Document.get(id=1406395)
paper.id
# -> '1406395'
paper.volume
# -> '378'
paper.keyword
# -> ['Planets', 'Stars']
```

A value from SciX that cannot be converted becomes `None` with a `DataWarning`, instead of passing
through with the wrong type. See [Results]({{< ref "guide/results#types-and-datawarning" >}}).

### Large fields are deferred

`scix.Document.select()` with no fields fetches every field except `ack`, `body`, `caption`, `citation`,
`credit`, `mention` and `reference`. Those can be very large (full text, or tens of thousands of
bibcodes). Select them by name to fetch them; reading one you did not select loads it on demand.

### Lazy loading is one request per page

Reading a field you did not select still loads it, with a `LazyLoadWarning`. In `ads` that cost one
request per field per document. In scixplorer one request loads the field for every document of the
same page of results, so reading `doc.citation_count` in a loop over 200 documents sends one request, not
200. See [Results]({{< ref "guide/results#lazy-loading" >}}).

### Affiliation countries are spelled out

`scix.Affiliation.country` holds the country as the upstream data spells it, such as `"United States"`
or `"United Kingdom"`. A condition such as `country == "USA"` matches nothing. The new field
`country_code` holds the ISO 3166-1 code, which is the stable way to filter:

```python
scix.Affiliation.get(id="A00951").country
# -> United States
scix.Affiliation.get(id="A00951").country_code
# -> US
```

### No local database and no `ads-setup`

`ads` built a SQLite database of journals and affiliations with the `ads-setup` command. scixplorer
reads that data from files packaged with it, held in memory. Two vocabularies, the Unified Astronomy
Thesaurus and the planetary nomenclature gazetteer, are not packaged: download them once with
`scix.data.refresh()`. Nothing is downloaded unless you ask. See
[Reference data]({{< ref "guide/reference-data" >}}).

### One base exception

Every exception the package raises derives from `scix.exceptions.SciXError`. Errors from the API are
`APIError`, with the subclasses `AuthenticationError` and `RateLimitExceeded`. See
[Errors and limits]({{< ref "guide/errors-and-limits" >}}).

```python
issubclass(scix.exceptions.RateLimitExceeded, scix.exceptions.SciXError)
# -> True
```

## What stayed the same

- Searches start with `select()` on the document model (`scix.Document.select()`) and use `where`,
  `order_by` and `limit`.
- `&`, `|` and `~` combine conditions, and each comparison needs its own parentheses.
- `scix.Document.journal`, `scix.Document.affiliation` and the operators such as
  `scix.Document.citations()` and `scix.Document.top_n()` work as before, except that an operator takes a
  condition, not a document: `scix.Document.citations(scix.Document.bibcode == paper.bibcode)`.
- `SearchQuery` still exists for Solr query strings you already have. It returns an ordinary query:

```python
query = scix.SearchQuery(q='author:"Mayor, M" year:[1990 TO 2000]', fl="bibcode,title", rows=10)
[paper.bibcode for paper in query]
# -> 1995Natur.378..355M
```

- `scix.export.bibtex(...)` and the other export formats are functions, as in `ads.services.export`. See
  [Services]({{< ref "guide/services" >}}).
