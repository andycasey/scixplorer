"""Plan and run document searches on the ADS search API.

:func:`plan` turns a :class:`~ads.query.Query` over :class:`~ads.models.document.Document`
into a :class:`SearchPlan`: the Solr ``q``, ``fl`` and ``sort`` parameters, and
which endpoint to use. It is pure (no I/O) and runs these passes in order:

1. Wrap predicates on a related model's fields (``Journal.title == ...``) in the
   one document relation that leads to that model.
2. Resolve each relation predicate against the related model (held in memory)
   into a set of keys: ``Document.journal.title.contains("x")`` becomes
   ``bibstem:(...)`` and ``Document.affiliation.country == "Australia"`` becomes
   ``aff_id:(...)``.
3. Library membership: left to ADS as ``docs(library/<id>)``, or (with
   ``config.library_membership = "bibcodes"``) replaced by the library's bibcodes.
4. Simplify; a filter that can never match needs no request at all.
5. Move large bibcode sets out of the query string: a top-level positive set is
   sent to ``/search/bigquery`` and a top-level negated set is excluded while
   streaming.
6. Compile, adding a final sort key so that pages are stable.

:class:`SearchBackend` executes plans in the sans-IO protocol of :mod:`ads.query`.
"""

from __future__ import annotations

import weakref
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import _config, solr
from .client import Request, Response
from .exceptions import APIError, NotLocallyEvaluable, QueryError, UnsupportedQueryError
from .expr import (
    EVERYTHING,
    NOTHING,
    And,
    Expr,
    Func,
    In,
    InLibrary,
    Like,
    Match,
    Not,
    Or,
    Order,
    Range,
    Related,
    simplify,
    transform,
    walk,
)
from .fields import Field, VirtualField
from .model import Model, Relation
from .models.library import fetch_bibcodes
from .query import Explanation, Query

__all__ = [
    "BIGQUERY_PATH",
    "MAX_PAGE_SIZE",
    "MAX_RELATED_VALUES",
    "QUERY_PATH",
    "ResultPage",
    "SearchBackend",
    "SearchPlan",
    "plan",
    "to_solr",
]

#: Most keys a relation predicate may resolve to: one GET request cannot carry more clauses.
MAX_RELATED_VALUES = 1000

#: Most rows ADS returns per search request.
MAX_PAGE_SIZE = 2000

QUERY_PATH = "/search/query"
BIGQUERY_PATH = "/search/bigquery"

_STRATEGIES = ("docs", "bibcodes")

#: Field names that identify a document; sorting by either needs no tiebreaker.
_IDENTIFYING = frozenset({"id", "bibcode"})


@dataclass(frozen=True)
class SearchPlan:
    """How a document query is sent to ADS.

    ``q``, ``fl`` (Solr field names) and ``sort`` are the search parameters.
    ``bigquery_bibcodes`` is set when the search goes to ``/search/bigquery``
    with those bibcodes; ``exclude_bibcodes`` are dropped from the results
    while streaming (offset and limit are then applied locally). An ``empty``
    plan sends no request: nothing can match. ``notes`` explain the planner's
    decisions.
    """

    q: str
    fl: tuple[str, ...]
    sort: str
    bigquery_bibcodes: tuple[str, ...] | None = None
    exclude_bibcodes: frozenset[str] | None = None
    empty: bool = False
    notes: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        """The endpoint: ``/search/bigquery`` or ``/search/query``."""
        return QUERY_PATH if self.bigquery_bibcodes is None else BIGQUERY_PATH

    def request(self, start: int, rows: int, *, fl: Sequence[str] | None = None) -> Request:
        """The request for ``rows`` results from position ``start`` (``fl`` overrides the fields)."""
        params: dict[str, Any] = {
            "q": self.q,
            "fl": ",".join(self.fl if fl is None else fl),
            "sort": self.sort,
            "start": start,
            "rows": rows,
        }
        if self.bigquery_bibcodes is None:
            return Request("GET", QUERY_PATH, params=params)
        params["fq"] = "{!bitset}"
        return Request(
            "POST",
            BIGQUERY_PATH,
            params=params,
            content="bibcode\n" + "\n".join(self.bigquery_bibcodes),
            headers={"Content-Type": "big-query/csv"},
            idempotent=True,
        )


