---
title: Token management
weight: 4
description: Every place scixplorer looks for your API token, the order it checks them in, how to set a token in code, and what happens when there is none.
---

After this page you will know every place the client looks for your API token and which one wins, how to
set a token in code or for one client, and what happens when there is none. To get a token and put it in
the usual place, see [Install and set your token]({{< ref "getting-started/install" >}}).

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
from scixplorer import Client, Document, config

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
query = (
    Document
    .select()
    .where(Document.year == 2020)
    .using(client)
)
```

## When there is no token

Without a token, the first request fails before anything is sent, with an
`AuthenticationError` that lists the places above. A token that SciX rejects raises the same exception,
with the status code (401 or 403) and SciX's message:

<!-- docs-test: raises AuthenticationError -->
```python
config.token = None
anonymous = Client()
query = (
    Document
    .select()
    .where(Document.year == 2020)
    .using(anonymous)
)
query.first()
```

Queries that never need a request, such as searches of [journals]({{< ref "usage/journals" >}}) and
[affiliations]({{< ref "usage/affiliations" >}}), work without a token.
