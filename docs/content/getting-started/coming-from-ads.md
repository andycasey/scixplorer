---
title: Coming from ads
weight: 3
description: Move code written for the ads package (ads.SearchQuery, version 0.13 and earlier) to scixplorer, and know what behaves differently.
---

After this page you can move code written for the `ads` package (`ads.SearchQuery(q=...)`, version 0.13
and earlier) to scixplorer, and you will know each difference that could change your results.

## The simplest change

Keep the query string you already have. Put it in `Document.raw()`, and write the sort order and the
number of rows as methods of the query:

```python
from scixplorer import Document, export, get_default_client, search

query = (
    Document
    .select()
    .where(Document.raw('author:"Seager, S" year:[2000 TO 2005]'))
    .order_by(Document.citation_count.desc())
    .limit(10)
)
[paper.bibcode for paper in query]
# -> ['2003ApJ...585.1038S', '2000ApJ...537..916S']
```

That replaces `ads.SearchQuery(q='author:"Seager, S" year:[2000 TO 2005]', sort="citation_count desc",
rows=10)`. For `sort="citation_count asc"`, write `.order_by(Document.citation_count.asc())`. Leave out
`order_by()` to sort by relevance, and leave out `limit()` to get every match.

That gets existing code running, but treat it as a first step. scixplorer sends a raw query string as you
wrote it and cannot check it, so a mistake in it is found by SciX, or not at all.

## The recommended change

Write the search itself in Python. This is the same search, with each part of the query string written
as a condition on a field:

```python
query = (
    Document
    .select()
    .where((Document.author == "Seager, S") & Document.year.between(2000, 2005))
    .order_by(Document.citation_count.desc())
    .limit(10)
)
[paper.bibcode for paper in query]
# -> ['2003ApJ...585.1038S', '2000ApJ...537..916S']
search.to_solr(query)
# -> author:"Seager, S" AND year:[2000 TO 2005]
```

It sends SciX the same query, as `search.to_solr()` shows, and it gives you what a string cannot:

- **Mistakes are caught as you write.** A misspelt field name is an error in your editor and in Python,
  and a value of the wrong type, such as `Document.year == "2000"`, raises `QueryError` before anything
  is sent.
- **Your editor completes the field names**, and offers only the operations each field supports.
- **Values are quoted and escaped for you.**
- **You can search by things a query string cannot easily express**, such as several conditions on the
  same author, an institution's country, or a journal's title. See [Usage]({{< ref "usage" >}}).

Most query strings translate one part at a time:

| In a query string | As an expression |
|---|---|
| `author:"Seager, S"` | `Document.author == "Seager, S"` |
| `author:"^Seager, S"` | `Document.author[0] == "Seager, S"` |
| `year:2005` | `Document.year == 2005` |
| `year:[2000 TO 2005]` | `Document.year.between(2000, 2005)` |
| `citation_count:[100 TO *]` | `Document.citation_count >= 100` |
| `title:"dark matter"` | `Document.title == "dark matter"` |
| `bibstem:(ApJ OR AJ)` | `Document.bibstem.in_(["ApJ", "AJ"])` |
| `a AND b`, `a OR b`, `NOT a` | `a & b`, `a \| b`, `~a`, each condition in its own parentheses |
| `citations(bibcode:2001ApJ...556..121K)` | `Document.citations(Document.bibcode == "2001ApJ...556..121K")` |

[Expressions]({{< ref "reference/expressions" >}}) lists every kind of expression with the query it
sends, and [Searching]({{< ref "usage/searching" >}}) explains them.

Keep `Document.raw()` as a last resort, for Solr syntax that has no expression. It mixes with
expressions, so a long query string can be translated a piece at a time, and only what is left inside
`raw()` goes unchecked:

```python
partly_translated = (
    Document
    .select()
    .where(
        Document.raw('author:"Seager, S"'),
        Document.year >= 2003,
    )
)
search.to_solr(partly_translated)
# -> (author:"Seager, S") AND year:[2003 TO *]
```

The rest of this page covers the other parameters and what behaves differently.

## At a glance

