---
title: Getting started
weight: 1
description: Install scixplorer, set your API token, run your first search, and choose a default collection, all on one page.
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

Every request to SciX needs a personal API token. If you have a NASA/ADS token, you already have one:
SciX accepts it as it is.

{{< callout type="warning" >}}
**No token yet?** Sign in at [scixplorer.org](https://scixplorer.org) and create one at
<https://scixplorer.org/user/settings/token>.
{{< /callout >}}

If you have used the `ads` package, there is nothing to do: scixplorer finds the token where `ads` did.
Otherwise, put it on the first line of the file `~/.scix/token`:

```shell
mkdir -p ~/.scix
echo "your-token" > ~/.scix/token
chmod 600 ~/.scix/token
```

or in the environment variable `SCIX_API_TOKEN`. Treat the token like a password.

[Token management]({{< ref "getting-started/token-management" >}}) lists every place the client looks,
and which one wins.

## 3. Search

A search starts from a model, usually `Document`. Write one method per line: `select()` says which
fields to fetch, `where()` which documents match, `order_by()` how to sort them, and `limit()` how many
to return.

```python
from scixplorer import Document, config, search

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

## 5. Set a default collection

SciX covers several disciplines, which it calls collections: `astronomy`, `physics`, `earthscience` and
others. By default scixplorer searches all of them, so a search for a name or a common word can return
papers from disciplines you did not have in mind. To keep your searches within one collection, set it
once, before you search:

```python
config.collection = "astronomy"
search.to_solr(recent_kewley)
# -> author:"Kewley, L" AND year:[2005 TO *] AND database:"astronomy"
```

Give a list for several collections, such as `config.collection = ["astronomy", "physics"]`, and set it
back to `None` to search them all.

The collection is added only to open searches. A search that names its documents, by bibcode, DOI or
library, is left alone, and so is one that names a collection itself:

```python
search.to_solr(Document.bibcode == "2015GeoRL..42..264E")
# -> bibcode:"2015GeoRL..42..264E"
search.to_solr(Document.database == "earthscience")
# -> database:"earthscience"
```

[Collections]({{< ref "usage/collections" >}}) lists exactly when the setting applies.

## Next

| Page | You will learn to |
|---|---|
| [Install and set your token]({{< ref "getting-started/install" >}}) | install with uv or pip, put your token in place, and change the other settings |
| [Your first search]({{< ref "getting-started/first-search" >}}) | every way to run a query, what it sends, and how to read the documents it returns |
| [Coming from ads]({{< ref "getting-started/coming-from-ads" >}}) | move code written for `ads.SearchQuery` |
| [Token management]({{< ref "getting-started/token-management" >}}) | every place the client looks for a token, and which one wins |
| [Usage]({{< ref "usage" >}}) | search by author, affiliation, journal and more, and use libraries, exports and async |
| [Guides]({{< ref "guides" >}}) | answer whole questions, such as who you have written with and in which countries |
