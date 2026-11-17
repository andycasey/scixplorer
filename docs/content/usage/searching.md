---
title: Searching
weight: 1
description: Build search conditions from fields and operators, combine them, and fall back to Solr syntax when you need to.
---

After this page you can write any search condition the expression language supports, combine conditions
correctly, and pass raw Solr syntax for anything else.

A condition is a Python expression on a field of `Document`. Building one sends nothing; it is
compiled to a Solr query when the search runs. `search.to_solr()` shows that query, and every
example below uses it so you can see exactly what is sent.

## Compare a field with a value

```python
from scixplorer import Document, days_ago, search

search.to_solr(Document.year == 2015)
# -> year:2015
search.to_solr(Document.bibstem == "ApJ")
# -> bibstem:"ApJ"
search.to_solr(Document.title == "dark matter halo")
# -> title:"dark matter halo"
```

How `==` matches depends on the kind of field: a text field such as `title` matches the words as a
phrase, a name field such as `author` also matches variants of the name, and a keyword field such as
`bibstem` matches the whole value. [Matching]({{< ref "usage/matching" >}}) explains the rules and how
to turn the expansion off with `.exact()`.

`!=` is "not equal":

```python
search.to_solr(Document.doctype != "eprint")
# -> *:* AND -doctype:"eprint"
```

A multi-valued field (such as `author`, `title` or `keyword`) matches if any of its values matches.

## Ranges

Numeric and date fields support `<`, `<=`, `>`, `>=` and `between()`, which includes both ends:

```python
search.to_solr(Document.year.between(2018, 2020))
# -> year:[2018 TO 2020]
search.to_solr(Document.year > 2020)
# -> year:[2021 TO *]
search.to_solr(Document.citation_count < 10)
# -> citation_count:[* TO 9]
```

Solr ranges here are inclusive, so an exclusive bound on an integer moves by one. On a floating-point
field it becomes an inclusive range that leaves out the bound itself:

```python
search.to_solr(Document.citation_count_norm > 0.5)
# -> citation_count_norm:[0.5 TO *] AND -citation_count_norm:0.5
```

### Dates

`pubdate` has month precision. Compare it with a `datetime.date`, a `datetime.datetime` or a `"YYYY-MM"`
string; only the year and month count:

```python
import datetime

search.to_solr(Document.pubdate.between(datetime.date(2020, 1, 1), datetime.date(2020, 6, 30)))
# -> pubdate:[2020-01 TO 2020-06]
search.to_solr(Document.pubdate > "2020-03")
# -> pubdate:[2020-04 TO *]
```

`date`, `entry_date` and `indexstamp` are timestamps. A naive `datetime` (or a `date`) is taken as UTC:

```python
search.to_solr(Document.entry_date >= datetime.datetime(2024, 1, 1))
# -> entry_date:["2024-01-01T00:00:00Z" TO *]
```

`days_ago(n)` and `now()` are evaluated by SciX when the search runs, so a saved query keeps
meaning "the last week":

```python
search.to_solr(Document.entry_date >= days_ago(7))
# -> entry_date:[NOW-7DAYS TO *]
```

## Several values

`in_()` matches any of several values, and `not_in()` none of them. Give it a list, tuple or set, never a
single string:

```python
search.to_solr(Document.year.in_([2019, 2021]))
# -> year:(2019 OR 2021)
search.to_solr(Document.doctype.not_in(["eprint", "abstract"]))
# -> *:* AND -doctype:("eprint" OR "abstract")
```

To match specific documents, `Document.in_()` takes bibcodes, documents or a
[library]({{< ref "usage/libraries" >}}); `Document.not_in()` excludes them:

```python
search.to_solr(Document.in_(["2006MNRAS.372..961K", "2000ApJ...537..916S"]))
# -> bibcode:("2006MNRAS.372..961K" OR "2000ApJ...537..916S")
```

Sets of more than 50 bibcodes are sent differently; see
[Errors and limits]({{< ref "usage/errors-and-limits#large-sets-of-bibcodes" >}}).

## Wildcards

Text, name and keyword fields support three kinds of wildcard match:

| Method | Meaning | Example | Sent as |
|---|---|---|---|
| `like(pattern)` | `*` (or `%`) is any run of characters, `?` is one character | `title.like("exoplanet*")` | `title:exoplanet*` |
| `startswith(text)` | starts with `text`, taken literally | `title.startswith("JWST")` | `title:JWST*` |
| `contains(text)` | contains `text`, taken literally | `title.contains("astro")` | `title:*astro*` |

```python
search.to_solr(Document.title.like("star-form*"))
# -> title:star\-form*
search.to_solr(Document.author.like("Kewley, L*"))
# -> author:"Kewley, L*"
```

