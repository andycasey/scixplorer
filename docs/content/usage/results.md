---
title: Results
weight: 4
description: Choose which fields to fetch, read typed values, understand lazy and deferred loading, and know how documents are identified.
---

After this page you can choose the fields a search fetches, rely on the type of every value, avoid
unnecessary requests from lazy loading, and know when two documents are the same.

## Fields and their types

`Document` has 101 fields. Every value you read has the type its field declares, or is `None` when
SciX has no value:

| Field class | Python type | Examples |
|---|---|---|
| keyword, text and name fields | `str` | `bibcode`, `doctype`, `abstract`, `first_author` |
| integer fields | `int` | `year`, `citation_count`, `author_count` |
| floating-point fields | `float` | `citation_count_norm`, `score` |
| `pubdate` | `datetime.date` | the day is 1 when SciX gives only a month |
| `date`, `entry_date`, `indexstamp` | `datetime.datetime` | always timezone-aware, in UTC |
| multi-valued fields | a `list` of the above | `author`, `title`, `keyword`, `doi`, `uat_id` |

`id` and `volume` are strings, because not every value is a number. The
[field reference]({{< ref "reference/document-fields" >}}) lists every field with its type.

```python
from scixplorer import Document, exceptions, utils

paper = Document.get(bibcode="2000ApJ...529L..45C")
paper.title
# -> ['Detection of Planetary Transits Across a Sun-like Star']
paper.volume
# -> '529'
paper.entry_date
# -> datetime.datetime(2000, 1, 20, 0, 0, tzinfo=datetime.timezone.utc)
paper.abstract is None
# -> True
```

### Types and DataWarning

Values from SciX are converted to the field's type. Conversions that lose nothing happen silently: an
integer sent as `12.0` or `"12"`, a number sent for a text field, `"true"` or `"false"` for a boolean,
a single value for a multi-valued field (it becomes a one-element list), or a one-element list for a
single-valued field. Anything else becomes `None`, with a `exceptions.DataWarning` naming the field
and the value. A value never has a type other than the declared one.

To make such values stop your program instead, turn the warning into an error:

```python
import warnings

warnings.simplefilter("error", exceptions.DataWarning)
```

## Choose the fields

`select()` with no arguments fetches every field except the deferred ones. Name fields to fetch only
those. The identifiers `bibcode`, `id` and `scix_id` are always fetched as well:

```python
query = (
    Document
    .select(
        Document.bibcode,
        Document.title,
    )
    .where(Document.author == "Hogg, D")
)
query.first().to_dict()
# -> 'title': ['The Cannon: A data-driven approach to Stellar Label Determination from Spectra']
```

Fewer fields mean smaller responses. Choose them when you know what you will read.

### Deferred fields

Seven fields are left out of a plain `select()` because they can be very large: `ack`, `body`
(full text), `caption`, `citation`, `credit`, `mention` and `reference` (lists that can hold tens of
thousands of bibcodes). Name them in `select()` to fetch them. Reading one that was not fetched loads it on
demand, as described next.

### Lazy loading

Reading a field that the search did not fetch sends one more search request, which loads that field for
every document of the same page of results that lacks it. A `exceptions.LazyLoadWarning` tells you
it happened and how to avoid it:

<!-- docs-test: warns LazyLoadWarning -->
```python
papers = query.all()
[paper.citation_count for paper in papers]
# -> [480, 120, 9800]
```

```text
LazyLoadWarning: Document.citation_count was not fetched; loading it for 3 document(s) with another search
request. Select the fields you need, e.g. Document.select(Document.citation_count, ...), to avoid this.
```

All three values came from one request, and reading `citation_count` again sends nothing. A page holds up
to `config.page_size` documents (200 by default), so a loop over 1000 documents that reads one
unfetched field sends 5 extra requests, one per page. A value SciX does not have is stored as `None`, so it
is never requested twice.

Some things to know:

- The extra request is synchronous, even inside [async code]({{< ref "usage/async" >}}). There, select the
  fields you need.
- A document is found again by its identifier (in practice its bibcode). A document with no identifier
  loads nothing; the field reads as `None`.
- `score` is never loaded this way, because a relevance score belongs to the search that produced the
  document.
- `doc.authors` needs several fields at once and loads all that are missing in one request.

### Search-only fields

Some fields can be searched but are never returned: `abs`, `affil`, `all`, `arxiv`, `collection`,
`full`, `grant_agencies`, `grant_id`, `inst`, `lang` and `orcid`. Selecting one raises `QueryError`, and
reading one on a document raises `AttributeError`:

<!-- docs-test: raises AttributeError -->
```python
paper.full
```

### Fields that cannot be searched

Two fields are returned but not indexed by SciX, so they cannot be searched or sorted by: `links_data` and
`page_range`. A condition on one raises `QueryError` when you build it.

## Sorting

`order_by()` accepts single-valued fields of the model. Sorting by a multi-valued field (such as
`author`), a search-only field such as `abs`, or an unindexed field raises `QueryError`. With no order,
results come by relevance. The client always adds `id` as a final sort key, so that pages of results
never overlap.

## Describe a model

`describe()` returns one `FieldInfo` per field, with its name, type, whether it is multi-valued, stored
and indexed, its Solr name and its match kind:

```python
info = Document.describe()
[field.name for field in info if field.multivalued][:5]
# -> ['aff', 'aff_abbrev', 'aff_canonical', 'aff_id', 'alternate_bibcode']
```

`to_dict()` returns the values loaded on a document, by field name, without loading anything:

```python
sorted(papers[0].to_dict())
# -> ['bibcode', 'citation_count', 'id', 'scix_id', 'title']
```

## Related objects

Documents link to other models. These are computed from the document's fields and need no request:

| Attribute | Value | From |
|---|---|---|
| `doc.journal` | a [`Journal`]({{< ref "usage/journals" >}}), or `None` | the journal part of the bibcode |
| `doc.affiliation` | one entry per author: `None`, an [`Affiliation`]({{< ref "usage/affiliations" >}}), or a tuple of them | `aff_id` |
| `doc.authors` | a list of [`Author`]({{< ref "usage/authors" >}}) objects | the per-author fields |
| `doc.uat_concepts` | a list of [`UATConcept`]({{< ref "usage/vocabularies" >}}) objects | `uat_id` |
| `doc.planetary_features` | a list of [`PlanetaryFeature`]({{< ref "usage/vocabularies" >}}) objects | `planetary_feature_id` |

`utils.flatten()` turns the per-author structure of `doc.affiliation` into a flat list:

```python
riess = Document.get(bibcode="1998AJ....116.1009R")
{affiliation.country for affiliation in utils.flatten(riess.affiliation)}
# -> Australia
```

## Identity

A document is identified by the first of `bibcode`, `scix_id` and `id` that has a value. Two documents are
equal, and hash alike, when that is the same field with the same value, so documents from different
searches can be compared and put in sets. A document with no identifier is equal only to itself.

```python
Document.get(bibcode="2000ApJ...529L..45C") == paper
# -> True
```
