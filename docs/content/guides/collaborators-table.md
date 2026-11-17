---
title: A collaborators table
weight: 2
description: Build the table of recent co-authors that grant applications ask for, with each person's latest institution and the date you last published together.
---

After this page you can produce the table of collaborators that funding agencies ask for: every
co-author over a period, with the institution they most recently gave and the date of your last paper
together. The NSF's Collaborators and Other Affiliations template, which covers the last 48 months, is one
such table.

## The question

"Who have I written a paper with in the last 48 months, where were they when we last did, and when was
that?"

## Find the papers, newest first

Search by ORCID iD and publication date, and sort by date with the newest first. Leave out the papers of
large collaborations if their author lists would swamp the table:

```python
from scixplorer import *
import datetime

me = "0000-0003-2866-9403"

papers = (
    Document
    .select()
    .where(
        (Document.orcid == me)
        & Document.pubdate.between(datetime.date(2012, 4, 1), datetime.date(2016, 3, 31))
        & (Document.author_count <= 50)
    )
    .order_by(Document.pubdate.desc())
)
search.to_solr(papers)
# -> orcid:"0000-0003-2866-9403" AND pubdate:[2012-04 TO 2016-03] AND author_count:[* TO 50]
papers.count()
# -> 3
```

The dates are fixed so that the example always gives the same answer. A publication date counts to the
month, so for the last 48 months give the first day of the month four years ago as the lower bound.

## Keep the latest entry for each co-author

Because the newest paper comes first, the first time a co-author appears is the last time you published
together. `setdefault` keeps that entry and ignores the older ones:

```python
table = {}
for paper in papers:
    for author in paper.authors:
        if author.orcid == me:
            continue
        organisation = "; ".join(a.canonical_name for a in author.affiliation) or author.aff or "unknown"
        key = paper.author_norm[author.position]
        table.setdefault(key, (author.name, organisation, paper.pubdate))

len(table)
# -> 9
table["Ness, M"]
# -> ('Ness, Melissa', 'Max-Planck-Institute for Astronomy, Heidelberg', datetime.date(2016, 3, 1))
table["Lang, D"]
# -> ('Lang, Dustin', 'unknown', datetime.date(2013, 3, 1))
```

Each author's `affiliation` is a tuple of curated institutions, and `canonical_name` is the institution's
full name. An author SciX has no curated institution for falls back to `author.aff`, the affiliation as
the paper wrote it, and then to `"unknown"`.

## Write the table

The rows are plain tuples, so any writer will do. As CSV, sorted by name:

```python
import csv
import io

out = io.StringIO()
writer = csv.writer(out)
writer.writerow(["Name", "Organisation", "Last active"])
writer.writerows(
    (name, organisation, f"{date:%m/%Y}")
    for name, organisation, date in sorted(table.values())
)
print(out.getvalue())
# -> Name,Organisation,Last active
# -> "Casey, Andrew R.","University of Cambridge, Institute of Astronomy",03/2016
# -> "Foreman-Mackey, Daniel",New York University,03/2013
```

## With `ads`

The `ads` package gives `author`, `aff` and `aff_id` as separate lists and `pubdate` as a string such as
`"2016-03-00"`. You would pair the lists by position, turn the identifiers into institution names with a
table of your own, and parse the dates.

## Things to know

- The table lists the institution given **on the last paper together**. A co-author who has since moved
  appears at their old institution until you publish together again.
- The key is SciX's short form of the name, `"Lastname, F"`, so that `"Ness, M."` and `"Ness, Melissa"` are
  one row. Two people who share a surname and a first initial would also become one row; compare
  `author.orcid` where that matters.
- Only papers that carry your ORCID iD are found. See
  [Papers to claim on ORCID]({{< ref "guides/papers-to-claim-on-orcid" >}}) for finding the rest.
- [Collaborations by country]({{< ref "guides/collaborations-by-country" >}}) groups the same co-authors by
  the country of their institution.
