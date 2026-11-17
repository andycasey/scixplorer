---
title: Literature for a landing site
weight: 10
description: Collect what has been written about named places on a planet or moon, chosen by body, kind of feature or the year they were named.
---

After this page you can find the papers about one named surface feature, about several candidate sites at
once, or about a whole class of features, and sort the papers by site.

## The question

"What has been written about the candidate landing sites?"

SciX tags records with features from the IAU/USGS Gazetteer of Planetary Nomenclature. In scixplorer each
feature is a `PlanetaryFeature` with a body, a kind and the year its name was approved, so you can choose
features by those properties and search for the papers about them.

## Find the sites

Download the gazetteer once (see [Vocabularies]({{< ref "usage/vocabularies" >}})). Names are not unique:
Copernicus is a crater on the Moon and another on Mars. So give the body as well as the name, and compare
the whole name with `.exact()`:

```python
from scixplorer import Document, Library, PlanetaryFeature, data, export, search

results = data.refresh("planetary_features")

candidates = (
    PlanetaryFeature
    .select()
    .where(
        (PlanetaryFeature.target == "Mars")
        & (PlanetaryFeature.name.exact("Gale") | PlanetaryFeature.name.exact("Jezero"))
    )
)
sites = candidates.all()
sites
# -> [<PlanetaryFeature: id=2071 (Gale, Mars)>, <PlanetaryFeature: id=14300 (Jezero, Mars)>]
```

## Papers about the sites

`Document.planetary_features.in_(...)` takes the features themselves. Add any other conditions, and sort
as you like:

```python
papers = (
    Document
    .select()
    .where(Document.planetary_features.in_(sites) & (Document.year >= 2005))
    .order_by(Document.citation_count.desc())
)
search.to_solr(papers)
# -> planetary_feature_id:(2071 OR 14300) AND year:[2005 TO *]
[(paper.bibcode, paper.title[0]) for paper in papers]
# -> ('2015GeoRL..42..264E', "Mineralogy and fluvial history of the watersheds of Gale, Knobel, and Sharp craters
```

## Sort the papers by site

A paper can be about several features. `paper.planetary_features` lists them, so group the results by the
sites you asked for:

```python
from collections import defaultdict

by_site = defaultdict(list)
for paper in papers:
    for feature in paper.planetary_features:
        if feature in sites:
            by_site[feature.name].append(paper.bibcode)

dict(by_site)
# -> {'Gale': ['2015GeoRL..42..264E']}
```

## A class of features

To describe the features instead of naming them, put the conditions in one `.any(...)`, so that they are
all about the same feature. This asks for papers about a Martian crater whose name was approved in 1990
or later:

```python
recent_craters = (
    (PlanetaryFeature.target == "Mars")
    & (PlanetaryFeature.feature_type == "Crater")
    & (PlanetaryFeature.approval_year >= 1990)
)

named = (
    PlanetaryFeature
    .select()
    .where(recent_craters)
    .order_by(PlanetaryFeature.approval_year)
)
[(feature.name, feature.approval_year) for feature in named]
# -> ('Gale', 1991)

about_them = (
    Document
    .select(Document.bibcode)
    .where(Document.planetary_features.any(recent_craters))
)
[paper.bibcode for paper in about_them]
# -> ['2015GeoRL..42..264E']
```

Run the search on `PlanetaryFeature` first, as here, to see which features the conditions select and
how many there are.

## Narrow by subject

The feature condition combines with a text search like any other condition:

```python
fluvial = (
    Document
    .select(Document.bibcode)
    .where(Document.planetary_features.in_(sites) & (Document.abs == "fluvial history"))
)
[paper.bibcode for paper in fluvial]
# -> ['2015GeoRL..42..264E']
```

## Keep the result

Put the papers in a library to share with the team, or export them:

```python
library = Library.create(name="Candidate landing sites", documents=papers)
library.num_documents
# -> 1
print(export.bibtex(papers))
# -> @ARTICLE{2015GeoRL..42..264E,
```

## With `ads`

In the `ads` package you could search the `planetary_feature` field for a name as text. That finds the
Copernicus on the Moon together with the one on Mars, and it cannot choose features by body, by kind or
by the year they were named.

## Things to know

- One condition may select at most 1000 features, because their identifiers must fit in one request. Mars
  alone has more than 1000 named craters, so "every Martian crater" is refused with
  `UnsupportedQueryError`. Narrow the features by name, by a rarer kind or by approval year; see
  [Vocabularies]({{< ref "usage/vocabularies#limits" >}}).
- Only records that SciX has tagged with a feature are found. To widen the search, join a text condition
  with `|`, such as `Document.planetary_features.in_(sites) | (Document.abs == "Gale crater")`.
- `feature_type` is the gazetteer's descriptor in the singular, such as `"Crater"`, `"Mons"` or
  `"Vallis"`, and is matched exactly, including case.
