---
title: An author's affiliation history
weight: 5
description: Follow one researcher through the institutions and countries they gave on their papers, year by year.
---

After this page you can take one ORCID iD and list the institutions that person gave on their papers, with
the years they gave each one, and find the papers they wrote while at a particular institution or in a
particular country.

## The question

"Where has this person worked, and when?"

A paper lists the affiliations of all its authors. The history of one person is the affiliation of *that
author* on each of their papers, in order of date.

## Read one author's affiliation on each paper

Search by ORCID iD, oldest paper first. On each paper, find the author who carries the iD and read their
curated affiliations:

```python
from scixplorer import Author, Document

who = "0000-0001-8152-3943"

papers = (
    Document
    .select()
    .where(Document.orcid == who)
    .order_by(Document.pubdate)
)

timeline = []
for paper in papers:
    for author in paper.authors:
        if author.orcid == who:
            timeline.append((paper.year, [affiliation.abbreviation for affiliation in author.affiliation]))

timeline
# -> [(2001, ['Res Sch Ast Astrop']), (2006, ['IfA']), (2008, ['IfA'])]
```

## First and last year at each institution

Turn the timeline into one entry per institution. An `Affiliation` can be a dictionary key:

```python
years = {}
for paper in papers:
    for author in paper.authors:
        if author.orcid != who:
            continue
        for affiliation in author.affiliation:
            first, last = years.get(affiliation, (paper.year, paper.year))
            years[affiliation] = (min(first, paper.year), max(last, paper.year))

[(affiliation.canonical_name, affiliation.country, span) for affiliation, span in years.items()]
# -> [('ANU, Research School of Astronomy and Astrophysics', 'Australia', (2001, 2001)), ('University of Hawaii, Manoa, Institute for Astronomy', 'United States', (2006, 2008))]
```

## By parent institution

People give a department on one paper and the university on the next. To count those as one place, climb
the hierarchy to the top-level institution:

```python
def top_level(affiliation):
    while affiliation.parent is not None:
        affiliation = affiliation.parent
    return affiliation

sorted({top_level(affiliation).abbreviation for affiliation in years})
# -> ['ANU', 'Manoa']
```

An institution can have more than one parent; `parent` is the first, and `parents` lists them all.

## The papers from one place

To get the papers this person wrote at an institution, or in a country, put the iD and the affiliation in
one `Document.authors.any(...)`, so that both are about the same author:

```python
in_australia = (
    Document
    .select(Document.bibcode)
    .where(
        Document.authors.any(
            (Author.orcid == who) & (Author.affiliation.country_code == "AU")
        )
    )
)
[paper.bibcode for paper in in_australia]
# -> ['2001ApJ...556..121K']
```

Without `any(...)`, a country condition is met by anyone on the paper. This search finds a paper although
this person never gave a German institution; two of her co-authors did:

```python
anyone_in_germany = (
    Document
    .select(Document.bibcode)
    .where((Document.orcid == who) & (Document.affiliation.country_code == "DE"))
)
[paper.bibcode for paper in anyone_in_germany]
# -> ['2006MNRAS.372..961K']
```

Inside `any(...)` the same two conditions find nothing, which is the right answer:

```python
in_germany = (
    Document
    .select(Document.bibcode)
    .where(
        Document.authors.any(
            (Author.orcid == who) & (Author.affiliation.country_code == "DE")
        )
    )
)
[paper.bibcode for paper in in_germany]
# -> []
```

## With `ads`

The `ads` package returns the ORCID lists, `aff` and `aff_id` as separate lists. You would find the
position of the iD in whichever ORCID list holds it, take the entry at that position in `aff_id`, split it
when it names several institutions, and look up names, countries and parents in a table of your own.

## Things to know

- A paper shows where its authors were when it was written, which can be a year or more before the
  publication date. The years are those of the papers, not of the appointments.
- Only papers that carry the ORCID iD are found, and only curated affiliations are listed. For the rest,
  read `author.aff`, the affiliation as the paper wrote it.
- A person can give two institutions on one paper: a visit, a joint appointment or a move in progress.
  Both then appear for that year.
