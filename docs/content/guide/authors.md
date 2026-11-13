---
title: Authors
weight: 3
description: Say several things about the same author, or about every author, and read each author's name, affiliations, email and ORCID.
---

After this page you can search for documents where *one* author meets several conditions (a name and an
affiliation, say), where *every* author meets one, or where the author at a given position does. You can
also read each author of a document as one object, and you will know which of these conditions SciX
checks and which the client checks.

## The problem: "an author" is not "the same author"

SciX stores what it knows about authors as separate lists on the document, one entry per author:
`author` (names), `aff` (affiliation strings), `aff_id` (curated affiliation identifiers), `email` and the
ORCID lists. A condition on one list means "any entry", so two conditions can be met by two different
authors.

Suppose you want papers by A. Casey written at a German institution. This finds one, but Casey was in
Cambridge; the German affiliation belongs to co-authors:

```python
import scixplorer as scix

careless = scix.Document.select(scix.Document.bibcode).where(
    (scix.Document.author == "Casey, A") & (scix.Document.affiliation.country_code == "DE")
)
[paper.bibcode for paper in careless]
# -> ['2016arXiv160303040C']
```

## One author: `scix.Document.authors.any()`

Inside `scix.Document.authors.any(...)`, every condition is about the same author. The conditions use the
fields of `scix.Author`:

```python
careful = scix.Document.select(scix.Document.bibcode).where(
    scix.Document.authors.any(
        (scix.Author.name == "Casey, A") & (scix.Author.affiliation.country_code == "DE")
    )
)
[paper.bibcode for paper in careful]
# -> []
```

The same search for D. Hogg finds the paper on which Hogg himself gave a German affiliation:

```python
query = scix.Document.select(scix.Document.bibcode).where(
    scix.Document.authors.any(
        (scix.Author.name == "Hogg, D") & (scix.Author.affiliation.country_code == "DE")
    )
)
[paper.bibcode for paper in query]
# -> ['2015ApJ...808...16N']
```

| `scix.Author` field | From the document's | Notes |
|---|---|---|
| `name` | `author` | a name field: matched with name variants |
| `position` | the author's index | 0 for the first author |
| `aff` | `aff` | the affiliation as written in the paper |
| `aff_id` | `aff_id` | a list: an author can have several curated affiliations |
| `affiliation` | `aff_id` | a relation to [`scix.Affiliation`]({{< ref "guide/affiliations" >}}) |
| `email` | `email` | |
| `orcid_pub`, `orcid_user`, `orcid_other` | the three ORCID lists | from the publisher, from users with a public profile, from the ADS claiming interface |
| `orcid` | the three ORCID lists | the first that is given; searched with the document's `orcid` field, which covers all three |

An author with several affiliations matches if any one of them does. To say several things about one
affiliation of the author, use `.any()` on the relation as well:

```python
one_affiliation = scix.Document.authors.any(
    (scix.Author.name == "Schmidt, B")
    & scix.Author.affiliation.any(
        (scix.Affiliation.country_code == "AU") & (scix.Affiliation.abbreviation == "MSSSO")
    )
)
scix.search.to_solr(one_affiliation)
# -> author:"Schmidt, B" AND aff_id:"A00343"
```

Conditions on the document itself go outside `any(...)`, joined with `&`. Putting one inside is an error
that says so:

<!-- docs-test: raises QueryError -->
```python
scix.Document.authors.any(scix.Document.year == 2015)
```

## Every author: `scix.Document.authors.all()`

`all(...)` matches documents on which every author meets the condition. A document must have at least one
author to match.

```python
swiss = scix.Document.select(scix.Document.bibcode).where(
    scix.Document.authors.all(scix.Author.affiliation.country_code == "CH")
    & (scix.Document.year < 2000)
)
[paper.bibcode for paper in swiss]
# -> ['1995Natur.378..355M']
```

An author with no curated affiliation does not meet an affiliation condition, so `all(...)` excludes papers
with any such author.

## By position

Index `scix.Document.authors` to choose authors by position. With one index you can write conditions
directly on its fields; `scix.Document.authors[0]` is the first author:

```python
first_in_britain = scix.Document.select(scix.Document.bibcode).where(
    scix.Document.authors[0].affiliation.country_code == "GB"
)
[paper.bibcode for paper in first_in_britain]
# -> ['2016arXiv160303040C']
scix.search.to_solr(scix.Document.authors[0].affiliation.abbreviation == "IoA")
# -> pos(aff_id:("A00319" OR "A13064"), 1)
```

A slice selects several positions, and then you say whether any or all of them must match:

```python
query = scix.Document.select(scix.Document.bibcode).where(
    scix.Document.authors[0:2].any(scix.Author.affiliation.abbreviation == "MPIA Heidelberg")
)
[paper.bibcode for paper in query]
# -> ['2015ApJ...808...16N']
```

Without an index (or with a slice) the attribute shorthand is ambiguous, so it raises an `AttributeError`
that suggests `.any()`, `.all()` or an index:

<!-- docs-test: raises AttributeError -->
```python
scix.Document.authors.name == "Ness, M"
```