class ResultPage:
    """The documents of one page of results and the client that fetched them.

    Documents keep a reference to their page so that a field read on one of
    them can be loaded for all of them with one request. The page holds the
    documents weakly, so it never keeps them alive.
    """

    __slots__ = ("client", "_members")

    def __init__(self, client: Any) -> None:
        self.client = client
        self._members: list[weakref.ref[Model]] = []

    def attach(self, document: Model) -> None:
        """Add ``document`` to this page."""
        self._members.append(weakref.ref(document))
        setattr(document, "_page", self)

    def members(self) -> list[Model]:
        """The documents of this page that still exist, in result order."""
        alive = (ref() for ref in self._members)
        return [document for document in alive if document is not None]


# Planning.


@dataclass(frozen=True)
class _Resolved:
    """The filter after relations are resolved (passes 1 and 2), with notes so far."""

    expr: Expr
    notes: tuple[str, ...]


def plan(query: Query[Any], *, library_bibcodes: Mapping[str, Sequence[str]] | None = None) -> SearchPlan:
    """Plan ``query``: decide the request parameters without performing any I/O.

    With ``config.library_membership == "bibcodes"``, ``library_bibcodes`` must
    map the id of every library in the filter to its bibcodes (the backend
    fetches them). Raises :class:`~ads.exceptions.QueryError` (or its subclass
    :class:`~ads.exceptions.UnsupportedQueryError`) for a query that cannot be
    sent, and ``ValueError`` for an invalid configuration.
    """
    return _finish(query, _resolve(query), library_bibcodes or {})


def to_solr(query_or_expr: Query[Any] | Expr) -> str:
    """The Solr ``q`` string that a document query (or a filter expression) sends.

    For a search sent to ``/search/bigquery`` this is the query applied to the
    uploaded bibcodes, without them. A filter that can never match gives the
    query matching nothing. Raises :class:`~ads.exceptions.UnsupportedQueryError`
    if the query needs the bibcodes of a library (``library_membership="bibcodes"``).
    """
    if isinstance(query_or_expr, Expr):
        from .models.document import Document

        query: Query[Any] = Document.select().where(query_or_expr)
    else:
        query = query_or_expr
    resolved = _resolve(query)
    ids = _library_ids(resolved)
    if ids:
        raise UnsupportedQueryError(
            f"With ads.config.library_membership = 'bibcodes' this query needs the bibcodes "
            f"of library {ids[0]!r}, which to_solr() does not fetch. Use query.explain(), or "
            f"set library_membership = 'docs' to see the query with docs(library/<id>)."
        )
    return _finish(query, resolved, {}).q


def _strategy() -> str:
    strategy = _config.config.library_membership
    if strategy not in _STRATEGIES:
        raise ValueError(
            f"ads.config.library_membership must be 'docs' or 'bibcodes', not {strategy!r}"
        )
    return strategy


def _page_size() -> int:
    size = _config.config.page_size
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ValueError(f"ads.config.page_size must be a positive int, not {size!r}")
    return min(size, MAX_PAGE_SIZE)


def _resolve(query: Query[Any]) -> _Resolved:
    """Passes 1 and 2: wrap implicit relation predicates, then resolve every relation."""
    model = query.model
    expr = query.expr if query.expr is not None else EVERYTHING
    notes: list[str] = []
    expr = _wrap_implicit(expr, model)
    expr = transform(expr, lambda node: _resolve_related(node, model, notes))
    return _Resolved(expr, tuple(notes))


def _wrap_implicit(expr: Expr, model: type[Model]) -> Expr:
    """Wrap each leaf on another model's field in the relation leading to that model.

    Existing :class:`Related` predicates are left alone: their leaves are
    meant to be on the related model. (``transform`` cannot skip a subtree,
    so this recursion is written out.)
    """
    if isinstance(expr, Related):
        return expr
    if isinstance(expr, (And, Or)):
        return type(expr)(tuple(_wrap_implicit(operand, model) for operand in expr.operands))
    if isinstance(expr, Not):
        return Not(_wrap_implicit(expr.operand, model))
    if isinstance(expr, Func):
        args = tuple(_wrap_implicit(arg, model) if isinstance(arg, Expr) else arg for arg in expr.args)
        return Func(expr.name, args)
    if isinstance(expr, (Match, In, Like, Range)):
        owner = expr.field.model
        if issubclass(model, owner):
            return expr
        relations = [rel for rel in model._relations.values() if rel.related_model is owner]
        if len(relations) == 1:
            return Related(relations[0], expr)
        if not relations:
            raise QueryError(
                f"{owner.__name__}.{expr.field.name} is not a field of {model.__name__}, and "
                f"{model.__name__} has no relation to {owner.__name__}"
            )
        names = ", ".join(f"{model.__name__}.{rel.name}" for rel in relations)
        raise QueryError(
            f"{owner.__name__}.{expr.field.name} is ambiguous: say which relation it is "
            f"reached through ({names}), e.g. {model.__name__}.{relations[0].name}.{expr.field.name}"
        )
    return expr


