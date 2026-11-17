---
title: Coming from ads
weight: 3
description: Move code written for the ads package (ads.SearchQuery, version 0.13 and earlier) to scixplorer, and know what behaves differently.
---

After this page you can move code written for the `ads` package (`ads.SearchQuery(q=...)`, version 0.13
and earlier) to scixplorer, and you will know each difference that could change your results.

## At a glance

| In `ads` | In scixplorer |
|---|---|
| `import ads` | `from scixplorer import *` |
| `ads.config.token = "..."` | `config.token = "..."` |
| `ads.SearchQuery(q="...", fl=[...], sort="...")` | `SearchQuery(q="...", fl=[...], sort="...")` |
| `ads.SearchQuery(author="Casey, A", year="2015")` | `Document.select().where((Document.author == "Casey, A") & (Document.year == 2015))` |
| `sort="citation_count"` | `sort="citation_count desc"`: the direction is required |
| `rows=50, max_pages=4` | `rows=200`: the most documents in total |
| `next(ads.SearchQuery(...))` | `SearchQuery(...).first()` |
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
and `~/.scix/token`, which come before them. See [Install and set your token]({{< ref "getting-started/install" >}}).

One order changed. In `ads`, a token in the environment or in a file won over `ads.config.token`. In
scixplorer, `config.token` comes first, so setting it in code always takes effect.

## Search with a query string

`SearchQuery` takes the Solr parameters you already use and returns a query:

```python
from scixplorer import *

query = SearchQuery(
    q='author:"Mayor, M" year:[1990 TO 2000]',
    fl="bibcode,title",
    sort="citation_count desc",
    rows=10,
)
[paper.bibcode for paper in query]
# -> ['1995Natur.378..355M', '2000ApJ...529L..45C']
query.count()
# -> 2
```

It takes `q`, `fl` (a list, or a comma-separated string), `sort`, `rows`, `start` and `fq`. Several of
them behave differently:

- **`rows` is the total.** In `ads`, `rows` was the size of each page and `max_pages` (1 by default)
  the number of pages, so a search stopped at 50 documents unless you asked for more. In scixplorer,
  `rows` is the most documents the query returns, and without it a query returns every match. Pages are
  fetched as you iterate. There is no `max_pages`.
- **`sort` needs a direction.** `ads` read `sort="citation_count"` as descending and accepted
  `"citation_count+desc"`. scixplorer needs `"citation_count desc"`, and raises `QueryError` otherwise.
  Without a sort, results are ordered by relevance.
- **`fl` names must be real fields.** A misspelt name raises `QueryError` instead of being sent. Without
  `fl`, every field is fetched except a few large ones (see below), where `ads` fetched six.
- **There are no keyword arguments.** `ads.SearchQuery(author="Casey, A")` added `author:"Casey, A"` to
  the query string. Write the condition in Python instead (next section), or add it to `q`.
- **There is no `hl`, `cursorMark` or `token`.** Highlights are not supported yet. Paging is handled for
  you. To search with another token, use a client: `SearchQuery(...).using(Client(token="..."))`.
- **A query can be read more than once.** An `ads.SearchQuery` was an iterator that could be read once.
  A scixplorer query can be iterated again, and once read to the end, its results are kept, so iterating
  again, `count()` and `len()` send nothing.

## Write the search in Python

You do not have to write Solr query strings at all. The same search, written with Python expressions:

```python
query = (
    Document
    .select(
        Document.bibcode,
        Document.title,
    )
    .where((Document.author == "Mayor, M") & Document.year.between(1990, 2000))
    .order_by(Document.citation_count.desc())
    .limit(10)
)
[paper.bibcode for paper in query]
# -> ['1995Natur.378..355M', '2000ApJ...529L..45C']
search.to_solr(query)
# -> author:"Mayor, M" AND year:[1990 TO 2000]
```

Conditions are checked as you write them, your editor completes the field names, and you can search by
things a query string cannot easily express, such as an author's affiliation or country. Start with
[Your first search]({{< ref "getting-started/first-search" >}}).

## Read the results

Results are `Document` objects instead of `ads` `Article` objects. Their fields have the same names, but
they hold typed values instead of the raw JSON:

```python
paper = Document.get(bibcode="1995Natur.378..355M")
paper.year
# -> 1995
paper.pubdate
# -> datetime.date(1995, 11, 1)
str(paper)
# -> 1995Natur.378..355M
```

| | In `ads` | In scixplorer |
|---|---|---|
| `year` | a string, `"1995"` | an `int`, `1995` |
| `pubdate` | a string, `"1995-11-00"` | a `datetime.date` (an unknown day is the 1st) |
| `id` | as SciX sent it | a string of digits |
| dates and times such as `entry_date` | strings | timezone-aware `datetime`s |
| `str(paper)` | `<Mayor, Michel et al. 1995, 1995Natur.378..355M>` | the bibcode |

Code that compares `paper.year` with a string, such as `paper.year == "1995"`, is now always false.

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
    .where(Document.citations(Document.bibcode == "2013PASP..125..306F"))
)
[paper.bibcode for paper in citing]
# -> ['2015ApJ...808...16N', '2016arXiv160303040C']
```

## Exports

`ads.ExportQuery` and `article.bibtex` are replaced by one function per format, which takes bibcodes,
documents and queries:

```python
print(export.bibtex("1995Natur.378..355M"))
# -> @ARTICLE{1995Natur.378..355M,
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

request = Request("POST", "/metrics", json={"bibcodes": ["1995Natur.378..355M"]}, idempotent=True)
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
