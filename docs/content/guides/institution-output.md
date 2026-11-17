---
title: An institution's output
weight: 7
description: Report the papers an institution led and the papers it took part in over a period, by year, journal and partner institution.
---

After this page you can list the refereed papers of an institution and all its departments over a period,
separate the papers it led from those it took part in, break them down by year and journal, name the
institutions it worked with, and export the list.

## The question

"What did we publish in the review period, how much of it did we lead, and who did we work with?"

## The institution and its departments

Authors give their affiliation at different levels, and a condition on an institution matches only that
one identifier. Start with the institution together with its children:

```python
from scixplorer import *

caltech = Affiliation.get(abbreviation="Caltech")
family = [caltech, *caltech.children]
[unit.abbreviation for unit in family]
# -> ['Caltech', 'LIGO Livingston', 'Big Bear Sol Obs', 'Div Chem', 'Div Geo Plan Sci', 'IPAC', 'Div Phy Math Ast', 'JPL'
```

`children` goes one level down. For an institution with departments inside its schools, add each child's
own `children` too.

## Papers it took part in, and papers it led

`Document.affiliation` matches when any author is at one of the institutions. `Document.authors[0]`
is the first author:

```python
period = Document.year.between(2010, 2016) & (Document.property == "refereed")

took_part = (
    Document
    .select()
    .where(Document.affiliation.in_(family) & period)
    .order_by(Document.year)
)
led = (
    Document
    .select()
    .where(Document.authors[0].affiliation.in_(family) & period)
    .order_by(Document.year)
)
[paper.bibcode for paper in took_part]
# -> ['2010GeoRL..3704201M', '2015ApJ...808...16N']
[paper.bibcode for paper in led]
# -> ['2010GeoRL..3704201M']
```

The first-author condition is sent to SciX as a position in the list of institution identifiers:

```python
search.to_solr(led)
# -> pos(aff_id:("A01093" OR "A11207" OR
```

## By year and by journal

The results are ordinary Python objects, so count them however the report needs. `paper.journal` is the
[journal]({{< ref "usage/journals" >}}) named by the bibcode, with its full title:

```python
from collections import Counter

Counter(paper.year for paper in took_part)
# -> Counter({2010: 1, 2015: 1})
Counter(paper.journal.title for paper in took_part)
# -> Counter({'Geophysical Research Letters': 1, 'The Astrophysical Journal': 1})
sum(paper.citation_count for paper in took_part)
# -> 790
```

## Partner institutions

For each paper, collect the institutions of the authors who are not yours. Climbing to the top-level
institution counts a university once, whichever of its departments the co-authors gave:

```python
def top_level(affiliation):
    while affiliation.parent is not None:
        affiliation = affiliation.parent
    return affiliation

ours = set(family)
partners = Counter()
for paper in took_part:
    partners.update({
        top_level(affiliation).abbreviation
        for author in paper.authors
        for affiliation in author.affiliation
        if affiliation not in ours
    })

sorted(partners.items())
# -> [('JHU', 2), ('Max Planck', 1), ('NYU', 1)]
```

Each partner is counted once per paper. For countries, collect `affiliation.country` in the same way.

## Export the list

Every [export function]({{< ref "usage/services#export" >}}) accepts a query:

```python
print(export.bibtex(led, sort=Document.year.desc()))
# -> @ARTICLE{2010GeoRL..3704201M,
```

## With `ads`

With the `ads` package you would look up the identifier of every department by hand, write the `pos()`
query as a string, and work out journal titles, parent institutions and countries from tables of your own.

## Things to know

- Only curated affiliations count. A paper whose authors' institutions SciX has not identified is missed
  by both searches; `Document.aff == "Caltech"` searches the affiliations as the papers wrote them.
- The first-author search relies on SciX accepting `pos()` on `aff_id`, which is not yet confirmed against
  the live API; see [Authors]({{< ref "usage/authors#by-position" >}}).
- One condition may name at most 1000 institutions; see
  [Affiliations]({{< ref "usage/affiliations#limits" >}}). A university with its departments is far below
  that, but every institution of a large country is not.
- "Led" here means the first author. For fields that list authors alphabetically, it means little.