def _resolve_related(node: Expr, model: type[Model], notes: list[str]) -> Expr:
    """Pass 2 for one node: a relation predicate becomes ``In(<owner key field>, keys)``."""
    if not isinstance(node, Related) or model._relations.get(node.relation.name) is not node.relation:
        return node
    relation = node.relation
    label = f"{model.__name__}.{relation.name}"
    keys, how = _related_keys(relation, node.predicate, label)
    key_field = model._fields[relation.key]
    if not keys:
        notes.append(f"{label}: {how}; no {relation.related_model.__name__} matched, so this condition matches nothing")
        return NOTHING
    if len(keys) > MAX_RELATED_VALUES:
        raise UnsupportedQueryError(
            f"{label}: {len(keys)} {relation.related_model.__name__} rows matched "
            f"{_describe(node.predicate)}, but at most {MAX_RELATED_VALUES} {relation.key} values fit in "
            f"one search request. Narrow the {relation.related_model.__name__} condition."
        )
    notes.append(f"{label}: {how}; matched as {relation.key} in {len(keys)} value(s)")
    return In(key_field, tuple(keys))


def _related_keys(relation: Relation[Any], predicate: Expr, label: str) -> tuple[list[Any], str]:
    """The ``related_key`` values of the related rows matching ``predicate``, and how they were found.

    A predicate that only compares the related key itself (``Document.journal ==
    "ApJ"``) gives its values directly, so a key missing from the local data
    still matches on ADS.
    """
    key_field = relation.related_field
    if isinstance(predicate, Match) and predicate.field is key_field:
        return [predicate.value], f"{relation.related_key} given directly"
    if isinstance(predicate, In) and predicate.field is key_field:
        return list(dict.fromkeys(predicate.values)), f"{relation.related_key} given directly"
    related = relation.related_model
    try:
        rows = _local_rows(related, related.select().where(predicate))
    except NotLocallyEvaluable as exc:
        raise NotLocallyEvaluable(
            f"{label}: the {related.__name__} condition must be evaluable locally, "
            f"because {related.__name__} rows are matched in Python before searching: {exc}"
        ) from exc
    keys = (getattr(row, relation.related_key) for row in rows)
    found = list(dict.fromkeys(key for key in keys if key is not None))
    return found, f"{len(rows)} {related.__name__} row(s) matched {_describe(predicate)}"


def _describe(predicate: Expr) -> str:
    """A predicate over a related model, written in Solr syntax for notes (its repr if that fails)."""
    try:
        return solr.compile(predicate)
    except (QueryError, TypeError):
        return repr(predicate)


def _local_rows(model: type[Model], query: Query[Any]) -> list[Any]:
    """Run ``query`` with its model's backend, refusing any request (planning is pure)."""
    rows: list[Any] = []
    run = model._backend.run(query)
    try:
        for item in run:
            if not isinstance(item, list):
                raise UnsupportedQueryError(
                    f"{model.__name__} conditions inside a document query must be answerable "
                    f"without requests to ADS"
                )
            rows.extend(item)
    finally:
        run.close()
    return rows


def _library_ids(resolved: _Resolved) -> list[str]:
    """Ids of the libraries whose bibcodes must be fetched before planning (in order, distinct)."""
    if _strategy() != "bibcodes":
        return []
    nodes = walk(simplify(resolved.expr))
    return list(dict.fromkeys(node.library_id for node in nodes if isinstance(node, InLibrary)))


