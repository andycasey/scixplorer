---
title: Your first search
weight: 2
description: Build a document search step by step, run it, and read the documents it returns.
---

After this page you can build a search with `select`, `where`, `order_by` and `limit`, run it in the ways
that suit you, see the request it sends, and read the fields of the documents it returns.

The examples on this site run against a small offline sample of real papers, so the values they show
(citation counts, for example) are illustrative.

## Build a query

A search starts from `Document.select()`, and each method after it adds one part of the query. Write
one method per line, inside parentheses:

```python
from scixplorer import *

query = (
    Document
    .select(
        Document.bibcode,
        Document.title,
        Document.year,
    )
    .where(Document.author == "Hogg, D")
    .order_by(Document.year.desc())
    .limit(10)
)
```

Each method returns a new query and leaves the one it was called on unchanged, so you can also keep a part
and build on it: `recent = query.where(Document.year >= 2015)` leaves `query` as it was.

| Method | What it does |
|---|---|
| `select(*fields)` | Starts a query and says which fields to fetch. With no fields, every field except the large [deferred ones]({{< ref "guide/results#deferred-fields" >}}). |
| `where(*conditions)` | Adds conditions. Several conditions, or several calls, must all hold. |
| `order_by(*keys)` | Sorts by fields: `Document.year.desc()`, `Document.year.asc()`, or a bare field (ascending). It replaces any earlier order. |
| `limit(n)` | Returns at most `n` documents. `limit(None)` removes the limit. |
| `offset(n)` | Skips the first `n` documents. |
| `using(client)` | Sends the requests with this client instead of the default one. |

Building a query sends nothing. Conditions are checked as you write them: a value of the wrong type fails at
once, not when the query runs.

<!-- docs-test: raises QueryError -->
```python
Document.year == "2015"
```

The message is `Document.year needs an int, not '2015'`.

## Run it

Iterating runs the query. Documents arrive one page of results at a time, and only as many pages are
requested as you read:

```python
for paper in query:
    print(paper.year, paper.bibcode)
```

Other ways to run it:

```python
query.count()
# -> 3
query.exists()
# -> True
query.first()
# -> <Document: bibcode='2016arXiv160303040C'>
query.all()
# -> [<Document: bibcode='2016arXiv160303040C'>, <Document: bibcode='2015ApJ...808...16N'>, <Document: bibcode='2013PASP..125..306F'>]
```

| Call | Returns | Requests |
|---|---|---|
| `for doc in query` | each document in turn | one per page of results |
| `query.all()` | a list of every document | one per page |
| `query.first()` | the first document, or `None` | one, for one document |
| `query.get()` | the first document, or raises `DoesNotExist` | one, for one document |
| `query.count()` | how many documents iterating would give (the offset and limit apply) | one, with no documents in it |
| `query.exists()` or `bool(query)` | whether anything matches | one, for one document |
| `len(query)`, `query[0]`, `query[1:3]` | as for a list | the whole result, like `all()` |

Once a query has been iterated to the end, its results are kept. Iterating again, `all()`, `count()`,
`len()` and indexing then make no further requests. Each new query, including one made by adding a
condition, starts afresh.

To fetch one document by an identifier, use `Document.get` with keyword arguments:

```python
paper = Document.get(bibcode="1995Natur.378..355M")
paper.title
# -> A Jupiter-mass companion to a solar-type star
```

If nothing matches, it raises `Document.DoesNotExist`, which is also a `LookupError`:

<!-- docs-test: raises DoesNotExist -->
```python
Document.get(bibcode="2099ApJ...999....1X")
```

## See what it sends

`explain()` shows the request a query would send, and the decisions behind it, without sending anything:

```python
print(query.explain())
# -> q = author:"Hogg, D"
# -> sort = year desc,id asc
```

The output for this query is:

```text
Searches ADS with GET /search/query, up to 200 rows per request.
GET /search/query
    q = author:"Hogg, D"
    fl = bibcode,id,scix_id,title,year
    sort = year desc,id asc
    start = 0
    rows = 10
- fetching 5 selected field(s); others load on first access
- sorted by year desc, then id asc so pages are stable
```

`q` is the Solr query. `fl` lists the fields fetched: the ones you selected, plus the identifiers
`bibcode`, `id` and `scix_id`, which are always fetched. The client adds `id asc` to every sort so that
pages never overlap or skip a document.

To see only the query string, use `search.to_solr`:

```python
search.to_solr(Document.author[0] == "Ness, M")
# -> pos(author:"Ness, M", 1)
```

## Read the documents

A document's fields are attributes. Each has a type: text fields are `str`, counts are `int`, dates are
`datetime.date` or timezone-aware `datetime.datetime`, and fields that hold several values are lists. A
field SciX has no value for is `None`.

```python
paper.year
# -> 1995
paper.author
# -> ['Mayor, Michel', 'Queloz, Didier']
paper.pubdate
# -> datetime.date(1995, 11, 1)
paper.journal
# -> <Journal: abbreviation='Natur'>
```

`paper.journal` is a [`Journal`]({{< ref "guide/journals" >}}) object. Documents also link to their authors'
[affiliations]({{< ref "guide/affiliations" >}}) and to [vocabulary concepts]({{< ref "guide/vocabularies" >}}).

A document's `repr` names its identifier, and `str()` gives the identifier alone:

```python
str(paper)
# -> 1995Natur.378..355M
```

`to_dict()` returns the values loaded so far:

```python
paper.to_dict()["bibcode"]
# -> 1995Natur.378..355M
```

[Results]({{< ref "guide/results" >}}) covers fields, types and loading in detail, and
[Searching]({{< ref "guide/searching" >}}) covers everything you can put in `where()`.
