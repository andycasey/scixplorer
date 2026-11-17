---
title: What a reading list is missing
weight: 8
description: Use a library as part of a search to find the papers its contents cite, or are cited by, that the library does not hold yet.
---

After this page you can ask what the papers in one of your libraries cite that the library lacks, rank
those papers, see how many of your papers cite each one, and add them to a library.

## The question

"I have a library of papers on a subject. Which papers do they depend on that I have not collected?"

A library can be part of a search: `Document.in_(library)` is a condition like any other, so it can go
inside a [citation operator]({{< ref "usage/operators" >}}) and be negated outside it.

## What the library cites and does not hold

`Document.references(...)` finds the papers cited by the papers that match its condition. Take away the
ones already in the library, and put the most cited first:

```python
from scixplorer import Document, Library, citation_helper, search

library = Library.get(name="Emission-line galaxies")
library.bibcodes
# -> ('2006MNRAS.372..961K', '2008ApJ...681.1183K')

missing = (
    Document
    .select(
        Document.bibcode,
        Document.title,
        Document.citation_count,
    )
    .where(Document.references(Document.in_(library)) & Document.not_in(library))
    .order_by(Document.citation_count.desc())
)
[(paper.bibcode, paper.title[0]) for paper in missing]
# -> [('2001ApJ...556..121K', 'Theoretical Modeling of Starburst Galaxies')]
```

SciX does the whole of this. The library's bibcodes are never downloaded:

```python
search.to_solr(missing)
# -> references(docs(library/Rk3bq7xQTgS0vVZ2mN8cWw)) AND -docs(library/Rk3bq7xQTgS0vVZ2mN8cWw)
```

## How many of your papers cite each one

A paper cited by one of your papers is a detail; a paper cited by most of them is a gap. For each missing
paper, count the papers in the library that cite it:

```python
def citing_papers_in_library(bibcode):
    query = (
        Document
        .select()
        .where(Document.citations(Document.bibcode == bibcode) & Document.in_(library))
    )
    return query.count()

cited_by = {paper.bibcode: citing_papers_in_library(paper.bibcode) for paper in missing.limit(20)}
cited_by
# -> {'2001ApJ...556..121K': 2}
```

That is one small request for each missing paper, so limit the list first. For a quick answer without the
counts, the [citation helper]({{< ref "usage/services#citation-suggestions" >}}) suggests up to ten papers
for a library in one request:

```python
suggestions = citation_helper(library)
[(suggestion["bibcode"], suggestion["score"]) for suggestion in suggestions]
# -> [('2001ApJ...556..121K', 2)]
```

## Other questions of the same shape

Swap the operator to change the question. These show what is sent:

```python
newer = Document.citations(Document.in_(library)) & Document.not_in(library) & (Document.year >= 2024)
search.to_solr(newer)
# -> citations(docs(library/Rk3bq7xQTgS0vVZ2mN8cWw)) AND -docs(library/Rk3bq7xQTgS0vVZ2mN8cWw) AND year:[2024 TO *]

methods = Document.useful(Document.in_(library)) & Document.not_in(library)
search.to_solr(methods)
# -> useful(docs(library/Rk3bq7xQTgS0vVZ2mN8cWw)) AND -docs(library/Rk3bq7xQTgS0vVZ2mN8cWw)
```

| Condition | Finds |
|---|---|
| `Document.references(Document.in_(library))` | what the library's papers cite |
| `Document.citations(Document.in_(library))` | what cites the library's papers: newer work that builds on them |
| `Document.useful(Document.in_(library))` | what the most relevant of them cite often, usually methods papers |
| `Document.reviews(Document.in_(library))` | what cites the most relevant of them: reviews of the subject |

To leave out more than one library, say a second one of papers you have decided to skip, add another
`& Document.not_in(skipped)`.

## Add them to a library

`add()` accepts a query. It returns the number of documents SciX added:

```python
library.add(missing)
# -> 1
library.num_documents
# -> 3
```

To keep the suggestions apart until you have read them, create a library for them instead:
`Library.create(name="To read", documents=missing)`.

A query remembers its results once it has run. After changing the library, build the query again to ask
the question afresh.

## With `ads`

The `ads` package does not manage libraries. You would fetch the library's identifier with a request of
your own, and write `references(docs(library/<id>)) -docs(library/<id>)` as a string.

## Things to know

- `docs(library/<id>)` is not yet confirmed against the live API. If it does not work, set
  `config.library_membership = "bibcodes"` and the client fetches the library's bibcodes instead; see
  [Libraries]({{< ref "usage/libraries#search-within-a-library" >}}). More than 50 bibcodes cannot go
  inside an operator, so in that mode this guide works only for small libraries.
- `useful` and `reviews` look only at the most relevant papers of the library, as SciX ranks them.
- The operators are evaluated by SciX, so their results cannot be checked by the client; see
  [Operators]({{< ref "usage/operators#what-can-go-inside-an-operator" >}}).