def _finish(query: Query[Any], resolved: _Resolved, library_bibcodes: Mapping[str, Sequence[str]]) -> SearchPlan:
    """Passes 3 to 7: libraries, simplification, large bibcode sets, compilation, fields."""
    model = query.model
    bibcode_field = model._fields["bibcode"]
    notes = list(resolved.notes)
    # Simplified first, so that libraries in branches that cannot match are
    # neither needed nor fetched (the backend collects ids the same way).
    expr = simplify(resolved.expr)
    from_libraries: set[Expr] = set()

    if _strategy() == "bibcodes":
        for library_id in _library_ids(resolved):
            if library_id not in library_bibcodes:
                raise ValueError(
                    f"plan() needs the bibcodes of library {library_id!r} "
                    f"(library_membership='bibcodes'): pass library_bibcodes={{id: bibcodes}}"
                )
            count = len(library_bibcodes[library_id])
            notes.append(f"library {library_id}: {count} bibcode(s), fetched from ADS")

        def replace(node: Expr) -> Expr:
            if not isinstance(node, InLibrary):
                return node
            members = In(bibcode_field, tuple(library_bibcodes[node.library_id]))
            from_libraries.add(members)
            return members

        expr = simplify(transform(expr, replace))
    elif any(isinstance(node, InLibrary) for node in walk(expr)):
        notes.append("library membership is tested by ADS with docs(library/<id>)")
    fl, fl_note = _field_list(query)
    sort, sort_note = _sort(query)
    notes.append(fl_note)
    notes.append(sort_note)
    if expr == NOTHING:
        notes.append("the filter can never match, so no request is made")
        return SearchPlan(solr.compile(NOTHING), fl, sort, empty=True, notes=tuple(notes))

    limit = _config.config.max_inline_bibcodes
    conjuncts = expr.operands if isinstance(expr, And) else (expr,)
    positive: list[tuple[Any, ...]] = []
    negative: list[tuple[Any, ...]] = []
    rest: list[Expr] = []
    for conjunct in conjuncts:
        large = _large_set(conjunct, bibcode_field, limit)
        negated = _large_set(conjunct.operand, bibcode_field, limit) if isinstance(conjunct, Not) else None
        if large is not None:
            positive.append(large.values)
        elif negated is not None:
            negative.append(negated.values)
        else:
            rest.append(conjunct)
    for conjunct in rest:
        for node in walk(conjunct):
            large = _large_set(node, bibcode_field, limit)
            if large is not None:
                raise UnsupportedQueryError(_large_set_message(large, limit, large in from_libraries))

    bigquery: tuple[str, ...] | None = None
    exclude: frozenset[str] | None = None
    if positive:
        kept = dict.fromkeys(positive[0])
        for other in positive[1:]:
            members = set(other)
            kept = {bibcode: None for bibcode in kept if bibcode in members}
        dropped = set().union(*negative)
        bigquery = tuple(bibcode for bibcode in kept if bibcode not in dropped)
        sets = f"the intersection of {len(positive)} bibcode sets" if len(positive) > 1 else "a bibcode set"
        notes.append(
            f"{sets} larger than max_inline_bibcodes={limit}: searching /search/bigquery "
            f"with {len(bigquery)} bibcode(s)"
            + (" (excluded bibcodes removed from the upload)" if negative else "")
        )
        if not bigquery:
            notes.append("no bibcode is left to search, so no request is made")
            return SearchPlan(solr.compile(NOTHING), fl, sort, empty=True, notes=tuple(notes))
    elif negative:
        exclude = frozenset().union(*negative)
        notes.append(
            f"excluding {len(exclude)} bibcode(s) (a negated set larger than max_inline_bibcodes={limit}) "
            f"while streaming; offset and limit are then applied locally"
        )

    remaining = rest[0] if len(rest) == 1 else And(tuple(rest)) if rest else EVERYTHING
    return SearchPlan(
        q=solr.compile(remaining),
        fl=fl,
        sort=sort,
        bigquery_bibcodes=bigquery,
        exclude_bibcodes=exclude,
        notes=tuple(notes),
    )


def _large_set(expr: Expr, bibcode_field: Field[Any, Any], limit: int) -> In | None:
    """``expr`` if it is a bibcode set larger than ``limit``, else ``None``."""
    if isinstance(expr, In) and expr.field is bibcode_field and len(expr.values) > limit:
        return expr
    return None