Positions count from 0 and slices follow Python. Negative or open-ended positions raise `QueryError`.

{{< callout type="warning" >}}
Searching `aff` and `aff_id` by position relies on SciX accepting `pos()` on those fields, which has not yet
been confirmed against the live API (see [Live tests]({{< ref "development/live-tests" >}})). Positions on
other per-author fields, such as `email` and the ORCID lists, are checked by the client.
{{< /callout >}}

## Shorthands

Simple per-author conditions have shorter forms, and both forms send the same query:

| Per-author form | Shorthand |
|---|---|
| `scix.Document.authors[0].name == "Ness, M"` | `scix.Document.author[0] == "Ness, M"` |
| `scix.Document.authors.any(scix.Author.name == "Ness, M")` | `scix.Document.author == "Ness, M"` |
| `scix.Document.authors.any(scix.Author.affiliation.country_code == "AU")` | `scix.Document.affiliation.country_code == "AU"` |
| `scix.Document.authors.any(scix.Author.orcid == "...")` | `scix.Document.orcid == "..."` |

```python
long = scix.Document.authors.any(scix.Author.affiliation.country_code == "AU")
short = scix.Document.affiliation.country_code == "AU"
scix.search.to_solr(long) == scix.search.to_solr(short)
# -> True
```

Use the per-author form when you need more than one condition about an author.

## Read the authors of a document

`doc.authors` is a list of `scix.Author` objects, one per author, in order:

```python
paper = scix.Document.get(bibcode="2015ApJ...808...16N")
paper.authors
# -> [<Author 0: 'Ness, M.'>, <Author 1: 'Hogg, David W.'>, <Author 2: 'Rix, H. -W.'>, <Author 3: 'Ho, Anna. Y. Q.'>, <Author 4: 'Zasowski, G.'>]
hogg = paper.authors[1]
hogg.aff_id
# -> ['A00951', 'A01570']
hogg.affiliation
# -> (<Affiliation: id='A00951' (NYU)>, <Affiliation: id='A01570' (MPIA Heidelberg)>)
hogg.email is None
# -> True
```

SciX writes `-` for "no value" in the per-author lists; on an `Author` that becomes `None` (or an empty
list for `aff_id`). Affiliation identifiers that are not in the packaged
[affiliation data]({{< ref "guide/affiliations" >}}) are skipped in `affiliation`.

Reading `doc.authors` needs the fields it is built from (`author`, `aff`, `aff_id`, `email` and the ORCID
lists). If you selected only some fields, the missing ones are [loaded]({{< ref "guide/results#lazy-loading" >}})
in one request. `scix.Author` objects exist only as part of a document; you cannot search for authors on
their own.

## Where conditions are checked

SciX's query language has no "the same author" operator. The client translates each per-author condition
into a condition on the document's lists. When that translation is exact, it is all that is sent. When it
is not, SciX is sent a weaker condition that every match must meet, and the client checks the real
condition on each document that comes back.

| Condition | Checked by |
|---|---|
| A single condition: `authors.any(scix.Author.name == x)` | SciX |
| Conditions joined with `\|` | SciX |
| Several conditions on one position: `authors[0].any((...) & (...))` | SciX, for `name`, `aff` and `aff_id` |
| Several conditions joined with `&`, on any author or a slice | the client, after SciX narrows by each condition |
| `all(...)` | the client, after SciX narrows by the condition |
| `~` inside `any(...)` | the client |
| A position on `email` or an ORCID field | the client, after SciX narrows by the value |

`explain()` says which it is:

```python
query = scix.Document.select(scix.Document.bibcode).where(
    scix.Document.authors.any(
        (scix.Author.name == "Hogg, D") & (scix.Author.affiliation.abbreviation == "MPIA Heidelberg")
    )
)
print(query.explain())
# -> q = author:"Hogg, D" AND aff_id:"A01570"
# -> checked client-side on each returned document (conditions joined with & may be met by different authors)
```

A condition checked by the client has three consequences:

- The client fetches the fields it needs to check it (here `author` and `aff_id`), and pages through every
  document SciX returns for the weaker condition, so `limit()`, `offset()` and `count()` are applied
  client-side. Add other conditions to keep the number of documents SciX returns small.
- Python compares names with the documented initial and full-name rule, and text by its words. It does not
  know SciX's curated synonyms (see [Matching]({{< ref "guide/matching#matching-in-python" >}})).
- It must be a top-level condition of the search: joined to the rest with `&`, optionally negated with `~`.
  Inside `|`, or inside an operator such as `scix.Document.citations()`, it raises
  `UnsupportedQueryError`, which explains how to restructure the query.

If nothing else narrows the search, the client refuses to run it, because SciX would return every
document in the index for the client to check:

<!-- docs-test: raises UnsupportedQueryError -->
```python
scix.Document.select().where(
    ~scix.Document.authors.any((scix.Author.name == "Hogg, D") & (scix.Author.aff_id == "A00951"))
).count()
```

Add a condition SciX can search, such as a year range, an author or a journal. See
[Errors and limits]({{< ref "guide/errors-and-limits#searches-that-would-download-the-whole-index" >}}).
