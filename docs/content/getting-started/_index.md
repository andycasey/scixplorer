---
title: Getting started
weight: 1
description: Install scixplorer, set your API token, and run your first search, all on one page.
---

This page takes you from nothing to a working search. The pages in this section go into each step in
more detail.

## 1. Install

scixplorer needs Python 3.10 or later. We recommend [uv](https://docs.astral.sh/uv/):

```shell
uv add scixplorer
```

or, with pip:

```shell
pip install scixplorer
```

## 2. Set your API token

Every request to SciX needs a personal API token. Sign in at [scixplorer.org](https://scixplorer.org),
create a token at <https://scixplorer.org/user/settings/token>, and save it where the client looks for
it:

```shell
mkdir -p ~/.scix
echo "your-token" > ~/.scix/token
chmod 600 ~/.scix/token
```

The environment variable `SCIX_API_TOKEN` works too, and so does a token you already use with the `ads`
package (`ADS_API_TOKEN`, `ADS_DEV_KEY`, `~/.ads/token` or `~/.ads/dev_key`). To set it in code:

```python
from scixplorer import Document, config, search

config.token = "your-token"
```

Treat the token like a password. scixplorer never shows it in a `repr` or an error message.

## 3. Search

A search starts from a model, usually `Document`. Write one method per line: `select()` says which
fields to fetch, `where()` which documents match, `order_by()` how to sort them, and `limit()` how many
to return.

```python
query = (
    Document
    .select(
        Document.bibcode,
        Document.title,
        Document.year,
    )
    .where(Document.author == "Kewley, L")
    .order_by(Document.year.desc())
    .limit(10)
)
```

Building a query sends nothing. Iterating sends it, a page of results at a time:

```python
[(paper.year, paper.bibcode) for paper in query]
# -> [(2008, '2008ApJ...681.1183K'), (2006, '2006MNRAS.372..961K'), (2001, '2001ApJ...556..121K')]
```

`count()`, `first()` and `all()` run it too, and `Document.get()` fetches one document by an identifier:

```python
query.count()
# -> 3
paper = Document.get(bibcode="2000ApJ...537..916S")
paper.title
# -> ['Theoretical Transmission Spectra during Extrasolar Giant Planet Transits']
```

Combine conditions with `&` (and), `|` (or) and `~` (not), each in its own parentheses:

```python
recent_kewley = (Document.author == "Kewley, L") & (Document.year >= 2005)
search.to_solr(recent_kewley)
# -> author:"Kewley, L" AND year:[2005 TO *]
```

`search.to_solr()` shows the Solr query a condition becomes, and `query.explain()` shows the whole
request a query would send, without sending it.

## 4. Read the results

A document's fields are attributes with Python types: `int` for counts and years, `datetime.date` for
dates, lists for fields with several values, and `None` where SciX has no value.

```python
paper.year
# -> 2000
paper.pubdate
# -> datetime.date(2000, 7, 1)
paper.author
# -> ['Seager, S.', 'Sasselov, D. D.']
```

Your editor completes the field names (`Document.` lists them all), and a type checker knows each
field's type. Values are checked as you write a condition, so `Document.year == "2000"` raises
`QueryError` straight away.

## Next

| Page | You will learn to |
|---|---|
| [Install and set your token]({{< ref "getting-started/install" >}}) | every place the token can go, using several tokens, and the other settings |
| [Your first search]({{< ref "getting-started/first-search" >}}) | every way to run a query, what it sends, and how to read the documents it returns |
| [Coming from ads]({{< ref "getting-started/coming-from-ads" >}}) | move code written for `ads.SearchQuery` |
| [Usage]({{< ref "usage" >}}) | search by author, affiliation, journal and more, and use libraries, exports and async |
| [Guides]({{< ref "guides" >}}) | answer whole questions, such as who you have written with and in which countries |