def _large_set_message(node: In, limit: int, from_library: bool) -> str:
    message = (
        f"A set of {len(node.values)} bibcodes (more than max_inline_bibcodes={limit}) can only be searched "
        f"as a condition AND-ed with the rest of the query at the top level: "
        f"Document.bibcode.in_(...) & (...) is sent to /search/bigquery, and "
        f"~Document.bibcode.in_(...) & (...) excludes the set while streaming. Here it is "
        f"inside an |, a nested ~ or an operator. Either restructure the query so that the set "
        f"is a top-level & condition, or put the papers in an ADS library and use "
        f"Document.in_(library), which ADS evaluates itself."
    )
    if from_library:
        message += (
            " This set comes from a library because ads.config.library_membership is "
            "'bibcodes'; with 'docs' (the default) ADS tests library membership directly."
        )
    return message


def _field_list(query: Query[Any]) -> tuple[tuple[str, ...], str]:
    """Pass 7: the Solr names to fetch, in definition order, and a note describing them."""
    model = query.model
    deferred: frozenset[str] = getattr(model, "_deferred", frozenset())
    if query.fields:
        for field in query.fields:
            if not field.stored:
                raise QueryError(
                    f"{model.__name__}.{field.name} is search-only: ADS can search it but not return it"
                )
        wanted = {field.name for field in query.fields} | _IDENTIFYING
        note = f"fetching {len(wanted)} selected field(s); others load on first access"
    else:
        wanted = {name for name, field in model._fields.items() if field.stored and name not in deferred}
        note = (
            f"fetching all {len(wanted)} stored fields except the deferred ones "
            f"({', '.join(sorted(deferred))}), which load on first access"
        )
    fl = tuple(field.solr_name for name, field in model._fields.items() if name in wanted)
    return fl, note


def _sort(query: Query[Any]) -> tuple[str, str]:
    """Pass 6: the ``sort`` parameter, always ending in a unique key so paging is stable."""
    model = query.model
    for order in query.order:
        field = order.field
        if isinstance(field, VirtualField) or field.multivalued:
            kind = "search-only" if isinstance(field, VirtualField) else "multivalued"
            raise QueryError(f"Cannot sort by {model.__name__}.{field.name}: it is {kind}")
    tiebreaker = solr.compile_order((Order(model._fields["id"]),))
    given = solr.compile_order(query.order)
    if given is None:
        return (
            f"score desc,{tiebreaker}",
            f"no order given: sorted by relevance (score desc), then {tiebreaker} so pages are stable",
        )
    if any(order.field.name in _IDENTIFYING for order in query.order):
        return given, f"sorted by {given}"
    return f"{given},{tiebreaker}", f"sorted by {given}, then {tiebreaker} so pages are stable"


# Execution.


def _read_page(request: Request, response: Response | None) -> tuple[list[dict[str, Any]], int]:
    """The documents and ``numFound`` of a search response; ``APIError`` if it has another shape."""
    data = None if response is None else response.data
    body = data.get("response") if isinstance(data, dict) else None
    docs = body.get("docs") if isinstance(body, dict) else None
    found = body.get("numFound") if isinstance(body, dict) else None
    if (
        not isinstance(docs, list)
        or not all(isinstance(doc, dict) for doc in docs)
        or isinstance(found, bool)
        or not isinstance(found, int)
    ):
        detail = ""
        if isinstance(data, dict) and data.get("error"):
            detail = f": {data['error']!r}"
        raise APIError(
            f"{request.method} {request.path}: unexpected response from ADS (expected "
            f"response.docs as a list of objects and response.numFound as an int){detail}",
            status_code=None if response is None else response.status_code,
            response=response,
        )
    return docs, found


def _fetch_libraries(resolved: _Resolved) -> Generator[Request, Response, dict[str, list[str]]]:
    """Yield the requests for the bibcodes of every library the plan needs, and return them."""
    found: dict[str, list[str]] = {}
    for library_id in _library_ids(resolved):
        found[library_id] = yield from fetch_bibcodes(library_id)
    return found