| In `ads` | In scixplorer |
|---|---|
| `import ads` | `from scixplorer import Document` |
| `ads.config.token = "..."` | `config.token = "..."` |
| `ads.SearchQuery(q="...")` | `Document.select().where(...)` with [expressions](#the-recommended-change), or `Document.raw("...")` to keep the string |
| `fl=["bibcode", "title"]` | `.select(Document.bibcode, Document.title)` |
| `fq="..."` | another condition in `.where(...)` |
| `sort="citation_count desc"` | `.order_by(Document.citation_count.desc())` |
| `rows=50, max_pages=4` | `.limit(200)`: the most documents in total |
| `ads.SearchQuery(author="Kewley, L", year="2008")` | `.where((Document.author == "Kewley, L") & (Document.year == 2008))` |
| `next(ads.SearchQuery(...))` | `query.first()` |
| `query.response.numFound` | `query.count()` |
| `ads.search.Article` | `Document` |
| `article.bibtex` | `export.bibtex(paper)` |
| `ads.ExportQuery(bibcodes, format="aastex").execute()` | `export.aastex(bibcodes)` |
| `ads.MetricsQuery(bibcodes).execute()` | no equivalent yet: [send the request yourself](#metrics) |
| `ads.RateLimits.getRateLimits("SearchQuery").to_dict()` | `get_default_client().rate_limits["search/query"]` |
| `ads.exceptions.APIResponseError` | `exceptions.APIError` |

## Your token

scixplorer reads your token from the places `ads` did: the environment variables `ADS_API_TOKEN` and
`ADS_DEV_KEY`, and the files `~/.ads/token` and `~/.ads/dev_key`. It also reads the new `SCIX_API_TOKEN`
and `~/.scix/token`, which come before them. See [Token management]({{< ref "getting-started/token-management" >}}).

One order changed. In `ads`, a token in the environment or in a file won over `ads.config.token`. In
scixplorer, `config.token` comes first, so setting it in code always takes effect.

## The other parameters

The other parameters of `ads.SearchQuery` are methods of the query too: `fl` becomes the fields in
`select()`, and each `fq` becomes another condition in `where()`. This is the search above with
`fl=["bibcode", "title"]` and `fq="year:[2000 TO 2005]"`:

```python
query = (
    Document
    .select(
        Document.bibcode,
        Document.title,
    )
    .where(
        Document.author == "Seager, S",
        Document.year.between(2000, 2005),
    )
    .order_by(Document.citation_count.desc())
    .limit(10)
)
[paper.bibcode for paper in query]
# -> ['2003ApJ...585.1038S', '2000ApJ...537..916S']
query.count()
# -> 2
```

A few things work differently from `ads.SearchQuery`:

- **`limit()` is the total.** In `ads`, `rows` was the size of each page and `max_pages` (1 by default)
  the number of pages, so a search stopped at 50 documents unless you asked for more. In scixplorer,
  `limit()` is the most documents the query returns, and without it a query returns every match. Pages
  are fetched as you iterate.
- **Without `select()` fields, every field is fetched** except a few large ones (see below), where `ads`
  fetched six.
- **Without `order_by()`, results are ordered by relevance**, as in `ads`.
- **There is no `hl` or `cursorMark`.** Highlights are not supported yet, and paging is handled for you.
  To search with another token, use a client: `query.using(Client(token="..."))`.
- **A query can be read more than once.** An `ads.SearchQuery` was an iterator that could be read once.
  A scixplorer query can be iterated again, and once read to the end, its results are kept, so iterating
  again, `count()` and `len()` send nothing.

## Read the results

Results are `Document` objects instead of `ads` `Article` objects. Their fields have the same names, but
they hold typed values instead of the raw JSON:

```python
paper = Document.get(bibcode="2000ApJ...537..916S")
paper.year
# -> 2000
paper.pubdate
# -> datetime.date(2000, 7, 1)
str(paper)
# -> 2000ApJ...537..916S
```

| | In `ads` | In scixplorer |
|---|---|---|
| `year` | a string, `"2000"` | an `int`, `2000` |
| `pubdate` | a string, `"2000-07-00"` | a `datetime.date` (an unknown day is the 1st) |
| `id` | as SciX sent it | a string of digits |
| dates and times such as `entry_date` | strings | timezone-aware `datetime`s |
| `str(paper)` | `<Seager, S. et al. 2000, 2000ApJ...537..916S>` | the bibcode |

Code that compares `paper.year` with a string, such as `paper.year == "2000"`, is now always false.

Reading a field you did not fetch still loads it, now with a `LazyLoadWarning`. In `ads` that sent one
request per field per article. In scixplorer one request loads the field for every document on the same
page of results. The large fields `ack`, `body`, `caption`, `citation`, `credit`, `mention` and
`reference` are only fetched when you select them by name. See [Results]({{< ref "usage/results" >}}).

`article.citation` and `article.reference` sent a search each. In scixplorer they are fields that hold
bibcodes. To search the citing papers, use the `citations` operator:

```python
citing = (
    Document
    .select(Document.bibcode)
    .where(Document.citations(Document.bibcode == "2001ApJ...556..121K"))
)
[paper.bibcode for paper in citing]
# -> ['2006MNRAS.372..961K', '2008ApJ...681.1183K']
```

## Exports

`ads.ExportQuery` and `article.bibtex` are replaced by one function per format, which takes bibcodes,
documents and queries:

```python
print(export.bibtex("2000ApJ...537..916S"))
# -> @ARTICLE{2000ApJ...537..916S,
```

The formats `ads` offered (`bibtex`, `bibtexabs`, `ads`, `endnote`, `aastex`, `ris`, `icarus`, `mnras`,
`soph` and `votable`) all have a function of that name, and there are more. See
[Services]({{< ref "usage/services" >}}).

## Metrics

scixplorer has no metrics service yet. Until it does, send the request that `ads.MetricsQuery` sent with
the client, which adds your token, retries and error handling:

<!-- docs-test: skip -->
```python
from scixplorer.client import Request

request = Request("POST", "/metrics", json={"bibcodes": ["2000ApJ...537..916S"]}, idempotent=True)
metrics = get_default_client().send(request).data
```

## Rate limits and errors

The client records the rate limits SciX reports, by service:

```python
get_default_client().rate_limits["search/query"]
# -> RateLimit(limit=5000
```

Errors from the API raise `exceptions.APIError` (in `ads`, `APIResponseError`), with the subclasses
`AuthenticationError` and `RateLimitExceeded`. Every exception scixplorer raises derives from
`exceptions.SciXError`. See [Errors and limits]({{< ref "usage/errors-and-limits" >}}).
