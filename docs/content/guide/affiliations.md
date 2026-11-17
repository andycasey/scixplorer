---
title: Affiliations
weight: 6
description: Find institutions in SciX's curated affiliation data, walk their hierarchy, and search for documents by institution or country.
---

After this page you can find an institution's curated identifier, move through its parents and children,
search for documents by institution or country, and read the affiliations of a document's authors.

## The affiliation data

SciX identifies the institutions in authors' affiliations with curated identifiers such as `A00211`
(Harvard University). `Affiliation` holds those institutions: 7382 of them, packaged with
scixplorer and searched in Python, so no request and no token are needed.

```python
from scixplorer import *

harvard = Affiliation.get(abbreviation="Harvard U")
harvard
# -> <Affiliation: id='A00211' (Harvard U)>
harvard.canonical_name
# -> Harvard University, Massachusetts
```

| Field | Meaning |
|---|---|
| `id` | the curated identifier, such as `"A00211"` |
| `abbreviation` | the short name, such as `"Harvard U"` |
| `canonical_name` | the full name, such as `"Harvard University, Massachusetts"` |
| `country` | the country, spelled out as the upstream data spells it, or `None` |
| `country_code` | the ISO 3166-1 alpha-2 code of that country, such as `"US"`, or `None` |
| `parent_ids` | the identifiers of the parent institutions (empty for a top-level one) |

`Affiliation.lookup()` finds one by identifier, or returns `None`:

```python
Affiliation.lookup("A01400")
# -> <Affiliation: id='A01400' (CfA)>
```

## Country and country code

`country` holds the name as the upstream data writes it, for example `"United States"` or
`"United Kingdom"`, and those spellings can change between releases of the data. A condition such as
`country == "USA"` matches nothing. Filter on `country_code` for a condition that stays stable:

```python
Affiliation.select().where(Affiliation.country == "USA").count()
# -> 0
Affiliation.select().where(Affiliation.country_code == "AU").count()
# -> 118
```

## The hierarchy

Institutions form a hierarchy: a department belongs to a university, and some institutions have more
than one parent. The Harvard-Smithsonian Center for Astrophysics has two:

```python
cfa = Affiliation.lookup("A01400")
cfa.parents
# -> [<Affiliation: id='A00211' (Harvard U)>, <Affiliation: id='A01397' (SI)>]
cfa.parent
# -> <Affiliation: id='A00211' (Harvard U)>
```

| Attribute | Value |
|---|---|
| `parents` | the parent institutions, as a list |
| `parent` | the first parent, or `None` |
| `children` | a query for the institutions that list this one as a parent |
| `siblings` | a query for the other institutions that share a parent with this one |

`children` and `siblings` are queries, so you can filter and count them:

```python
harvard.children.count()
# -> 13
```

## Search for documents by affiliation

A condition on `Document.affiliation` is resolved against the affiliation data before the search is
sent, and becomes a condition on the documents' `aff_id`:

```python
search.to_solr(Document.affiliation == cfa)
# -> aff_id:"A01400"
query = Document.select(Document.bibcode).where(Document.affiliation.country_code == "AU")
[paper.bibcode for paper in query]
# -> ['1998AJ....116.1009R']
```

A condition on an institution matches only that identifier, not its children. To include the
departments of a university, list them, or use SciX's `inst` field, which searches an institution
together with its children:

```python
search.to_solr(Document.affiliation.in_([harvard, *harvard.children]))
# -> aff_id:("A00211" OR "A00212"
search.to_solr(Document.inst == "CfA")
# -> inst:"CfA"
```

### Several conditions on one affiliation

Each condition through `Document.affiliation` is resolved on its own, so two conditions can be met by
two different affiliations. To find papers from a physics department in Australia, put both conditions in
one `.any(...)`:

```python
one = Document.affiliation.any(
    (Affiliation.country_code == "AU") & (Affiliation.abbreviation == "Dep Phy")
)
search.to_solr(one)
# -> aff_id:("A11677" OR "A00307" OR "A11671" OR "A00733")
```

The same conditions written separately mean "some affiliation is in Australia, and some affiliation is a
physics department anywhere". They match a paper with an Australian astronomer and a co-author in a German
physics department, and send two long lists instead of four identifiers:

```python
separate = (Document.affiliation.country_code == "AU") & (Document.affiliation.abbreviation == "Dep Phy")
search.to_solr(separate).count(" OR ") + 2
# -> 324
```

`Document.affiliation` matches if *any* author has a matching affiliation. To tie an affiliation to
a particular author (a name, or a position), use [`Document.authors`]({{< ref "guide/authors" >}}).

Other ways to search affiliations, which the client passes to SciX as they are:

| Field | Searches |
|---|---|
| `aff` | the affiliation strings as written in the papers |
| `aff_id` | curated identifiers directly |
| `inst` | an institution's abbreviation, including its children |
| `affil` | all of the above at once |

## Limits

- One condition may match at most 1000 affiliations, because their identifiers must fit in one search
  request. A larger match raises `UnsupportedQueryError` when the search is planned; narrow the condition.
  For example, `country_code == "US"` matches 1773 institutions and is refused. See
  [Errors and limits]({{< ref "guide/errors-and-limits#related-values" >}}).
- The curated data is incomplete. Many affiliation strings in SciX have no curated identifier (`-` in
  `aff_id`), and some institutions have no country. A search by affiliation finds only documents whose
  affiliations SciX has identified.
- Identifiers on a document that are missing from the packaged data are skipped in `doc.affiliation`. A
  newer copy of the data may know them: run `data.refresh("affiliations")`.

## Affiliations of a document

`doc.affiliation` has one entry per author: `None` for an author with no curated affiliation, an
`Affiliation`, or a tuple of them for an author with several. `utils.flatten()` turns it into a flat
list:

```python
paper = Document.get(bibcode="2015ApJ...808...16N")
paper.affiliation[1]
# -> (<Affiliation: id='A00951' (NYU)>, <Affiliation: id='A01570' (MPIA Heidelberg)>)
sorted({affiliation.country_code for affiliation in utils.flatten(paper.affiliation)})
# -> ['DE', 'US']
```

`doc.authors[i].affiliation` gives the same per author, always as a tuple; see
[Authors]({{< ref "guide/authors#read-the-authors-of-a-document" >}}).

## Data licence

The affiliation data comes unmodified from the
[adsabs/CanonicalAffiliations](https://github.com/adsabs/CanonicalAffiliations) repository and is licensed
under the GPL-3.0, not under scixplorer's MIT licence. `data.notices()` returns the details. See
[Reference data]({{< ref "guide/reference-data" >}}).
