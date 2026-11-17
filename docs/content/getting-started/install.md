---
title: Install and set your token
linkTitle: Install
weight: 1
description: Install scixplorer, put your API token where the client reads it, and run one search.
---

After this page you will have scixplorer installed, an API token in a place the client reads, and one
search that works.

## Install

scixplorer needs Python 3.10 or later. Its only dependency is [httpx](https://www.python-httpx.org).
We recommend installing it with [uv](https://docs.astral.sh/uv/). To add it to a project managed with uv:

```shell
uv add scixplorer
```

To try it without setting up a project, start Python with scixplorer available:

```shell
uv run --with scixplorer python
```

If you use pip instead:

```shell
pip install scixplorer
```

Installing also gives you the command `scix`, which searches from the terminal:
see [Command line]({{< ref "usage/command-line" >}}).

## Set your API token

Every request to SciX needs a personal API token. If you have a NASA/ADS token, you already have one:
SciX accepts it as it is.

{{< callout type="warning" >}}
**No token yet?** Sign in at [scixplorer.org](https://scixplorer.org) and create one at
<https://scixplorer.org/user/settings/token>.
{{< /callout >}}

If you have used the `ads` package, there is nothing to do. scixplorer reads the token from the places
`ads` did: the environment variables `ADS_API_TOKEN` and `ADS_DEV_KEY`, and the files `~/.ads/token` and
`~/.ads/dev_key`.

Otherwise, put the token on the first line of the file `~/.scix/token`:

```shell
mkdir -p ~/.scix
echo "your-token" > ~/.scix/token
chmod 600 ~/.scix/token
```

or in the environment variable `SCIX_API_TOKEN`.

Treat the token like a password. scixplorer never puts it in the `repr` of a configuration or a client,
or in an exception message.

[Token management]({{< ref "getting-started/token-management" >}}) lists every place the client looks and
the order it checks them in, and shows how to set a token in code.

## Your first search

With the token in place, search:

```python
from scixplorer import Document

paper = Document.get(bibcode="2006MNRAS.372..961K")
paper.title
# -> The host galaxies and classification of active galactic nuclei
```

## Other settings

`config` holds the other settings too. They are read each time a query runs or a request is sent,
so a change applies from then on.

| Setting | Default | Meaning |
|---|---|---|
| `api_url` | `"https://api.scixplorer.org/v1"` | The API's base URL. |
| `timeout` | `30.0` | Seconds allowed for each HTTP request. |
| `max_retries` | `3` | How many times a failed request is retried (see [Errors and limits]({{< ref "usage/errors-and-limits" >}})). |
| `retry_backoff` | `1.0` | Seconds before the first retry; each later retry waits twice as long. |
| `page_size` | `200` | Documents requested per search request (at most 2000 are used). |
| `max_inline_bibcodes` | `50` | Larger bibcode sets are sent to `/search/bigquery` (see [Errors and limits]({{< ref "usage/errors-and-limits" >}})). |
| `library_membership` | `"docs"` | How "document is in a library" is searched (see [Libraries]({{< ref "usage/libraries" >}})). |
| `collection` | `None` | Limit open searches to a SciX collection (see [Collections]({{< ref "usage/collections" >}})). |

Next: [your first search]({{< ref "getting-started/first-search" >}}), step by step.
