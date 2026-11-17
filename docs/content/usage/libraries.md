---
title: Libraries
weight: 8
description: List, create, change and share your SciX libraries, combine them, and search the documents in them.
---

After this page you can find your libraries (and other people's public ones), create and change them,
combine them with set operations, manage who can use them, and search within them.

A library is a named collection of documents that SciX keeps for you. In scixplorer it is
`Library`. Every change is an explicit method that sends one request: there is no `save()`, and
nothing is sent behind your back. Assigning to an attribute changes only your local object.

## Find libraries

`Library.select()` lists your libraries with one request and filters them in Python. Use the same
expressions as for documents:

```python
from scixplorer import Document, Library, search

Library.select().all()
# -> [<Library: id='Rk3bq7xQTgS0vVZ2mN8cWw' name='Emission-line galaxies'>, <Library: id='Z3x9QhVnS2uYtLm4KpWb7g' name='Exoplanet transits'>]
galaxies = Library.get(name="Emission-line galaxies")
galaxies.num_documents
# -> 2
```

A condition on `id` (`Library.id == "..."`, or `.in_([...])`) fetches those libraries directly
instead of listing yours, so it also finds other people's public libraries. An id SciX does not know gives
no result:

```python
Library.get(id="Z3x9QhVnS2uYtLm4KpWb7g")
# -> <Library: id='Z3x9QhVnS2uYtLm4KpWb7g' name='Exoplanet transits'>
```

| Field | Meaning |
|---|---|
| `id` | the identifier SciX assigned |
| `name`, `description` | as you set them; names are unique among one owner's libraries |
| `public` | whether anyone may read it |
| `owner` | the owner's SciX username |
| `permission` | your own access: `"owner"`, `"admin"`, `"write"` or `"read"` |
| `num_documents`, `num_users` | counts kept by SciX |
| `date_created`, `date_last_modified` | timezone-aware UTC datetimes |

## The documents in a library

`bibcodes` fetches the bibcodes (page by page, 2000 at a time) and keeps them until a method changes the
library:

```python
galaxies.bibcodes
# -> ('2006MNRAS.372..961K', '2008ApJ...681.1183K')
"2006MNRAS.372..961K" in galaxies
# -> True
len(galaxies)
# -> 2
```

`len()` is `num_documents` as SciX reports it. `documents` is a query for the documents themselves, and
iterating over a library runs it:

```python
[paper.title[0] for paper in galaxies.documents]
# -> Metallicity Calibrations and the Mass-Metallicity Relation for Star-forming Galaxies
```

## Search within a library

`Document.in_(library)` and `Document.not_in(library)` combine with any other condition. SciX
tests membership itself, so the library's bibcodes are not fetched:

```python
search.to_solr(Document.in_(galaxies) & (Document.year > 2006))
# -> docs(library/Rk3bq7xQTgS0vVZ2mN8cWw) AND year:[2007 TO *]
unread = (
    Document
    .select(Document.bibcode)
    .where(Document.not_in(galaxies) & (Document.author == "Kewley, L"))
)
[paper.bibcode for paper in unread]
# -> ['2001ApJ...556..121K']
```

{{< callout type="warning" >}}
`docs(library/<id>)` is not yet confirmed against the live API. If it does not work, set
`config.library_membership = "bibcodes"`: the client then fetches the library's bibcodes first and
searches for them (with `/search/bigquery` for more than 50), which costs more requests.
{{< /callout >}}

## Create a library

```python
transits = Library.create(
    name="Planet transits",
    description="Transmission spectra of transiting planets",
    documents=["2000ApJ...537..916S"],
)
transits
# -> <Library: id='NewLibrary01xQ7vT2mKp9w' name='Planet transits'>
```

`documents` (and the arguments of `add` and `remove`) can be bibcodes, documents, libraries, or lists and
queries of them, in any mix. A library given as a source contributes its bibcodes, which are fetched if they are not known yet. `public` defaults to
`False`. If you leave out the name, SciX chooses one.

## Change a library

| Method | What it does | Returns |
|---|---|---|
| `add(*documents)` | adds documents; ones already there are skipped | the number SciX added |
| `remove(*documents)` | removes documents | the number SciX removed |
| `empty()` | removes every document | `None` |
| `update(name=, description=, public=)` | changes only the arguments you give | `None` |
| `refresh()` | fetches the metadata again and forgets the cached bibcodes | the library |
| `delete()` | deletes the library; the object can no longer be used | `None` |

```python
transits.add("2003ApJ...585.1038S", Document.get(bibcode="2000ApJ...537..916S"))
# -> 1
transits.num_documents
# -> 2
transits.update(name="Transiting planets", public=True)
transits.name
# -> Transiting planets
```

`add`, `remove` and `empty` update `num_documents` locally. Fields that SciX maintains, such as
`date_last_modified`, are not updated locally; call `refresh()` for them.

## Combine libraries

The set operations create a new library on SciX and return it. Pass the other libraries as objects or ids,
and the new library's `name`, `description` and `public`:

```python
both = galaxies.union(transits, name="Galaxies and planets")
both.bibcodes
# -> '2006MNRAS.372..961K', '2008ApJ...681.1183K', '2000ApJ...537..916S', '2003ApJ...585.1038S'
```

| Method | The new library holds documents |
|---|---|
| `union(*others)` | in this library or any of the others |
| `intersection(*others)` | in this library and all of the others |
| `difference(*others)` | in this library but in none of the others |

`copy_to(other)` adds this library's documents to an existing library instead.

## Share a library

```python
transits.get_permissions()
# -> {'example_user@example.org': ['owner']}
transits.set_permissions("colleague@example.org", read=True)
```

`set_permissions(email, read=, write=, admin=)` sets one SciX user's permissions; all `False` revokes
them. `transfer(email)` makes that user the owner, after which you have no access unless they grant it.

## Clients and retries

Each method accepts `client=` to send its request with a particular `Client`. Otherwise it uses the
library's `client` attribute if you set one (`create()` and the set operations set it to the client you
passed), and otherwise the default client. Libraries returned by a query do not remember the query's
client.

Whether a failed request is sent again depends on whether repeating it is safe (see
[Errors and limits]({{< ref "usage/errors-and-limits#retries" >}})):

| Request | Retried after a timeout or HTTP 502/503/504 |
|---|---|
| listing, fetching, `bibcodes`, `refresh`, `update`, `delete` | yes |
| `empty`, `set_permissions` | yes: repeating them changes nothing |
| `create`, `add`, `remove`, `union`, `intersection`, `difference`, `copy_to`, `transfer` | only if the connection could not be made, because otherwise SciX may already have applied them |

Library methods are synchronous; see [Async]({{< ref "usage/async" >}}).