class SearchBackend:
    """Runs document queries on the ADS search API (``/search/query`` and ``/search/bigquery``).

    Pages are requested one at a time as results are consumed, with
    ``rows = min(config.page_size, remaining)`` (at most 2000). Each document
    remembers its page so that unfetched fields load for the whole page at once.
    """

    def __repr__(self) -> str:
        return "<SearchBackend>"

    def _plan(self, query: Query[Any]) -> Generator[Request, Response, SearchPlan]:
        resolved = _resolve(query)
        library_bibcodes = yield from _fetch_libraries(resolved)
        return _finish(query, resolved, library_bibcodes)

    def run(self, query: Query[Any]) -> Generator[Request | list[Any], Response | None, None]:
        """Yield the search requests, and each page of documents as soon as it arrives."""
        search = yield from self._plan(query)
        if search.empty:
            return
        page_size = _page_size()
        model = query.model
        bibcode_name = model._fields["bibcode"].solr_name
        requested = [name for name, field in model._fields.items() if field.solr_name in search.fl]
        exclude = search.exclude_bibcodes
        remaining = query.limit_
        if exclude is None:
            start, skip = query.offset_, 0
        else:
            start, skip = 0, query.offset_
        while remaining is None or remaining > 0:
            rows = page_size if remaining is None or exclude is not None else min(page_size, remaining)
            request = search.request(start, rows)
            response = yield request
            docs, found = _read_page(request, response)
            start += len(docs)
            kept: list[dict[str, Any]] = []
            for doc in docs:
                if exclude is not None and doc.get(bibcode_name) in exclude:
                    continue
                if skip:
                    skip -= 1
                    continue
                if remaining is not None:
                    if remaining == 0:
                        break
                    remaining -= 1
                kept.append(doc)
            if kept:
                page = ResultPage(query.client)
                yield [_document(model, doc, requested, page) for doc in kept]
            if not docs or start >= found:
                return

    def count(self, query: Query[Any]) -> Generator[Request, Response, int]:
        """Return the number of matching documents, ignoring offset and limit.

        One request with ``rows=0``, unless bibcodes are excluded locally: then
        every matching bibcode is fetched and the survivors counted.
        """
        search = yield from self._plan(query)
        if search.empty:
            return 0
        exclude = search.exclude_bibcodes
        if exclude is None:
            request = search.request(0, 0, fl=(query.model._fields["bibcode"].solr_name,))
            response = yield request
            return _read_page(request, response)[1]
        page_size = _page_size()
        bibcode_name = query.model._fields["bibcode"].solr_name
        total = start = 0
        while True:
            request = search.request(start, page_size, fl=(bibcode_name,))
            response = yield request
            docs, found = _read_page(request, response)
            total += sum(1 for doc in docs if doc.get(bibcode_name) not in exclude)
            start += len(docs)
            if not docs or start >= found:
                return total

    def explain(self, query: Query[Any]) -> Explanation:
        """Describe the first request(s) and the planner's decisions, without any I/O."""
        resolved = _resolve(query)
        ids = _library_ids(resolved)
        if ids:
            requests = tuple(_first_request(fetch_bibcodes(library_id)) for library_id in ids)
            noun = "library" if len(ids) == 1 else "libraries"
            return Explanation(
                f"Fetches the bibcodes of {len(ids)} {noun} first (library_membership='bibcodes'); "
                f"the search request follows once they are known.",
                requests,
                (*resolved.notes, "the search endpoint depends on how many bibcodes the libraries hold"),
            )
        search = _finish(query, resolved, {})
        if search.empty:
            return Explanation("No request is made: the filter can never match.", (), search.notes)
        if query.limit_ == 0:
            return Explanation("No request is made: the limit is 0.", (), search.notes)
        page_size = _page_size()
        if search.exclude_bibcodes is None:
            start = query.offset_
            rows = page_size if query.limit_ is None else min(page_size, query.limit_)
        else:
            start, rows = 0, page_size
        request = search.request(start, rows)
        summary = f"Searches ADS with {request.method} {request.path}, up to {page_size} rows per request."
        if search.bigquery_bibcodes is not None:
            summary += f" Uploads {len(search.bigquery_bibcodes)} bibcodes."
        return Explanation(summary, (request,), search.notes)


def _first_request(gen: Generator[Request, Response, Any]) -> Request:
    """The first request a protocol generator would yield, without running the rest."""
    try:
        return next(gen)
    finally:
        gen.close()


def _document(model: type[Model], row: Mapping[str, Any], requested: list[str], page: ResultPage) -> Model:
    """A document from one result row, attached to its page.

    Fields that were requested but are absent from the row (ADS omits empty
    values) are set to ``None``, so reading them does not load them again.
    """
    document = model._from_row(row)
    for name in requested:
        document._data.setdefault(name, None)
    page.attach(document)
    return document
