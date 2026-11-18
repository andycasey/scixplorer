---
title: Architecture
weight: 1
description: How scixplorer is put together, for contributors - expression nodes, typed fields, the two interpreters, the planner and the sans-IO backends.
---

After this page you will know where each part of scixplorer lives, how a query flows through it, and which
invariants a change must keep.

SciX search is Apache Solr, not SQL, so scixplorer has no ORM. It has four parts:

1. A small **expression language**: immutable tree nodes (`expr.py`) built by operator overloading on
   field handles (`fields.py`).
2. **Two interpreters** over the same trees: `solr.compile()` writes a Solr query string, and
   `evaluate.predicate()` evaluates a tree in Python.
3. A **planner** for document searches (`search.py`) that resolves conditions on other models, decides
   what SciX can check and what the client must, picks the endpoint and compiles.
4. **Sans-IO execution** (`query.py`, `client.py`): backends are generators that yield request
   descriptions and receive responses, so paging and merging are written once and driven synchronously or
   asynchronously.

The only runtime dependency is `httpx`.

## Modules

| Module | Contents |
|---|---|
| `__init__.py` | the public names, and `__version__` |
| `exceptions.py` | `SciXError` and its subclasses, `LazyLoadWarning`, `DataWarning` |
| `expr.py` | expression nodes, `Order`, `SolrLiteral` (`now()`, `days_ago()`), wildcard patterns, `walk`, `transform`, `simplify` |
| `fields.py` | field definitions (descriptors) and field handles |
| `model.py` | `Model` and its metaclass, `Relation` and `RelationExpr`, `Collection` and its handles |
| `solr.py` | `compile()` and `compile_order()`: the only place that knows Solr syntax |
| `evaluate.py` | `predicate()`, `matches()` and `sort_rows()`: the Python interpreter |
| `query.py` | `Query`, the `Backend` protocol, the `drive`/`adrive` drivers, `MemoryBackend`, `Explanation` |
| `_config.py` | `Config` and the module-level `config` (public as `scixplorer.config`) |
| `client.py` | `Request`, `Response`, `RateLimit`, `Client` and the default client |
| `search.py` | the planner (`plan`, `to_solr`) and `SearchBackend` |
| `models/` | `Document`, `Author`, `Journal`, `Affiliation`, `Library`, `UATConcept`, `PlanetaryFeature` |
| `services/` | `export`, `resolver`, `vis`, `citation_helper`, `metrics`: thin functions, one request each |
| `data/` | the packaged data files and the functions that download, cache and load reference data |
| `utils.py` | `parse_bibcode`, `to_bibcodes`, `flatten` |
| `cli/` | the `scix` command: `interpret.py` reads plain words into conditions, `session.py` runs the searches, `output.py` formats documents, `tui.py` is the results browser |

## Expression nodes

Every node is a frozen, hashable dataclass that compares by structure. `&`, `|` and `~` build `And`, `Or`
and `Not` (flattening nested groups), and `__bool__` always raises, so that `and`, `or`, `not` and chained
comparisons fail instead of silently dropping a clause.

| Node | Fields | Meaning |
|---|---|---|
| `And`, `Or` | `operands` | all / any |
| `Not` | `operand` | negation |
| `Match` | `field`, `value`, `exact` | the field matches the value; `exact` turns off SciX's expansion |
| `In` | `field`, `values`, `exact` | matches any of the values |
| `Like` | `field`, `pattern` | wildcard match (`*`, `?`, backslash escapes) |
| `Range` | `field`, `lo`, `hi`, `include_lo`, `include_hi` | `None` is unbounded |
| `Func` | `name`, `args` | a Solr function: `pos`, `citations`, `topn`, ... |
| `Related` | `relation`, `predicate` | the related object(s) satisfy a predicate over the related model |
| `Quantified` | `collection`, `predicate`, `quantifier`, `positions` | any / all of the records of a collection (the authors of a document) satisfy a predicate |
| `InLibrary` | `library_id` | the document is in a SciX library |
| `Raw` | `query` | verbatim Solr syntax |
| `Constant` | `value` | `EVERYTHING` and `NOTHING` |

Nodes hold the field *definitions*, never the handles. `walk()` visits a tree in pre-order (including
`Func` arguments and the predicates of `Related` and `Quantified`), `transform()` rebuilds it bottom-up, and
`simplify()` flattens groups, removes duplicates, folds constants and rewrites `In` with zero or one value.

```python
from scixplorer.expr import EVERYTHING, NOTHING, simplify
from scixplorer.models.document import Document

simplify((Document.year == 2015) & EVERYTHING)
# -> Match(field=<IntegerField Document.year>, value=2015, exact=False)
simplify((Document.year == 2015) & NOTHING)
# -> Constant(value=False)
```

