---
title: How it works
weight: 15
description: What happens between a Python expression and a SciX request, for readers who want to know why a search is sent the way it is.
---

After this page you will know how a search goes from a Python expression to an HTTP request, which parts
run in Python, and which endpoints the client uses. You do not need this to use scixplorer, but it helps
to read the output of `explain()`.

## 1. Expressions are trees

Comparing a field builds a small, immutable tree of nodes. Nothing is sent and nothing is compiled yet:

```python
from scixplorer import *

condition = (Document.author[0] == "Ness, M") & Document.year.between(2015, 2020)
condition
# -> And(operands=(Func(name='pos', args=(Match(field=<NameField Document.author>, value='Ness, M', exact=False), 1, 1)), Range(field=<IntegerField Document.year>, lo=2015, hi=2020, include_lo=True, include_hi=True)))
```

Each field type offers only the operations that make sense for it (ranges on numbers and dates, wildcards
on text), and checks values as the tree is built. Expressions have no truth value, so `and`, `or` and
`not` fail loudly instead of dropping a condition.

## 2. Two interpreters

The same tree can be read two ways, by two internal modules:

- `solr.compile()` turns it into a Solr query string. It is the only place that knows Solr's quoting,
  escaping and range syntax.
- `evaluate.matches()` evaluates it in Python against an object.

```python
from scixplorer import evaluate, solr

solr.compile(condition)
# -> pos(author:"Ness, M", 1) AND year:[2015 TO 2020]
evaluate.matches(Journal.title.contains("astrophysical"), Journal.lookup("ApJ"))
# -> True
```

Journals, affiliations, vocabulary concepts and features, and your list of libraries are searched with the
Python interpreter, because they are held in memory. Documents are searched by SciX.

## 3. The planner

Before a document search is compiled, a planner (`search.plan`) rewrites it, in this order:

1. A condition on another model's field, such as `Journal.title == "Icarus"`, is attached to the
   document relation that leads to that model.
2. Each relation condition is evaluated in Python against the related model, and replaced by the matching
   identifiers: journal titles become `bibstem` values, affiliations become `aff_id` values, concepts and
   features become `uat_id` and `planetary_feature_id` values. At most 1000 are allowed.
3. Library membership is left to SciX (`docs(library/<id>)`), or, with
   `config.library_membership = "bibcodes"`, replaced by the library's bibcodes.
4. The tree is simplified. A condition that can never match ends the planning: no request is sent.
   Then each [per-author condition]({{< ref "guide/authors#where-conditions-are-checked" >}}) is translated
   into a condition on the document's per-author lists, exactly where possible; otherwise a necessary
   condition is sent and the real one is kept for the client to check on each result.
5. The [default collection]({{< ref "guide/collections" >}}) is added to open searches.
6. Sets of more than 50 bibcodes are taken out of the query string: uploaded to `/search/bigquery`, or
   excluded from the results as they arrive. A search that would then leave SciX with nothing to narrow it
   is refused.
7. The tree is compiled, and the sort gets a final `id` key so that pages are stable.

The plan is pure: given the same query and configuration, it gives the same result without any I/O.

```python
query = (
    Document
    .select(Document.bibcode)
    .where(condition)
    .limit(5)
)
plan = search.plan(query)
plan.q
# -> pos(author:"Ness, M", 1) AND year:[2015 TO 2020]
plan.request(0, 5)
# -> Request(method='GET', path='/search/query'
```

`explain()` shows the first request of the plan and its notes, which record each of these decisions.

## 4. Sending requests

The code that runs a search never performs I/O itself. It yields request descriptions and receives
responses, one page at a time. A small driver sends each request with a `Client`, either
synchronously or asynchronously, so paging and result handling are written once and work both ways. This
is why every query has both `all()` and `aall()`.

The client adds your token, retries transient failures, records rate limits and turns error responses into
exceptions (see [Errors and limits]({{< ref "guide/errors-and-limits" >}})).

## Endpoints

| Endpoint | Method | Used for |
|---|---|---|
| `/search/query` | GET | document searches, counts and lazy loading |
| `/search/bigquery` | POST | document searches with more than 50 bibcodes |
| `/biblib/libraries`, `/biblib/libraries/<id>` | GET | listing and fetching libraries |
| `/biblib/libraries` | POST | creating a library |
| `/biblib/documents/<id>` | POST, PUT, DELETE | adding and removing documents, changing metadata, deleting |
| `/biblib/libraries/operations/<id>` | POST | `union`, `intersection`, `difference`, `copy_to`, `empty` |
| `/biblib/permissions/<id>`, `/biblib/transfer/<id>` | GET, POST | permissions and ownership |
| `/export/<format>` | POST | exports |
| `/resolver/<bibcode>[/<link type>]` | GET | external resources |
| `/vis/author-network`, `/vis/paper-network` | POST | network data |
| `/citation_helper` | POST | citation suggestions |

Paths are relative to `config.api_url`, `https://api.scixplorer.org/v1` by default.

## Sending your own requests

For an endpoint scixplorer does not wrap, send a `Request` from `scixplorer.client` with a client. You get the token,
retries, rate-limit tracking and error handling, and a `Response` whose `data` is the parsed
JSON:

```python
from scixplorer.client import Request

request = Request("GET", "/resolver/2015ApJ...808...16N")
response = get_default_client().send(request)
response.status_code
# -> 200
```

`Request` takes `method`, `path`, and optionally `params`, `json` (a JSON body), `content` (a raw body),
`headers` and `idempotent` (whether it is safe to retry; by default GET, PUT and DELETE are and POST is
not). `await client.asend(request)` is the asynchronous form.
