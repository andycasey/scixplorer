---
title: Install and set your token
linkTitle: Install
weight: 1
description: Install scixplorer, get a SciX API token, and tell the client where to find it.
---

After this page you will have scixplorer installed, an API token in a place the client reads, and one
search that works.

## Install

scixplorer needs Python 3.10 or later. Its only dependency is [httpx](https://www.python-httpx.org).
It is not yet on PyPI, so install it from a copy of the source:

```shell
pip install /path/to/scixplorer
```

or, in a project managed with [uv](https://docs.astral.sh/uv/):

```shell
uv add /path/to/scixplorer
```

## Get an API token

Every request to SciX needs a personal API token. Sign in at [scixplorer.org](https://scixplorer.org) and
create one at <https://scixplorer.org/user/settings/token>.

Treat the token like a password. scixplorer never puts it in the `repr` of a configuration or a client,
or in an exception message.

## Tell the client where the token is

The client looks for the token when it sends a request, in this order, and uses the first one it finds:

| Order | Source | Example |
|---:|---|---|
| 1 | `config.token` | `config.token = "..."` in your code |
| 2 | Environment variable `SCIX_API_TOKEN` | `export SCIX_API_TOKEN=...` |
| 3 | Environment variable `ADS_API_TOKEN` | |
| 4 | Environment variable `ADS_DEV_KEY` | |
| 5 | First line of `~/.scix/token` | |
| 6 | First line of `~/.ads/token` | |
| 7 | First line of `~/.ads/dev_key` | |

Surrounding whitespace is removed, and an empty value is skipped. The environment and the files are read
again for every request, so a token you set while Python is running is picked up straight away.

The most durable place is the file:

```shell
mkdir -p ~/.scix
echo "your-token" > ~/.scix/token
chmod 600 ~/.scix/token
```

If you already used the `ads` package, your `ADS_API_TOKEN` variable or `~/.ads/dev_key` file works as it
is.

To set the token in code, assign it to the configuration:

```python
from scixplorer import *

config.token = "your-token"
```

`config` is the configuration every client uses unless it is given its own. Its `repr` leaves the
token out:

```python
config
# -> api_url='https://api.scixplorer.org/v1'
```

You can also give one client its own token, and send a query with it using `using()`:

```python
client = Client(token="your-token")
query = Document.select().where(Document.year == 2020).using(client)
```

## When there is no token

Without a token, the first request fails before anything is sent, with an
`AuthenticationError` that lists the places above. A token that SciX rejects raises the same exception,
with the status code (401 or 403) and SciX's message:

<!-- docs-test: raises AuthenticationError -->
```python
config.token = None
anonymous = Client()
Document.select().where(Document.year == 2020).using(anonymous).first()
```

Queries that never need a request, such as searches of [journals]({{< ref "guide/journals" >}}) and
[affiliations]({{< ref "guide/affiliations" >}}), work without a token.

## Your first search

With the token in place, search:

```python
paper = Document.get(bibcode="2015ApJ...808...16N")
paper.title
# -> The Cannon: A data-driven approach to Stellar Label Determination from Spectra
```

## Other settings

`config` holds the other settings too. They are read each time a query runs or a request is sent,
so a change applies from then on.

| Setting | Default | Meaning |
|---|---|---|
| `api_url` | `"https://api.scixplorer.org/v1"` | The API's base URL. |
| `timeout` | `30.0` | Seconds allowed for each HTTP request. |
| `max_retries` | `3` | How many times a failed request is retried (see [Errors and limits]({{< ref "guide/errors-and-limits" >}})). |
| `retry_backoff` | `1.0` | Seconds before the first retry; each later retry waits twice as long. |
| `page_size` | `200` | Documents requested per search request (at most 2000 are used). |
| `max_inline_bibcodes` | `50` | Larger bibcode sets are sent to `/search/bigquery` (see [Errors and limits]({{< ref "guide/errors-and-limits" >}})). |
| `library_membership` | `"docs"` | How "document is in a library" is searched (see [Libraries]({{< ref "guide/libraries" >}})). |
| `collection` | `None` | Limit open searches to a SciX collection (see [Collections]({{< ref "guide/collections" >}})). |

Next: [your first search]({{< ref "getting-started/first-search" >}}), step by step.