## Fields and handles

Two kinds of object, kept apart on purpose:

- A **`Field`** is the definition and a data descriptor on the model class. It has plain identity equality,
  so it can sit inside nodes. Instance access returns the value; class access returns a handle.
- A **`FieldExpr`** handle (`TextExpr`, `NumericExpr`) is what `Document.year` returns. Its overloaded
  operators build nodes, after validating the value once with `field.to_solr()`. A handle may carry a
  relation (`via`), positions (`position`, wrapped in `pos()`) and a record scope (`scope`, wrapped in
  `Quantified`).

| Field class | Value type | Handle | Match kind |
|---|---|---|---|
| `StringField` | `str` | `TextExpr` | keyword |
| `NumericStringField` | `str` of digits (an `int` is accepted in queries) | `TextExpr` | keyword |
| `TextField` | `str` | `TextExpr` | text |
| `NameField` | `str` | `TextExpr` | name |
| `VirtualField` | none: search-only | `TextExpr` | text |
| `IntegerField` | `int` | `NumericExpr` | keyword |
| `FloatField` | `float` | `NumericExpr` | keyword |
| `BooleanField` | `bool` | `FieldExpr` | keyword |
| `DateTimeField` | aware UTC `datetime` | `NumericExpr` | keyword |
| `DateField` | `date` (month precision in queries) | `NumericExpr` | keyword |

Fields are generic in their value type, so type checkers know that `doc.year` is `int | None` and
`doc.title` is `list[str] | None`. At run time `Field.to_python()` returns exactly that type or `None`:
lossless coercions are silent, anything else becomes `None` with a `DataWarning`. A field can be
`multivalued`, not `stored` (search-only), and not `indexed` (returned but not searchable). `describe()`
reports all of this as `FieldInfo`.

## Models, relations and collections

`ModelMeta` collects a model's `_fields`, `_relations` and `_collections` in definition order and gives each
model its own `DoesNotExist`. A model names its key (`_key`) and its backend (`_backend`); `Document`
uses `_identifiers` (bibcode, then scix_id, then id) instead of a single key.

- A **`Relation`** links an owner to a related model through a key field (`Document.journal` through
  `bibstem` to `Journal.abbreviation`). Class access gives a `RelationExpr`, whose attributes are handles
  on the related model's fields with `via` set, and whose `.any(predicate)` makes one `Related` node for
  several conditions on the same related object. Instance access resolves the related objects and caches
  them.
- A **`Collection`** turns an owner's index-aligned arrays into records: `Document.authors` builds one
  `Author` per entry of `author`, reading `aff`, `aff_id`, `email` and the ORCID arrays at the same index
  (each `Source` names the owner fields a record field is read from and the owner field it is searched
  through). Class access gives a `CollectionExpr` with `.any()`, `.all()` and indexing; an index gives a
  `RecordExpr`, whose attributes are scoped handles.

## The two interpreters

`solr.compile(expr)` is pure. It refuses unresolved `Related` and `Quantified` nodes, which the planner
must translate first. Quoting is decided in one place, `format_value`, from the table `QUOTED_FIELD_TYPES`;
exactness is sent as `=field:` only for the match kinds in `EXPANDED_MATCH_KINDS` (text and name), because
SciX returns nothing for `=bibcode:`. A `Not` inside an `And` with a positive operand becomes `-clause`;
otherwise it is anchored as `*:* AND -clause`. Exclusive bounds move by one step on discrete fields
(integers, months) and become `[v TO *] AND -v` on others.

`evaluate.predicate(expr)` compiles a tree once into a Python function. Logic is two-valued and a missing
value never satisfies a positive condition. Text matches its words as a contiguous phrase ignoring case;
names follow the documented initial and full-name rule; `Related` and `Quantified` are evaluated over the
related objects and records. `Func`, `Raw`, `InLibrary`, search-only fields and `SolrLiteral` values raise
`NotLocallyEvaluable`.

```python
from scixplorer.evaluate import matches
from scixplorer.models.journal import Journal

matches(Journal.title.contains("astrophysical"), Journal.lookup("ApJ"))
# -> True
```

## The planner

`search.plan(query)` turns a document query into a `SearchPlan`: `q`, `fl`, `sort`, the endpoint, and
what the client filters while streaming. It is pure given its inputs. In order:

1. **Wrap implicit relations.** A leaf on another model's field (`Journal.title == "Icarus"`) is wrapped in
   the one document relation that leads to that model; none or several is a `QueryError`.
