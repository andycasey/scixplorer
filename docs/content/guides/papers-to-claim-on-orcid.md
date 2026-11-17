---
title: Papers to claim on ORCID
weight: 4
description: Find the papers that are probably yours but do not carry your ORCID iD yet, and collect them for claiming.
---

After this page you can list the papers that match your name and your institutions but do not carry your
ORCID iD, put them in a library to check, and export them for your ORCID record.

## The question

"Which of my papers does SciX not yet connect to my ORCID iD?"

A search by ORCID iD cannot answer this, because it finds only the papers that already carry the iD. The
missing ones have to be found by name, and a name alone also finds other people. So ask for your name
*at your institutions*, and take away what is already claimed.

## What is already claimed

`Document.orcid` searches all three places SciX keeps ORCID iDs: those sent by publishers, those from
users' public ORCID profiles, and those claimed in ADS or SciX itself.

```python
from scixplorer import Affiliation, Author, Document, Library, export

me = "0000-0001-5082-6693"

claimed = (
    Document
    .select(Document.bibcode)
    .where(Document.orcid == me)
)
[paper.bibcode for paper in claimed]
# -> ['2015ApJ...808...16N']
```

## Your name at your institutions, without your iD

The per-author condition ties the name to the institution, as in
[One author among namesakes]({{< ref "guides/one-author-among-namesakes" >}}). The ORCID condition is
about the whole document, so it goes outside `any(...)`:

```python
mpia = Affiliation.get(abbreviation="MPIA Heidelberg")

mine = Document.authors.any((Author.name == "Ness, M") & (Author.affiliation == mpia))
unclaimed = (
    Document
    .select(
        Document.bibcode,
        Document.title,
    )
    .where(mine & ~(Document.orcid == me))
)
[(paper.bibcode, paper.title[0]) for paper in unclaimed]
# -> [('2016arXiv160303040C', 'The Cannon 2: A data-driven model of stellar spectra for detailed chemical abundance analyses')]
```

SciX is sent the name, the institution and the missing iD, and the client then checks that the name and
the institution belong to the same author:

```python
print(unclaimed.explain())
# -> q = author:"Ness, M" AND aff_id:"A01570" AND -orcid:"0000-0001-5082-6693"
```

If you have worked at several institutions, list them all, each with its departments:
`Author.affiliation.in_([mpia, columbia, *columbia.children])`.

## Collect them

Put the candidates in a library, so that you can look through them in SciX before claiming anything:

```python
to_claim = Library.create(
    name="To claim on ORCID",
    description="Papers with my name and institutions but without my ORCID iD",
    documents=unclaimed,
)
to_claim.num_documents
# -> 1
```

ORCID can also import works from a BibTeX file, and `export.bibtex()` accepts the query or the library:

```python
print(export.bibtex(to_claim))
# -> @ARTICLE{2016arXiv160303040C,
```

## Where an iD came from

To see which of the three sources connects you to a paper, read the author's ORCID fields. Each is `None`
when that source has no iD for the author:

```python
paper = Document.get(bibcode="2015ApJ...808...16N")
author = paper.authors[0]
(author.name, author.orcid_pub, author.orcid_user, author.orcid_other)
# -> ('Ness, M.', '0000-0001-5082-6693', None, None)
```

`author.orcid` is the first of the three that is given.

## With `ads`

In the `ads` package you can write `author:"Ness, M" aff_id:A01570 -orcid:...` by hand, once you have
looked up the identifier. It cannot require that the name and the institution belong to the same author,
so a paper by another M. Ness comes back too whenever one of its other authors is at your institute.

## Things to know

- The list holds candidates, not certainties: a namesake at the same institution still matches. Check
  each paper before you claim it.
- Papers on which SciX has no curated institution for you are not found. Search for them with
  `Author.aff`, the affiliation as the paper wrote it, or by name and year range.
- The client compares names without SciX's curated list of name variants, so a paper under a former name
  needs its own `Author.name` condition; join the two with `|` inside `any(...)`.
