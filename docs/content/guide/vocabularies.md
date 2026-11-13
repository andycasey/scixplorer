---
title: Vocabularies
weight: 7
description: Use the Unified Astronomy Thesaurus and the planetary nomenclature gazetteer to read and search the concepts and features SciX assigns to documents.
---

After this page you can download the two controlled vocabularies SciX uses, look up concepts and features
in them, read the ones assigned to a document, and search for documents by them.

SciX tags records with concepts from the [Unified Astronomy Thesaurus](https://astrothesaurus.org) (UAT)
and with features from the IAU/USGS
[Gazetteer of Planetary Nomenclature](https://planetarynames.wr.usgs.gov). In scixplorer these are
`scix.UATConcept` and `scix.PlanetaryFeature`.

## Download them first

Neither vocabulary is packaged with scixplorer, because of their licences and size. Download them once;
they are kept in your cache directory and used from then on:

```python
import scixplorer as scix

results = scix.data.refresh("uat", "planetary_features")
[(result.name, result.ok) for result in results]
# -> [('uat', True), ('planetary_features', True)]
```

The downloads come from GitHub (about 1.5 MB and 0.9 MB) and never carry your API token. Until you have
downloaded a vocabulary, anything that needs it raises `scix.exceptions.DataNotAvailable`, whose message
says what to run. See [Reference data]({{< ref "guide/reference-data" >}}) for where the files are kept and
how to update them.

You do not need the download to search by identifier: `scix.Document.uat == 498` is sent as it is.

## UAT concepts

Each concept has a number, such as 498 for Exoplanets:

```python
exoplanets = scix.UATConcept.lookup(498)
exoplanets
# -> <UATConcept: id=498 (Exoplanets)>
exoplanets.alt_names
# -> ('Extrasolar planets', 'Planets', 'Planets (Extrasolar)')
```

| Field | Meaning |
|---|---|
| `id` | the concept number, as stored in SciX's `uat_id` |
| `name` | the preferred name |
| `alt_names` | alternative names |
| `parent_ids` | the numbers of the broader concepts |
| `uri` | the concept's URI, such as `http://astrothesaurus.org/uat/498` |
| `deprecated` | whether the concept has been withdrawn |

Search them like any model. `name` and `alt_names` are matched like SciX text: `==` finds the words as a
phrase, ignoring case, and `.exact()` compares the whole name:

```python
scix.UATConcept.select().where(scix.UATConcept.alt_names == "extrasolar planets").all()
# -> [<UATConcept: id=498 (Exoplanets)>]
```

The thesaurus is a hierarchy in which a concept can have several broader concepts:

| Attribute | Value |
|---|---|
| `parents` | the broader concepts |
| `children` | a query for the narrower concepts |
| `ancestors` | every broader concept, nearest first |
| `descendants` | every narrower concept, nearest first |

```python
exoplanets.parents
# -> [<UATConcept: id=486 (Exoplanet astronomy)>]
[concept.name for concept in exoplanets.children]
# -> Hot Jupiters
```

## Planetary features

Each feature has the gazetteer's identifier, such as 2071 for Gale crater on Mars:

```python
gale = scix.PlanetaryFeature.lookup(2071)
gale.to_dict()
# -> {'id': 2071, 'name': 'Gale', 'target': 'Mars', 'feature_type': 'Crater', 'approval_status': 'Approved', 'approval_year': 1991}
```

| Field | Meaning |
|---|---|
| `id` | the gazetteer's feature id, as stored in SciX's `planetary_feature_id` |
| `name` | the name without diacritics, such as `"Gale"` |
| `target` | the body it is on, such as `"Mars"` or `"Moon"` |
| `feature_type` | the descriptor in the singular, such as `"Crater"` or `"Mons"` |
| `approval_status` | `"Approved"`, `"Dropped"` or `"Not Approved"` |
| `approval_year` | the year the IAU approved the name, or `None` |

`name` is matched like text, so `name == "Copernicus"` also finds the lunar features "Copernicus A",
"Copernicus B" and so on; use `.exact("Copernicus")` for the whole name. The other fields are matched
exactly, including case. Names are not unique across bodies:

```python
scix.PlanetaryFeature.select().where(scix.PlanetaryFeature.name.exact("Copernicus")).all()
# -> [<PlanetaryFeature: id=1297 (Copernicus, Mars)>, <PlanetaryFeature: id=1296 (Copernicus, Moon)>]
```

## Concepts and features of a document

`doc.uat` and `doc.planetary_feature` are lists of the concepts and features assigned to a document. They
are built from `uat_id` and `planetary_feature_id`; identifiers missing from your copy of the vocabulary
are skipped, and a document with none gives an empty list:

```python
paper = scix.Document.get(bibcode="2010GeoRL..3704201M")
paper.planetary_feature
# -> [<PlanetaryFeature: id=2071 (Gale, Mars)>]
paper.uat
# -> []
```

SciX also stores the names as text, in the Solr fields `uat` and `planetary_feature`. In scixplorer those
are `uat_name` and `planetary_feature_name`, so that `uat` and `planetary_feature` can be the
relations:

```python
paper.planetary_feature_name
# -> ['Gale']
```

## Search for documents by concept or feature

A condition through `scix.Document.uat` or `scix.Document.planetary_feature` is resolved against the
downloaded vocabulary, and becomes a condition on the identifiers:

```python
scix.search.to_solr(scix.Document.uat.name == "Hot Jupiters")
# -> uat_id:753
scix.search.to_solr(scix.Document.uat == exoplanets)
# -> uat_id:498
```

To include narrower concepts, list them:

```python
scix.search.to_solr(scix.Document.uat.in_([exoplanets, *exoplanets.descendants]))
# -> uat_id:(498 OR 
```

### Several conditions on one concept or feature: `.any()`

Each condition through a relation is resolved on its own. Two conditions therefore mean "some feature
meets the first, and some feature meets the second", and those can be different features. `.any(...)`
makes everything inside it about one feature.

Compare a search for papers about the Martian crater Copernicus, written both ways:

```python
separate = (scix.Document.planetary_feature.target == "Mars") & (
    scix.Document.planetary_feature.name.exact("Copernicus")
)
scix.search.to_solr(separate)
# -> planetary_feature_id:(16115 OR 1297 OR 2071 OR 14300 OR 4453 OR 6288) AND planetary_feature_id:(1297 OR 1296)
one_feature = scix.Document.planetary_feature.any(
    (scix.PlanetaryFeature.target == "Mars") & scix.PlanetaryFeature.name.exact("Copernicus")
)
scix.search.to_solr(one_feature)
# -> planetary_feature_id:1297
```

The first also matches a paper about Gale crater on Mars that mentions the lunar crater Copernicus. The
second matches only papers about the Martian Copernicus. The identifier lists come from your copy of the
gazetteer, so the first list is longer with the full data.

`.any()` works on every relation: `scix.Document.uat`, `scix.Document.journal`,
`scix.Document.affiliation`, and `scix.Author.affiliation` inside
[`scix.Document.authors.any()`]({{< ref "guide/authors" >}}). There is no `.all()` on a relation: "every
related feature" cannot be searched, and `.all` raises an `AttributeError` that says so.

```python
query = scix.Document.select(scix.Document.bibcode).where(
    scix.Document.planetary_feature.any(
        (scix.PlanetaryFeature.target == "Mars") & (scix.PlanetaryFeature.feature_type == "Crater")
    )
)
[paper.bibcode for paper in query]
# -> ['2010GeoRL..3704201M']
```

A condition directly on `scix.UATConcept` or `scix.PlanetaryFeature` fields can also be used in a document
search, and is read as going through the relation.

At most 1000 concepts or features may match one condition; see
[Errors and limits]({{< ref "guide/errors-and-limits#related-values" >}}).

## Licences

The UAT is published by the American Astronomical Society under CC BY-SA 3.0. The gazetteer is in the
public domain (IAU and USGS), downloaded from an MIT-licensed adsabs repository. Neither is distributed with
scixplorer; `scix.data.refresh()` downloads them for you.
