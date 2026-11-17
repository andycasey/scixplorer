---
title: Errors and limits
weight: 14
description: The exceptions scixplorer raises, rate limits, retries, and the limits on what one search can contain.
---

After this page you can catch the right exceptions, keep an eye on your rate limits, predict which failed
requests are retried, and stay within the limits on what a search can contain.

{{< callout type="info" >}}
scixplorer has not yet been run against the live SciX API. The behaviour on this page is tested against
simulated responses; how the server answers some requests is still to be confirmed by the
[live test suite]({{< ref "development/live-tests" >}}).
{{< /callout >}}

## Exceptions

Every exception scixplorer raises derives from `exceptions.SciXError`, so one `except` clause can
catch them all:

| Exception | Derives from | Raised when |
|---|---|---|
| `SciXError` | `Exception` | (the base class) |
| `QueryError` | `SciXError` | a condition is malformed: a value of the wrong type, a field that cannot be searched or sorted, a bad position |
| `UnsupportedQueryError` | `QueryError` | the condition is valid, but no search can carry it (see the limits below) |
| `NotLocallyEvaluable` | `QueryError` | a condition the client must check in Python (on a journal, an affiliation, a vocabulary, or an author) uses something only SciX can evaluate |
| `DoesNotExist` | `SciXError`, `LookupError` | `get()` found nothing; each model has its own subclass, such as `Document.DoesNotExist` |
| `APIError` | `SciXError` | SciX answered with an error, or with a response of an unexpected shape, or could not be reached |
| `AuthenticationError` | `APIError` | there is no token, or SciX rejected it (HTTP 401 or 403) |
| `RateLimitExceeded` | `APIError` | the rate limit for a service is used up (HTTP 429) |
| `DataNotAvailable` | `SciXError` | a vocabulary has not been downloaded (see [Reference data]({{< ref "guide/reference-data" >}})) |

Two warnings, which are not errors:

| Warning | Emitted when |
|---|---|
| `LazyLoadWarning` | a field that was not fetched is loaded on demand (see [Results]({{< ref "guide/results#lazy-loading" >}})) |
| `DataWarning` | a value from SciX could not be converted to its field's type and became `None`, or a cached data file could not be read |

`APIError` has `message`, `status_code` (or `None` if no response arrived) and `response` (the parsed
response, or `None`). The message gives the method, the path and SciX's own explanation:

```python
from scixplorer import *

query = (
    Document
    .select()
    .where(Document.year == 2015)
)
try:
    papers = query.all()
except exceptions.AuthenticationError:
    print("check your API token")
except exceptions.APIError as error:
    print(error.status_code, error.message)
```

## Rate limits

SciX limits how many requests each token may send to each service per day. After every response the
client records what SciX reported, by service, in the client's `rate_limits`:

```python
client = get_default_client()
client.rate_limits["search/query"]
# -> RateLimit(limit=5000
```

Each entry has `limit`, `remaining` and `reset` (a timezone-aware UTC `datetime`). The service is the first
two parts of the request path, such as `search/query`, `search/bigquery` or `biblib/libraries`.

When a limit is used up, SciX answers HTTP 429 and the client raises `RateLimitExceeded` at once, without
waiting for the reset. Its `limit`, `remaining` and `reset` attributes say when you can continue. Once a
response has reported no remaining requests for a service, later requests to that service raise
`RateLimitExceeded` without being sent, until the reset time.

```python
try:
    papers = query.all()
except exceptions.RateLimitExceeded as error:
    print(f"rate limited until {error.reset}")
```

Searches cost one request per page of results, so set a `limit()` when you need only the first few
documents, and select the fields you need to avoid [lazy loading]({{< ref "guide/results#lazy-loading" >}}).

## Retries

A request that fails for a transient reason is sent again, up to `config.max_retries` times (3 by
default). The first retry waits `config.retry_backoff` seconds (1.0 by default), and each later one
twice as long as the one before.

