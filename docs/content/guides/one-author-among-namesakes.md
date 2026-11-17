---
title: One author among namesakes
weight: 3
description: Find the papers of one person with a common name by tying the name to the institutions that person has worked at.
---

After this page you can separate one author from other people with the same name, by asking for papers
on which *that author* gave one of the institutions you know they worked at.

## The question

"Which papers are by the S. Seager who worked at Harvard, and which are by someone else of the same name?"

A name search finds every S. Seager. Adding an institution to the search does not help on its own, because
SciX then asks only that *somebody* on the paper is at that institution.

## The search that goes wrong

This asks for papers with an author called S. Seager and an author at Princeton University's Department of
Astrophysical Sciences. It finds a paper on which Seager was at the Institute for Advanced Study and her
co-author was at the university:

```python
from scixplorer import Affiliation, Author, Document

princeton = Affiliation.lookup("A01140")
princeton.canonical_name
# -> Princeton University, Department of Astrophysical Sciences

careless = (
    Document
    .select(Document.bibcode)
    .where((Document.author == "Seager, S") & (Document.affiliation == princeton))
)
[paper.bibcode for paper in careless]
# -> ['2003ApJ...585.1038S']
```

## Tie the name to the institution

Inside `Document.authors.any(...)` every condition is about the same author. Now the Princeton search
finds nothing, as it should:

```python
careful = (
    Document
    .select(Document.bibcode)
    .where(Document.authors.any((Author.name == "Seager, S") & (Author.affiliation == princeton)))
)
[paper.bibcode for paper in careful]
# -> []
```

## An institution and its departments

People give their affiliation at different levels: the university on one paper, a department or an
observatory on the next. A condition on an institution matches only that identifier, so list the
institution together with its children:

```python
harvard = Affiliation.get(abbreviation="Harvard U")
harvard.children.count()
# -> 13

at_harvard = Document.authors.any(
    (Author.name == "Seager, S") & Author.affiliation.in_([harvard, *harvard.children])
)
query = (
    Document
    .select(Document.bibcode)
    .where(at_harvard)
)
[paper.bibcode for paper in query]
# -> ['2000ApJ...537..916S']
```

For someone who has moved, put every institution of their career in the one list:
`Author.affiliation.in_([harvard, *harvard.children, ias])`.

## The papers that are someone else's

Negate the condition to see what a plain name search would have added. These are the papers by an
S. Seager who did not give a Harvard affiliation: namesakes, papers she wrote somewhere else, and papers
on which SciX has no curated institution for the author.

```python
others = (
    Document
    .select(Document.bibcode)
    .where((Document.author == "Seager, S") & ~at_harvard)
)
[paper.bibcode for paper in others]
# -> ['2003ApJ...585.1038S']
```

Read through this list before you discard it. Here it holds a paper she wrote at another institution;
against SciX it will also hold the namesakes.

## What is sent

SciX's query language cannot say "the same author", so the client sends a weaker condition (the name, and
any of the institutions, anywhere on the paper) and checks each paper that comes back:

```python
print(query.explain())
# -> q = author:"Seager, S" AND aff_id:("A00211" OR "A00212"
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
  a condition on `Author.affiliation`. `Author.aff == "Harvard"` searches the affiliation as the paper wrote
  it instead, and can be combined with the name in the same way.
- The client compares names by SciX's documented rule for initials and full names, without SciX's curated
  list of name variants; see [Matching]({{< ref "usage/matching#matching-in-python" >}}).
