---
title: Matching
weight: 2
description: How SciX matches a value against keyword, text and name fields, when to use .exact(), and how to search by position.
---

After this page you will know what `==` matches on each kind of field, how to turn off SciX's expansion of
words and names with `.exact()`, and how to search by an author's position.

## Three kinds of field

Every field has a *match kind*, which says how SciX compares a value with it:

| Match kind | Fields (examples) | `==` matches | `.exact()` matches |
|---|---|---|---|
| keyword | `bibcode`, `bibstem`, `doctype`, `doi`, numbers and dates | the whole value, literally | the same as `==` |
| text | `title`, `abstract`, `keyword`, `aff`, and search-only fields such as `abs` and `full` | the words as a phrase, plus SciX's synonyms and acronyms | the phrase without synonyms |
| name | `author`, `first_author`, `editor`, `book_author` | the name and its variants (initials, full given names) | only that form of the name |

The [field reference]({{< ref "reference/document-fields" >}}) gives the match kind of every field. In
code, `describe()` reports it:

```python
from scixplorer import Document, search

kinds = {field.name: field.match_kind for field in Document.describe()}
kinds["bibcode"], kinds["title"], kinds["author"]
# -> ('keyword', 'text', 'name')
```

## `==` and `.exact()`

`.exact(value)` asks SciX to match the value with no expansion. It is sent as `=field:"value"`:

```python
search.to_solr(Document.title == "star")
# -> title:"star"
search.to_solr(Document.title.exact("star"))
# -> =title:"star"
```

On a keyword field nothing is expanded, so `.exact()` changes nothing and no `=` is sent. (SciX answers
`=bibcode:"..."` with no documents, so the client never sends it.)

```python
search.to_solr(Document.bibcode.exact("2006MNRAS.372..961K"))
# -> bibcode:"2006MNRAS.372..961K"
```

`in_()` and `not_in()` take `exact=True` too:

```python
search.to_solr(Document.author.in_(["Freedman, W", "Kewley, L"], exact=True))
# -> =author:"Freedman, W" OR =author:"Kewley, L"
```

## Text fields

`==` on a text field matches the words of the value as a phrase, in that order, anywhere in the field. Case
does not matter. SciX also matches its curated synonyms of the phrase.

Two rules from the ADS search documentation are worth knowing:

- A word in capitals is treated as an acronym and matches only the acronym. `title == "FUSE"` finds the
  satellite; `title == "fuse"` also finds the ordinary word.
- A value of several words is one phrase: `title == "dark matter halo"` does not match a title that has
  the three words apart.

To find words anywhere in the field rather than as a phrase, combine conditions with `&`:

```python
both = (Document.abs == "dark matter") & (Document.abs == "halo")
search.to_solr(both)
# -> abs:"dark matter" AND abs:"halo"
```

## Name fields

Names are written `"Last, First Middle"` or `"Last, F M"`. `==` matches the name variants SciX knows. The
The ADS search documentation gives these rules for `author`, shown here for the name Vera C. Rubin:

| Query | Matches | Does not match |
|---|---|---|
| `author == "Rubin, V"` | Rubin, V. / Rubin, V. C. / Rubin, Vera / Rubin, Vera C. / Rubin, Vera D. / Rubin, Victoria C. / Rubin, Vincent | |
| `author == "Rubin, Vera"` | Rubin, Vera / Rubin, Vera C. / Rubin, Vera D. / Rubin, V. / Rubin, V. C. / Rubin, V. D. | Rubin, Victoria C. / Rubin, Vincent |
| `author == "Rubin, Vera C"` | Rubin, Vera C. / Rubin, Vera / Rubin, V. C. / Rubin, V. | Rubin, Vera D. / Rubin, V. D. |
| `author.exact("Rubin, V")` | Rubin, V. | everything else |
| `author.exact("Rubin, Vera")` | Rubin, Vera | everything else |

Even with `.exact()`, case and the full stops after initials are ignored.

So a query with initials only is broad. Give the full first name to narrow it, and use `.exact()` when you
know exactly how the name is written:

```python
search.to_solr(Document.author.exact("Kewley, Lisa"))
# -> =author:"Kewley, Lisa"
```

SciX also applies curated synonyms for authors who changed their names. Those synonyms are known only to
the server.

## Positions

`author` holds the authors in order. Index it to search one position, or slice it for a range of positions.
Indices start at 0 and slices follow Python, so `[0:3]` is the first three:

```python
search.to_solr(Document.author[0] == "Kewley, L")
# -> pos(author:"Kewley, L", 1)
search.to_solr(Document.author[0:3] == "Dalcanton, J")
# -> pos(author:"Dalcanton, J", 1, 3)
search.to_solr(Document.author[1].exact("Dalcanton, Julianne"))
# -> pos(=author:"Dalcanton, Julianne", 2)
```

SciX counts positions from 1, which is why the query says `1` for `[0]`. It cannot count from the end, so
negative indices and open-ended slices such as `[2:]` raise `QueryError`. Only multi-valued fields can be
indexed.

`first_author` holds the first author's name on its own, so `Document.first_author == "Kewley, L"`
is another way to search for a first author.

For a condition written in Solr syntax, `Document.pos()` does the same:

```python
search.to_solr(Document.pos('author:"Kewley, L"', 1))
# -> pos(author:"Kewley, L", 1)
```

## Matching in Python

Some searches are partly checked by the client, on documents SciX returned (see
[Authors]({{< ref "usage/authors#where-conditions-are-checked" >}})). Searches of journals, affiliations
and vocabularies are checked entirely in Python. Python follows the same rules as far as they are
documented: text by its words as a phrase, ignoring case; names by the initial and full-name rule in the
table above. It does not know SciX's curated synonyms, so in rare cases the two can differ.
