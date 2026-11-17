---
title: Operators
weight: 9
description: Search citation networks and SciX's rankings with second-order operators such as citations, references, trending, useful, reviews, similar and top_n.
---

After this page you can find the papers that cite or are cited by a set of papers, ask SciX for trending,
useful and review papers on a topic, take the top few papers of a search, and restrict a condition to
positions in a list.

## Second-order operators

A second-order operator takes a search and returns other documents related to its results. In scixplorer
each is a class method of `Document` that takes a condition (or a Solr query string) and returns a
condition, which you combine with others like any condition.

```python
from scixplorer import Document, search

cannon = Document.bibcode == "2015ApJ...808...16N"
search.to_solr(Document.citations(cannon))
# -> citations(bibcode:"2015ApJ...808...16N")
```

| Method | Finds | Sent as |
|---|---|---|
| `citations(expr)` | documents that cite the documents matching `expr` | `citations(...)` |
| `references(expr)` | documents cited by the documents matching `expr` | `references(...)` |
| `trending(expr)` | documents most read by people who recently read papers matching `expr` | `trending(...)` |
| `useful(expr)` | documents frequently cited by the most relevant papers matching `expr`, usually on methods | `useful(...)` |
| `reviews(expr)` | documents citing the most relevant papers matching `expr`: the reviews of the field | `reviews(...)` |
| `instructive(expr)` | a synonym of `reviews` | `instructive(...)` |
| `similar(expr)` | documents whose abstracts are worded like those matching `expr` | `similar(...)` |
| `citis(expr)` | as `citations`, using less memory but more slowly | `citis(...)` |
| `classic_relevance(expr)` | the documents matching `expr`, ranked (almost) as ADS Classic did | `classic_relevance(...)` |
| `join_citations(expr)`, `join_references(expr)` | as `citations` and `references`, using a Lucene block join | `joincitations(...)`, `joinreferences(...)` |
| `reviews2(expr)`, `useful2(expr)` | the original implementations of `reviews` and `useful` | `reviews2(...)`, `useful2(...)` |

The ADS help pages describe the
[citation operators](https://ui.adsabs.harvard.edu/help/search/citations-and-references) and the
[second-order operators](https://ui.adsabs.harvard.edu/help/search/second-order) in detail.

### Citations and references

To find the papers citing a paper, or the papers it cites, pass a condition that matches it:

```python
emcee = Document.bibcode == "2013PASP..125..306F"
citing = (
    Document
    .select(Document.bibcode)
    .where(Document.citations(emcee))
)
[paper.bibcode for paper in citing]
# -> ['2015ApJ...808...16N', '2016arXiv160303040C']
```

The argument is a condition, not a document: write `Document.citations(Document.bibcode ==
paper.bibcode)` for a document you already have. The condition can be any search, so you can follow the
citations of a whole set of papers:

```python
first_author_papers = Document.author[0] == "Ness, M"
citing_others = Document.citations(first_author_papers) & ~(Document.author == "Ness, M")
search.to_solr(citing_others)
# -> citations(pos(author:"Ness, M", 1)) AND -author:"Ness, M"
```

### Topics

`trending`, `useful`, `reviews` and `similar` are most useful with a topic search. They also accept Solr
syntax as a string:

```python
search.to_solr(Document.useful(Document.abs == "stellar spectroscopy") & (Document.year >= 2010))
# -> useful(abs:"stellar spectroscopy") AND year:[2010 TO *]
search.to_solr(Document.trending("exoplanets"))
# -> trending(exoplanets)
```

## The top of a search: `top_n`

`Document.top_n(n, expr, order_by=None)` matches the first `n` documents of a search, ranked by
relevance or by `order_by`. Unlike `limit()`, it is part of the condition, so you can combine it with
others or pass it to another operator:

```python
most_cited = Document.top_n(
    10,
    (Document.year == 2020) & (Document.bibstem == "ApJ"),
    order_by=Document.citation_count.desc(),
)
search.to_solr(most_cited)
# -> topn(10, year:2020 AND bibstem:"ApJ", citation_count desc)
search.to_solr(Document.references(most_cited))
# -> references(topn(10, year:2020 AND bibstem:"ApJ", citation_count desc))
```

`order_by` is a sort key such as `Document.citation_count.desc()`, or a field for ascending order.
`n` must be at least 1.

## Positions: `pos`

Indexing a field restricts a condition to positions in it: `Document.author[0] == "Ness, M"`. See
[Matching]({{< ref "usage/matching#positions" >}}). `Document.pos(expr, start, end=None)` does the same
for a condition written as Solr syntax, or on another multi-valued field. Its positions count from 1 and
`end` is included:

```python
search.to_solr(Document.pos('author:"Ness, M"', 1))
# -> pos(author:"Ness, M", 1)
search.to_solr(Document.pos(Document.aff == "Heidelberg", 1, 2))
# -> pos(aff:"Heidelberg", 1, 2)
```

## What can go inside an operator

Anything SciX can search goes inside an operator, including conditions on journals, affiliations,
vocabularies and libraries, which the client resolves first:

```python
search.to_solr(Document.useful(Document.journal == "AJ"))
# -> useful(bibstem:"AJ")
```

A per-author condition that the client must check itself (see
[Authors]({{< ref "usage/authors#where-conditions-are-checked" >}})) cannot go inside an operator, because
SciX evaluates the operator on its own. That raises `UnsupportedQueryError`, as does a set of more than 50
bibcodes inside an operator (see [Errors and limits]({{< ref "usage/errors-and-limits#large-sets-of-bibcodes" >}})).

Operators run on the server, so the client cannot check their results. The sample used for the examples on
this site evaluates only `citations`, `references` and `topn`; the other examples here show the query
instead of running it.
