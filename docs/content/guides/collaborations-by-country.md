---
title: Collaborations by country
weight: 1
description: List the co-authors of one researcher by the country of their institution, leaving out the countries you do not have to report.
---

After this page you can take one ORCID iD and a range of years, and produce a list of countries, the
co-authors who gave an institution there, and the papers you wrote with them. This is the list a
foreign-collaboration disclosure asks for.

## The question

"Which co-authors have I had in the last five years at institutions abroad, except in a list of countries
I do not have to report?"

The answer needs three things together: your papers, each co-author's own institution on each paper, and
the country of that institution. scixplorer gives the second as [`Author`]({{< ref "usage/authors" >}})
objects and the third from the packaged [affiliation data]({{< ref "usage/affiliations" >}}).

## Find the papers

Search by ORCID iD and year. `select()` with no fields fetches every stored field, which includes the
ones `paper.authors` is built from:

```python
from scixplorer import *

me = "0000-0003-2866-9403"

papers = (
    Document
    .select()
    .where((Document.orcid == me) & Document.year.between(2012, 2016))
    .order_by(Document.year)
)
[paper.bibcode for paper in papers]
# -> ['2013PASP..125..306F', '2015ApJ...808...16N', '2016arXiv160303040C']
```

The years are fixed here so that the example always gives the same answer. For the last five years, write
`Document.year >= datetime.date.today().year - 5`.

## Group the co-authors by country

Each author of each paper has a tuple of curated affiliations, and each affiliation has a `country` and a
`country_code`. Skip yourself, skip the countries you do not report, and keep what is left:

```python
from collections import defaultdict

exempt = {"US"}

report = defaultdict(lambda: defaultdict(list))
unknown = defaultdict(list)
for paper in papers:
    for author in paper.authors:
        if author.orcid == me:
            continue
        name = paper.author_norm[author.position]
        if not author.affiliation:
            unknown[name].append(paper.bibcode)
        for affiliation in author.affiliation:
            if affiliation.country_code not in exempt:
                report[affiliation.country][name].append(paper.bibcode)

sorted(report)
# -> ['Germany', 'United Kingdom']
dict(report["Germany"])
# -> {'Ness, M': ['2015ApJ...808...16N', '2016arXiv160303040C'], 'Rix, H': ['2015ApJ...808...16N', '2016arXiv160303040C']}
dict(report["United Kingdom"])
# -> {'Casey, A': ['2016arXiv160303040C'], 'Gilmore, G': ['2016arXiv160303040C']}
```

`author_norm` is SciX's short form of each name, `"Lastname, F"`. It is the key here because the same
person is written differently from paper to paper (`"Ness, M."` on one, `"Ness, Melissa"` on the next).

`unknown` holds the co-authors SciX has no curated institution for. They need a look by hand, because
their country is not known:

```python
dict(unknown)
# -> {'Lang, D': ['2013PASP..125..306F']}
```

## A longer list of exempt countries

The exempt countries are a set of ISO 3166-1 alpha-2 codes, so build the set you need. scixplorer has no
list of the members of the European Union or of any other group; write them out:

```python
eu = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}
exempt = {"AU", "NZ", "CA", "GB", "US"} | eu

outside = {
    affiliation.country
    for paper in papers
    for author in paper.authors
    for affiliation in author.affiliation
    if affiliation.country_code not in exempt
}
outside
# -> set()
```

With that list nothing in the sample is left to report: every curated institution on these three papers is
in the United States, Germany or the United Kingdom.

## Only the papers with a co-author in certain countries

When you have a short list of countries to look *for*, put it in the search. Inside
`Document.authors.any(...)` both conditions are about the same author, so your own affiliations do not
count:

```python
watched = (
    Document
    .select(Document.bibcode)
    .where(
        (Document.orcid == me)
        & Document.authors.any(
            (Author.orcid != me) & Author.affiliation.country_code.in_(["GB", "CH"])
        )
    )
)
[paper.bibcode for paper in watched]
# -> ['2016arXiv160303040C']
```

SciX narrows the search to your papers that have any author at an institution in those countries, and the
client then checks each one for a co-author who is there (see
[where conditions are checked]({{< ref "usage/authors#where-conditions-are-checked" >}})).

This form does not work for "every country except a few". The client sends the countries as a list of
institutions, at most 1000 of them, and the institutions outside any short list of countries number in
the thousands. That is why the report above searches by ORCID iD and sorts the authors in Python.

## With `ads`

The `ads` package returns `author`, `aff` and `aff_id` as three separate lists. You would line them up by
position yourself, split the entries that hold several institutions, skip the `-` placeholders, and bring
your own table from institution identifier to country.

## Things to know

- The country is that of the institution the author gave **on that paper**, not where they work now.
- Only curated affiliations have a country. An affiliation string SciX has not identified gives no
  `Affiliation`, and a few institutions in the data have no country: for those `country` is `None`.
- A search by ORCID iD finds only the papers that carry the iD. See
  [Papers to claim on ORCID]({{< ref "guides/papers-to-claim-on-orcid" >}}) for finding the rest.
- `"Lastname, F"` can stand for two people. Where it matters, compare `author.orcid` as well.