| Request | Retried after a timeout, a lost connection or HTTP 502/503/504 |
|---|---|
| Searches (`/search/query` and `/search/bigquery`), exports, resolver, network and citation-helper requests, library listings and fetches, `update`, `delete`, `empty`, `set_permissions` | yes |
| Library changes that SciX may already have applied: `create`, `add`, `remove`, `union`, `intersection`, `difference`, `copy_to`, `transfer` | only if the connection could not be made at all |

These are never retried: HTTP 429 (see above), HTTP 401 and 403, any other 4xx, and HTTP 500. They raise
at once. `config.timeout` (30 seconds by default) limits each attempt.

## Limits on what a search can contain

### Related values

A condition through a journal, an affiliation or a vocabulary is resolved in Python to a list of
identifiers, which is sent in the query. At most 1000 identifiers fit in one search request, so a condition
that matches more raises `UnsupportedQueryError` when the search is planned:

<!-- docs-test: raises UnsupportedQueryError -->
```python
query = (
    Document
    .select()
    .where(Document.affiliation.country_code == "US")
)
query.count()
```

```text
UnsupportedQueryError: Document.affiliation: 1773 Affiliation rows matched country_code:"US", but at most
1000 aff_id values fit in one search request. Narrow the Affiliation condition.
```

Narrow the condition, or split the search into several.

### Large sets of bibcodes

Up to 50 bibcodes (`config.max_inline_bibcodes`) are sent in the query string. A larger set is handled
in one of two ways, depending on where it is:

| Where the set is | What is sent |
|---|---|
| joined to the rest of the search with `&` | the bibcodes are uploaded to `/search/bigquery`, and the rest of the search filters them |
| negated and joined with `&` (`Document.not_in(...)`) | the rest of the search is sent, and the client drops the excluded bibcodes as results arrive |
| inside `\|`, a nested `~`, or an operator | nothing: `UnsupportedQueryError` |

```python
many = [f"2020ApJ...{900 + i}....1A" for i in range(60)]
query = (
    Document
    .select(Document.bibcode)
    .where(Document.in_(many) & (Document.year > 2000))
)
print(query.explain())
# -> POST /search/bigquery
# -> searching /search/bigquery with 60 bibcode(s)
```

When the client drops results itself, it also applies `offset()` and `limit()`, and `count()` pages through
the results. If you need a large set inside a more complex condition, put the documents in a
[library]({{< ref "guide/libraries" >}}) and use `Document.in_(library)`, which SciX evaluates itself.

### Conditions checked by the client

Some [per-author conditions]({{< ref "guide/authors#where-conditions-are-checked" >}}) are checked by the
client on the documents SciX returns. Like a negated large bibcode set, each must be a top-level condition
(joined to the rest with `&`, optionally negated with `~`); inside `|`, a nested `~` or an operator they
raise `UnsupportedQueryError` with an explanation of how to restructure the search.

### Searches that would download the whole index

A search whose only conditions are checked by the client would make SciX return every document in its
index for the client to check. The client refuses such a search when it is planned, from `explain()`,
`count()`, iteration, `first()` and `search.to_solr()` alike:

<!-- docs-test: raises UnsupportedQueryError -->
```python
query = (
    Document
    .select()
    .where(Document.not_in(many))
)
query.first()
```

```text
UnsupportedQueryError: This query would download every document in SciX: excluding 60 bibcode(s) is done
client-side on each returned document, and nothing else narrows the search ADS runs (its query would be
*:*, apart from the default collection). Add a condition ADS can search, AND-ed with the rest, for example a
year range (Document.year.between(2015, 2020)), an author (Document.author == "Last, F") or a journal
(Document.journal == "ApJ").
```

Add any condition SciX can search. The [default collection]({{< ref "guide/collections" >}}) does not count,
but a positive set of bibcodes uploaded to `/search/bigquery` does. Here an author narrows the search, and
the excluded set is applied as results arrive:

```python
search.to_solr(Document.not_in(many) & (Document.author == "Hogg, D"))
# -> author:"Hogg, D"
```

### Pages

The client requests `config.page_size` documents per request (200 by default). SciX returns at most
2000 per request, so a larger setting is treated as 2000.
