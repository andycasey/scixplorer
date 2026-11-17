---
title: Async
weight: 13
description: Run document searches from asyncio code, and know which parts of scixplorer remain synchronous.
---

After this page you can run searches from `asyncio` code, run several at once, and avoid the parts of
scixplorer that still block.

## Async queries

Every query can be run asynchronously. The async methods mirror the synchronous ones:

| Synchronous | Asynchronous |
|---|---|
| `for doc in query` | `async for doc in query` |
| `query.all()` | `await query.aall()` |
| `query.first()` | `await query.afirst()` |
| `query.get()` | `await query.aget()` |
| `query.exists()` | `await query.aexists()` |
| `query.count()` | `await query.acount()` |

```python
from scixplorer import *

import asyncio


async def main():
    query = Document.select(Document.bibcode, Document.year).where(
        Document.author == "Hogg, D"
    )
    total = await query.acount()
    papers = [paper.bibcode async for paper in query]
    return total, papers


asyncio.run(main())
# -> (3, ['2015ApJ...808...16N', '2016arXiv160303040C', '2013PASP..125..306F'])
```

Pages are requested one at a time as you consume them, exactly as in synchronous code. The same
`Client` serves both: it creates an `httpx.AsyncClient` the first time it is used asynchronously.

## Several searches at once

Run independent searches concurrently with `asyncio.gather`:

```python
async def counts(years):
    queries = [Document.select().where(Document.year == year) for year in years]
    return await asyncio.gather(*(query.acount() for query in queries))


asyncio.run(counts([1995, 1998, 2015]))
# -> [1, 1, 1]
```

Each search counts against your [rate limit]({{< ref "guide/errors-and-limits#rate-limits" >}}) as usual.

## What stays synchronous

Some parts of scixplorer make requests synchronously, even when called from async code. They block the
event loop while they wait:

| Synchronous only | What to do in async code |
|---|---|
| [Lazy loading]({{< ref "guide/results#lazy-loading" >}}) of a field that was not fetched (including `doc.authors` when its fields are missing) | select every field you will read |
| [`Library`]({{< ref "guide/libraries" >}}) methods and properties (`create`, `add`, `update`, `bibcodes`, ...) | call them before or outside the event loop, or in a thread with `asyncio.to_thread` |
| [Services]({{< ref "guide/services" >}}) (`export`, `resolver`, `vis`, `citation_helper`) | the same |
| [`data.refresh()`]({{< ref "guide/reference-data" >}}) | the same |

Queries over libraries (`await Library.select().aall()`) run asynchronously like any query.
Searching for documents in a library works asynchronously too, because SciX tests the
membership: `await Document.select().where(Document.in_(library)).aall()`. With
`config.library_membership = "bibcodes"`, the library's bibcodes are fetched through the same async
path.

## Closing the client

The default client is created on first use and lives as long as your program. A client you create can be
closed when you are done, or used as a context manager:

```python
async def with_own_client():
    async with Client(token="your-token") as client:
        query = Document.select().where(Document.year == 2015).using(client)
        return await query.acount()
```

`async with` closes both the async and the sync `httpx` clients; `aclose()` does the same.