`like()` uses the pattern exactly as written: `like("JWST")` matches only the word JWST. To match a
literal `*`, `?` or backslash, put a backslash before it. A pattern containing a space is sent as a quoted
phrase.

## Combine conditions

Use `&` for "and", `|` for "or" and `~` for "not". Put every comparison in its own parentheses, because
`&` and `|` bind more tightly than `==` in Python:

```python
condition = ((Document.year == 2020) | (Document.year == 2022)) & (Document.bibstem == "ApJ")
search.to_solr(condition)
# -> (year:2020 OR year:2022) AND bibstem:"ApJ"
search.to_solr(~((Document.year == 2020) & (Document.bibstem == "ApJ")))
# -> *:* AND -(year:2020 AND bibstem:"ApJ")
```

Passing several conditions to `where()`, or calling `where()` again, joins them with "and":

```python
query = (
    Document
    .select()
    .where(Document.year == 2020, Document.bibstem == "ApJ")
)
search.to_solr(query)
# -> year:2020 AND bibstem:"ApJ"
```

### The `and` / `or` / `not` pitfall

Python's `and`, `or`, `not`, `in` and chained comparisons such as `2015 <= year <= 2020` cannot be
overloaded: they ask each side whether it is true. A condition has no truth value, so they raise an error
that says what to write instead:

<!-- docs-test: raises TypeError -->
```python
(Document.year == 2020) and (Document.author == "Kewley, L")
```

```text
TypeError: A query expression cannot be used as a truth value. Combine expressions with & (and),
| (or) and ~ (not) instead of the keywords and/or/not, wrap each comparison in parentheses, e.g.
(Document.year == 2020) & (Document.author == 'Kewley, L'), and use .in_(values) instead of the `in` operator.
```

Forgetting the parentheses fails too, with a less helpful message, because Python tries `2020 & ...` first:

<!-- docs-test: raises TypeError -->
```python
Document.year == 2020 & Document.author == "Kewley, L"
```

## Values are checked when you build the condition

A value of the wrong type, a comparison with `None`, or a field that cannot be searched raises at once:

<!-- docs-test: raises QueryError -->
```python
Document.year == "2020"
```

| You wrote | You get |
|---|---|
| `Document.year == "2020"` | `QueryError: Document.year needs an int, not '2020'` |
| `Document.year == None` | `QueryError: Comparing <Document.year> with None is not supported` |
| `Document.author[-1] == "x"` | `QueryError: ADS cannot search by position from the end` |
| `Document.bibcode.in_("2006MNRAS.372..961K")` | `TypeError`: `in_()` needs a collection, not a single string |
| `Document.links_data == "x"` | `QueryError`: the field is returned but not searchable |
| `Document.year == Document.citation_count` | `TypeError`: fields can only be compared with values |

## Raw Solr syntax

For anything the expression language lacks, `Document.raw()` takes Solr syntax and sends it as it is.
It combines with other conditions like any condition, and is parenthesised when nested:

```python
dark_energy = Document.raw('abs:"dark energy" OR abs:"cosmological constant"')
search.to_solr(dark_energy & (Document.year < 2000))
# -> (abs:"dark energy" OR abs:"cosmological constant") AND year:[* TO 1999]
```

If you already have a whole query string, such as one copied from the SciX website, put it in
`Document.raw()` and build the rest of the query as usual. Choose the fields with `select()`, add filter
queries as more conditions in `where()`, and sort and limit with `order_by()` and `limit()`:

```python
query = (
    Document
    .select(
        Document.bibcode,
        Document.title,
        Document.year,
    )
    .where(
        Document.raw('author:"Seager, S"'),
        Document.raw("year:[2000 TO 2005]"),
    )
    .order_by(Document.year.asc())
    .limit(5)
)
search.to_solr(query)
# -> (author:"Seager, S") AND (year:[2000 TO 2005])
[paper.bibcode for paper in query]
# -> ['2000ApJ...537..916S', '2003ApJ...585.1038S']
```

The client does not check raw syntax; SciX reports any error in it as an `APIError`.

## Searching by other models

Conditions can also go through a document's journal, its authors' affiliations, or its vocabulary
concepts, which the client resolves before searching:

```python
query = (
    Document
    .select()
    .where(Document.journal.title == "The Astrophysical Journal")
)
search.to_solr(query)
# -> bibstem:"ApJ"
```

See [Journals]({{< ref "usage/journals" >}}), [Affiliations]({{< ref "usage/affiliations" >}}),
[Authors]({{< ref "usage/authors" >}}) and [Vocabularies]({{< ref "usage/vocabularies" >}}). For citation
networks and rankings, see [Operators]({{< ref "usage/operators" >}}). The
[expression cheat sheet]({{< ref "reference/expressions" >}}) lists many more expressions with the query
each sends.
