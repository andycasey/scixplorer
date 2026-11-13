---
title: Searching
weight: 1
description: Build search conditions from fields and operators, combine them, and fall back to Solr syntax when you need to.
---

After this page you can write any search condition the expression language supports, combine conditions
correctly, and pass raw Solr syntax for anything else.

A condition is a Python expression on a field of `scix.Document`. Building one sends nothing; it is
compiled to a Solr query when the search runs. `scix.search.to_solr()` shows that query, and every
example below uses it so you can see exactly what is sent.

## Compare a field with a value

```python
import scixplorer as scix

scix.search.to_solr(scix.Document.year == 2015)
# -> year:2015
scix.search.to_solr(scix.Document.bibstem == "ApJ")
# -> bibstem:"ApJ"
scix.search.to_solr(scix.Document.title == "dark matter halo")
# -> title:"dark matter halo"
```

How `==` matches depends on the kind of field: a text field such as `title` matches the words as a
phrase, a name field such as `author` also matches variants of the name, and a keyword field such as
`bibstem` matches the whole value. [Matching]({{< ref "guide/matching" >}}) explains the rules and how
to turn the expansion off with `.exact()`.

`!=` is "not equal":

```python
scix.search.to_solr(scix.Document.doctype != "eprint")
# -> *:* AND -doctype:"eprint"
```

A multi-valued field (such as `author`, `title` or `keyword`) matches if any of its values matches.

## Ranges

Numeric and date fields support `<`, `<=`, `>`, `>=` and `between()`, which includes both ends:

```python
scix.search.to_solr(scix.Document.year.between(2018, 2020))
# -> year:[2018 TO 2020]
scix.search.to_solr(scix.Document.year > 2020)
# -> year:[2021 TO *]
scix.search.to_solr(scix.Document.citation_count < 10)
# -> citation_count:[* TO 9]
```

Solr ranges here are inclusive, so an exclusive bound on an integer moves by one. On a floating-point
field it becomes an inclusive range that leaves out the bound itself:

```python
scix.search.to_solr(scix.Document.citation_count_norm > 0.5)
# -> citation_count_norm:[0.5 TO *] AND -citation_count_norm:0.5
```

### Dates

`pubdate` has month precision. Compare it with a `datetime.date`, a `datetime.datetime` or a `"YYYY-MM"`
string; only the year and month count:

```python
import datetime

scix.search.to_solr(scix.Document.pubdate.between(datetime.date(2020, 1, 1), datetime.date(2020, 6, 30)))
# -> pubdate:[2020-01 TO 2020-06]
scix.search.to_solr(scix.Document.pubdate > "2020-03")
# -> pubdate:[2020-04 TO *]
```

`date`, `entry_date` and `indexstamp` are timestamps. A naive `datetime` (or a `date`) is taken as UTC:

```python
scix.search.to_solr(scix.Document.entry_date >= datetime.datetime(2024, 1, 1))
# -> entry_date:["2024-01-01T00:00:00Z" TO *]
```

`scix.days_ago(n)` and `scix.now()` are evaluated by SciX when the search runs, so a saved query keeps
meaning "the last week":

```python
scix.search.to_solr(scix.Document.entry_date >= scix.days_ago(7))
# -> entry_date:[NOW-7DAYS TO *]
```

## Several values

`in_()` matches any of several values, and `not_in()` none of them. Give it a list, tuple or set, never a
single string:

```python
scix.search.to_solr(scix.Document.year.in_([2019, 2021]))
# -> year:(2019 OR 2021)
scix.search.to_solr(scix.Document.doctype.not_in(["eprint", "abstract"]))
# -> *:* AND -doctype:("eprint" OR "abstract")
```

To match specific documents, `scix.Document.in_()` takes bibcodes, documents or a
[library]({{< ref "guide/libraries" >}}); `scix.Document.not_in()` excludes them:

```python
scix.search.to_solr(scix.Document.in_(["2015ApJ...808...16N", "1995Natur.378..355M"]))
# -> bibcode:("2015ApJ...808...16N" OR "1995Natur.378..355M")
```

Sets of more than 50 bibcodes are sent differently; see
[Errors and limits]({{< ref "guide/errors-and-limits#large-sets-of-bibcodes" >}}).

## Wildcards

Text, name and keyword fields support three kinds of wildcard match:

| Method | Meaning | Example | Sent as |
|---|---|---|---|
| `like(pattern)` | `*` (or `%`) is any run of characters, `?` is one character | `title.like("exoplanet*")` | `title:exoplanet*` |
| `startswith(text)` | starts with `text`, taken literally | `title.startswith("JWST")` | `title:JWST*` |
| `contains(text)` | contains `text`, taken literally | `title.contains("astro")` | `title:*astro*` |

```python
scix.search.to_solr(scix.Document.title.like("star-form*"))
# -> title:star\-form*
scix.search.to_solr(scix.Document.author.like("Casey, A*"))
# -> author:"Casey, A*"
```

