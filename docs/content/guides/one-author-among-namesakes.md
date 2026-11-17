---
title: One author among namesakes
weight: 3
description: Find the papers of one person with a common name by tying the name to the institutions that person has worked at.
---

After this page you can separate one author from other people with the same name, by asking for papers
on which *that author* gave one of the institutions you know they worked at.

## The question

"Which papers are by the M. Mayor who worked at the University of Geneva, and which are by someone else
of the same name?"

A name search finds every M. Mayor. Adding an institution to the search does not help on its own, because
SciX then asks only that *somebody* on the paper is at that institution.

## The search that goes wrong

This asks for papers with an author called M. Mayor and an author at the Center for Astrophysics. It
finds a paper on which Mayor was in Geneva and two of his co-authors were at the CfA:

```python
from scixplorer import *

cfa = Affiliation.get(abbreviation="CfA")

careless = (
    Document
    .select(Document.bibcode)
    .where((Document.author == "Mayor, M") & (Document.affiliation == cfa))
)
[paper.bibcode for paper in careless]
# -> ['2000ApJ...529L..45C']
```

## Tie the name to the institution

Inside `Document.authors.any(...)` every condition is about the same author. Now the CfA search finds
nothing, as it should:

```python
careful = (
    Document
    .select(Document.bibcode)
    .where(Document.authors.any((Author.name == "Mayor, M") & (Author.affiliation == cfa)))
)
[paper.bibcode for paper in careful]
# -> []
```

## An institution and its departments

People give their affiliation at different levels: the university on one paper, a department or an
observatory on the next. A condition on an institution matches only that identifier, so list the
institution together with its children:

```python
geneva = Affiliation.get(abbreviation="U Geneva")
[child.abbreviation for child in geneva.children]
# -> ['Data Ctr Astrop', 'Phy Sec', 'Ast Obs', 'ISDC']

at_geneva = Document.authors.any(
    (Author.name == "Mayor, M") & Author.affiliation.in_([geneva, *geneva.children])
)
query = (
    Document
    .select(Document.bibcode)
    .where(at_geneva)
)
[paper.bibcode for paper in query]
# -> ['1995Natur.378..355M', '2000ApJ...529L..45C']
```

For someone who has moved, put every institution of their career in the one list:
`Author.affiliation.in_([geneva, *geneva.children, cambridge, *cambridge.children])`.

## The papers that are someone else's

Negate the condition to see what a plain name search would have added. These are the papers by an M. Mayor
who did not give a Geneva affiliation: namesakes, and papers on which SciX has no curated institution for
the author.

```python
others = (
    Document
    .select(Document.bibcode)
    .where((Document.author == "Mayor, M") & ~at_geneva)
)
[paper.bibcode for paper in others]
# -> []
```

Read through this list before you discard it. In the sample it is empty; against SciX it will hold the
real person's papers that lack a curated affiliation as well as the namesakes.

## What is sent

SciX's query language cannot say "the same author", so the client sends a weaker condition (the name, and
any of the institutions, anywhere on the paper) and checks each paper that comes back:

```python
print(query.explain())
# -> q = author:"Mayor, M" AND aff_id:("A03003" OR "A03004" OR "A03005" OR "A03006" OR "A11267")
# -> checked client-side on each returned document
```

See [where conditions are checked]({{< ref "usage/authors#where-conditions-are-checked" >}}) for what that
means for `limit()` and `count()`.

## With `ads`

With the `ads` package you can only write the search that goes wrong. To do better you would fetch
`author` and `aff_id` for every paper by anyone of that name, and compare the two lists position by
position yourself.

## Things to know

- Find an institution's identifier with `Affiliation.get()` or by searching `Affiliation`; see
  [Affiliations]({{< ref "usage/affiliations" >}}). Abbreviations are not unique: two institutions are
  called `"IoA"`.
- Many affiliations in SciX have no curated identifier, more so on older papers. Those papers cannot match
  a condition on `Author.affiliation`. `Author.aff == "Geneva"` searches the affiliation as the paper wrote
  it instead, and can be combined with the name in the same way.
- The client compares names by SciX's documented rule for initials and full names, without SciX's curated
  list of name variants; see [Matching]({{< ref "usage/matching#matching-in-python" >}}).
