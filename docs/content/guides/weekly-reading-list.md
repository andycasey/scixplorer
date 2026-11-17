---
title: A weekly reading list by topic
weight: 9
description: Find the papers SciX added this week on a thesaurus concept and everything narrower, and never see the same paper twice.
---

After this page you can run one search each week that finds the new papers on a topic, including its
narrower topics, leaves out the papers earlier runs already found, and records the new ones in a library.

## The question

"What is new on my topic since I last looked?"

Three parts make up the answer: the topic, as a concept of the Unified Astronomy Thesaurus together with
the concepts below it; the date SciX added each paper; and a library of the papers you have already seen.

## The topic and everything narrower

Download the thesaurus once (see [Vocabularies]({{< ref "usage/vocabularies" >}})), then find your
concept by searching the names:

```python
from scixplorer import *

results = data.refresh("uat")

found = (
    UATConcept
    .select()
    .where(UATConcept.name.contains("exoplanets"))
)
found.all()
# -> <UATConcept: id=498 (Exoplanets)>
```

A condition on a concept matches only that concept. `descendants` lists every narrower one, so put them
all in the condition:

```python
exoplanets = UATConcept.lookup(498)
[concept.name for concept in exoplanets.descendants]
# -> ['Habitable planets', 'Hot Jupiters', 'Mini Neptunes', 'Super Earths']

topic = Document.uat_concepts.in_([exoplanets, *exoplanets.descendants])
search.to_solr(topic)
# -> uat_id:(498 OR 695 OR 753 OR 1063 OR 1655)
```

## The papers you have already seen

Keep them in a library. Create it once; on later runs fetch it with `Library.get(name="Seen: exoplanets")`:

```python
seen = Library.create(
    name="Seen: exoplanets",
    description="Papers the weekly search has already reported",
    documents=["1995Natur.378..355M"],
)
```

## The search

`entry_date` is the date SciX added the record, which is what "new" means here; a paper's publication date
can be months earlier or later. `days_ago(7)` is worked out by SciX when the search runs:

```python
def new_on_topic(since):
    return (
        Document
        .select(
            Document.bibcode,
            Document.title,
            Document.entry_date,
        )
        .where(topic & (Document.entry_date >= since) & Document.not_in(seen))
        .order_by(Document.entry_date.desc())
    )

this_week = new_on_topic(days_ago(7))
search.to_solr(this_week)
# -> uat_id:(498 OR 695 OR 753 OR 1063 OR 1655) AND entry_date:[NOW-7DAYS TO *] AND -docs(library/
```

The sample of papers on this site has nothing from the last week, so here is the same search from a fixed
date:

```python
import datetime

since_1999 = new_on_topic(datetime.datetime(1999, 1, 1))
[(paper.bibcode, paper.title[0]) for paper in since_1999]
# -> [('2000ApJ...529L..45C', 'Detection of Planetary Transits Across a Sun-like Star')]
```

The 1995 paper on the topic is left out because it is in the library.

## Record what you found

Add the results to the library, so that the next run does not report them again. `add()` accepts the
query and returns the number of documents SciX added:

```python
seen.add(since_1999)
# -> 1
```

With the library in place the date matters less. Search the last 30 days every week: a paper SciX indexed
late, or a week you missed, is still found, and nothing is reported twice.

## Variations

| To | Add to the condition |
|---|---|
| leave out preprints | `& (Document.doctype != "eprint")` |
| keep refereed papers only | `& (Document.property == "refereed")` |
| follow certain journals | `& Document.journal.in_(["ApJ", "AJ", "MNRAS"])` |
| follow several topics | put the concepts of all of them in the one `in_([...])` |

To run the search every week, call the script from a scheduler such as cron, with your token in the
`SCIX_API_TOKEN` environment variable.

## With `ads`

The `ads` package has no thesaurus and does not manage libraries. You could search the `uat` field for the
name of one concept, but not for everything below it, and you would keep the list of papers already seen
in a file of your own.

## Things to know

- SciX assigns thesaurus concepts mostly to recent papers from the journals that use the thesaurus. A
  paper without a concept is not found; add a text search with `|`, such as
  `topic | (Document.abs == "hot Jupiters")`, to widen the net.
- One condition may name at most 1000 concepts; see
  [Vocabularies]({{< ref "usage/vocabularies#limits" >}}).
- `docs(library/<id>)` is not yet confirmed against the live API; see
  [Libraries]({{< ref "usage/libraries#search-within-a-library" >}}) for the setting that fetches the
  library's bibcodes instead.
- A preprint and its published version are two records at first, which SciX later merges. Leaving out
  preprints avoids reading a paper twice, at the cost of seeing it later.
