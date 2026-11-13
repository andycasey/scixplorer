---
title: Journals
weight: 5
description: Look up journals by abbreviation or title, read a document's journal, and search for documents by journal.
---

After this page you can find a journal's abbreviation (its *bibstem*) from its title, read the journal of a
document, and search for documents by journal title without knowing the abbreviations.

## The journal list

`scix.Journal` holds the journals and other publication series SciX knows, each identified by its
abbreviation. The list (4502 journals) is packaged with scixplorer and searched in Python, so no
request and no token are needed:

```python
import scixplorer as scix

scix.Journal.get(abbreviation="ApJ").title
# -> The Astrophysical Journal
scix.Journal.select().count()
# -> 4502
```

| Field | Meaning |
|---|---|
| `abbreviation` | the bibstem, such as `"ApJ"` or `"MNRAS"`: the journal part of a bibcode without the padding dots |
| `title` | the full title, such as `"The Astrophysical Journal"` |

Search the list with the same expressions as documents. `==` compares the whole value, including case;
`like()`, `startswith()` and `contains()` ignore case:

```python
planetary = scix.Journal.select().where(scix.Journal.title.contains("planetary")).order_by(scix.Journal.abbreviation)
[journal.abbreviation for journal in planetary.limit(4)]
# -> ['AREPS', 'AdEPS', 'CRASE', 'CoLPL']
```

`scix.Journal.lookup()` finds one journal by abbreviation, and returns `None` if there is none:

```python
scix.Journal.lookup("MNRAS").title
# -> Monthly Notices of the Royal Astronomical Society
scix.Journal.lookup("NotAJournal") is None
# -> True
```

The packaged list is a snapshot of the ADS journal list taken in 2021. To replace it with the current
list, run `scix.data.refresh("journals")`; see [Reference data]({{< ref "guide/reference-data" >}}).

## The journal of a document

`doc.journal` is the `scix.Journal` named by the journal part of the document's bibcode (or, if the
bibcode cannot be parsed, its first `bibstem`), or `None` if the abbreviation is not in the list:

```python
paper = scix.Document.get(bibcode="2013PASP..125..306F")
paper.journal
# -> <Journal: abbreviation='PASP'>
paper.journal.title
# -> Publications of the Astronomical Society of the Pacific
```

This needs no request: it is computed from the bibcode.

## Search for documents by journal

A condition on `scix.Document.journal` is resolved against the journal list before the search is sent,
and becomes a condition on the documents' `bibstem`:

```python
scix.search.to_solr(scix.Document.journal.title.startswith("The Astrophysical Journal"))
# -> bibstem:("ApJ" OR "ApJS")
```

To match one journal, compare the relation with an abbreviation or a `scix.Journal` object. An
abbreviation is used as given, even one missing from the packaged list:

```python
scix.search.to_solr(scix.Document.journal == "AJ")
# -> bibstem:"AJ"
scix.search.to_solr(scix.Document.journal.in_(["ApJ", "AJ", "MNRAS"]))
# -> bibstem:("ApJ" OR "AJ" OR "MNRAS")
```

A condition on `scix.Journal` fields can also be used directly in a document search. It is understood as
"the document's journal":

```python
query = scix.Document.select().where(scix.Journal.title == "Icarus")
scix.search.to_solr(query)
# -> bibstem:"Icar"
```

To say several things about the same journal, use `.any()`:

```python
same_journal = scix.Document.journal.any(
    scix.Journal.title.contains("astrophysical") & scix.Journal.abbreviation.startswith("Ap")
)
scix.search.to_solr(same_journal)
# -> bibstem:("ApInv" OR "ApJ" OR "ApJS" OR "ApL" OR "ApL&C")
```

If no journal matches, the condition can never be true and no request is sent:

```python
print(scix.Document.select().where(scix.Document.journal.title == "No such journal").explain())
# -> No request is made: the filter can never match.
```

At most 1000 journals may match one condition. See
[Errors and limits]({{< ref "guide/errors-and-limits#related-values" >}}).