2. **Resolve relations.** Each `Related` is evaluated against the related model's in-memory rows (with
   requests refused) and replaced by `In(<owner key field>, keys)`. A predicate comparing only the key is
   used as given. More than `MAX_RELATED_VALUES` (1000) keys is an `UnsupportedQueryError`.
3. **Simplify**, then handle **library membership**: with `config.library_membership == "docs"`, `InLibrary`
   stays for the compiler (`docs(library/<id>)`); with `"bibcodes"`, it is replaced by the library's
   bibcodes, which the backend fetches before planning.
4. **Per-record conditions** (`_per_record`). Each `Quantified` is translated to a condition on the owner's
   arrays (`_translate`): leaves move to the owner field each record field is searched through, a record
   relation resolves like a document relation, and positions wrap leaves in `pos()` for fields in
   `POSITIONAL_FIELDS` (`author`, `aff`, `aff_id`). `|` is exact if every part is; `&` only on one position;
   `~` and `all` never. Exact translations replace the node anywhere. Others must be top-level conjuncts
   (optionally negated): their necessary condition (nothing when negated) stays in `q` and the conjunct
   becomes a client-side *refinement*, with the fields it reads added to `fl`.
5. **Field list and sort.** `fl` is the selected fields plus the identifiers, or every stored, non-deferred
   field. The sort always ends in `id asc` unless it already has a unique key. A filter that is now
   `NOTHING` gives an empty plan: no request.
6. **Default collection** (`_collection_scope`): `config.collection` is added as a top-level `database:`
   condition unless the filter names a collection or specific documents.
7. **Large bibcode sets.** A top-level positive set larger than `config.max_inline_bibcodes` goes to
   `/search/bigquery` (several are intersected); a top-level negated one is excluded while streaming;
   anywhere else is an `UnsupportedQueryError`. A plan that would filter locally while `q` is `*:*`
   (ignoring the collection) is refused: it would download the whole index.
8. **Compile** `q`.

When the plan filters locally (excluded bibcodes or refinements), `SearchBackend` requests full pages and
applies offset and limit itself, and `count()` pages through the results counting survivors.

## Sans-IO execution

A backend never performs I/O:

```python
from typing import Protocol


class Backend(Protocol):
    def run(self, query): ...      # yields Request (Response sent back) or lists of rows
    def count(self, query): ...    # yields Requests, returns an int
    def explain(self, query): ...  # an Explanation, without I/O
```

`query.drive(gen, send)` and `query.adrive(gen, asend)` run such a generator with a synchronous or an
asynchronous send function; an exception from `send` is thrown into the generator. `Query` uses them for
iteration, `all`, `count` and their async forms, and obtains the default client only when a request is
actually yielded, so in-memory models work without a token.

| Backend | Model | How |
|---|---|---|
| `MemoryBackend` | `Journal`, `Affiliation`, `UATConcept`, `PlanetaryFeature` | filters, sorts and slices rows from a loader in Python; never yields a request |
| `SearchBackend` | `Document` | plans, then pages through `/search/query` or `/search/bigquery` |
| `LibraryBackend` | `Library` | lists your libraries (or fetches pinned ids), then filters in Python |

`Client.send` and `Client.asend` share everything except the network call and the sleep: `_prepare` builds
the `httpx.Request` (token, `User-Agent`, body, timeout) and `_settle` interprets one attempt (retry,
response, or exception) and records rate limits.

## The command

`scix` (`cli/__init__.py`, also `python -m scixplorer`) is built on the public API and adds no dependency.
Importing `scixplorer` does not import it.

- `interpret(words)` is pure: it reads the words against patterns, the journal list and the thesaurus
  (if downloaded), and returns an `Interpretation` holding one `Reading` per piece of the search: what
  was typed, what it was read as, and the condition. A reading may carry a wider condition (`loose`),
  which `Session.find` uses once if the search matches nothing. The rules and their order are in the
  module's docstring, and `tests/test_cli_interpret.py` has an example of each.
- `Session` sends each search with a wrapper around the client that remembers `numFound`, so one
  request gives a page of documents and their total.
- `Browser` (in `tui.py`) holds the state of the screen. `key()` changes it and `render()` returns the
  screen as lines of text, so tests drive it without a terminal; `run()` is the only code that puts a
  terminal in raw mode, with the standard library alone (`termios`), so on Windows results are printed.

## Lazy loading

`Document._load_missing` loads a field for the document and for every document of its `ResultPage` that
lacks it, in one request per identifier field in use, with one `LazyLoadWarning`. Pages hold their
documents weakly. `Collection.records` calls `_load_fields` to load all the fields it needs at once.