`like()` uses the pattern exactly as written: `like("JWST")` matches only the word JWST. To match a
literal `*`, `?` or backslash, put a backslash before it. A pattern containing a space is sent as a quoted
phrase.

## Combine conditions

Use `&` for "and", `|` for "or" and `~` for "not". Put every comparison in its own parentheses, because
`&` and `|` bind more tightly than `==` in Python:

```python
condition = ((scix.Document.year == 2020) | (scix.Document.year == 2022)) & (scix.Document.bibstem == "ApJ")
scix.search.to_solr(condition)
# -> (year:2020 OR year:2022) AND bibstem:"ApJ"
scix.search.to_solr(~((scix.Document.year == 2020) & (scix.Document.bibstem == "ApJ")))
# -> *:* AND -(year:2020 AND bibstem:"ApJ")
```

Passing several conditions to `where()`, or calling `where()` again, joins them with "and":

```python
query = scix.Document.select().where(scix.Document.year == 2020, scix.Document.bibstem == "ApJ")
scix.search.to_solr(query)
# -> year:2020 AND bibstem:"ApJ"
```

### The `and` / `or` / `not` pitfall

Python's `and`, `or`, `not`, `in` and chained comparisons such as `2015 <= year <= 2020` cannot be
overloaded: they ask each side whether it is true. A condition has no truth value, so they raise an error
that says what to write instead:

<!-- docs-test: raises TypeError -->
```python
(scix.Document.year == 2020) and (scix.Document.author == "Ness, M")
```

```text
TypeError: A query expression cannot be used as a truth value. Combine expressions with & (and),
| (or) and ~ (not) instead of the keywords and/or/not, wrap each comparison in parentheses, e.g.
(Document.year == 2020) & (Document.author == 'Ness, M'), and use .in_(values) instead of the `in` operator.
```

Forgetting the parentheses fails too, with a less helpful message, because Python tries `2020 & ...` first:

<!-- docs-test: raises TypeError -->
```python
scix.Document.year == 2020 & scix.Document.author == "Ness, M"
```

## Values are checked when you build the condition

A value of the wrong type, a comparison with `None`, or a field that cannot be searched raises at once:

<!-- docs-test: raises QueryError -->
```python
scix.Document.year == "2020"
```

| You wrote | You get |
|---|---|
| `scix.Document.year == "2020"` | `QueryError: Document.year needs an int, not '2020'` |
| `scix.Document.year == None` | `QueryError: Comparing <Document.year> with None is not supported` |
| `scix.Document.author[-1] == "x"` | `QueryError: ADS cannot search by position from the end` |
| `scix.Document.bibcode.in_("2015ApJ...808...16N")` | `TypeError`: `in_()` needs a collection, not a single string |
| `scix.Document.links_data == "x"` | `QueryError`: the field is returned but not searchable |
| `scix.Document.year == scix.Document.citation_count` | `TypeError`: fields can only be compared with values |

## Raw Solr syntax

For anything the expression language lacks, `scix.Document.raw()` takes Solr syntax and sends it as it is.
It combines with other conditions like any condition, and is parenthesised when nested:

```python
dark_energy = scix.Document.raw('abs:"dark energy" OR abs:"cosmological constant"')
scix.search.to_solr(dark_energy & (scix.Document.year < 2000))
# -> (abs:"dark energy" OR abs:"cosmological constant") AND year:[* TO 1999]
```

If you already have a whole query string, `scix.SearchQuery` turns Solr parameters into an ordinary query.
`fl` is a list or a comma-separated string of field names, `fq` filter queries are joined to `q` with
"and", and `rows` and `start` become the limit and offset:

```python
query = scix.SearchQuery(
    q='author:"Mayor, M"',
    fl="bibcode,title,year",
    fq=["year:[1990 TO 2000]"],
    sort="year asc",
    rows=5,
)
scix.search.to_solr(query)
# -> (author:"Mayor, M") AND (year:[1990 TO 2000])
[paper.bibcode for paper in query]
# -> ['1995Natur.378..355M', '2000ApJ...529L..45C']
```

The client does not check raw syntax; SciX reports any error in it as an `APIError`.

## Searching by other models

Conditions can also go through a document's journal, its authors' affiliations, or its vocabulary
concepts, which the client resolves before searching:

```python
query = scix.Document.select().where(scix.Document.journal.title == "The Astrophysical Journal")
scix.search.to_solr(query)
# -> bibstem:"ApJ"
```

See [Journals]({{< ref "guide/journals" >}}), [Affiliations]({{< ref "guide/affiliations" >}}),
[Authors]({{< ref "guide/authors" >}}) and [Vocabularies]({{< ref "guide/vocabularies" >}}). For citation
networks and rankings, see [Operators]({{< ref "guide/operators" >}}). The
[expression cheat sheet]({{< ref "reference/expressions" >}}) lists many more expressions with the query
each sends.
