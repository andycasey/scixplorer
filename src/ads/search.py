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
   Then per-record conditions (``Document.authors.any(...)``, see
   :func:`_per_record`): each is translated into a condition on the
   document's arrays. One that this expresses exactly is replaced by it,
   wherever it is; any other must be a top-level ``&`` condition (optionally
   negated), sends only its necessary condition, and is checked client-side
   on each returned document (a *refinement*).
5. The default collection (``config.collection``): an open search is limited
   to it by a final top-level ``database:`` condition, unless the filter names
   the collection itself or names specific documents (see :func:`_collection_scope`).
6. Move large bibcode sets out of the query string: a top-level positive set is
   sent to ``/search/bigquery`` and a top-level negated set is excluded while
   streaming (with offset and limit then applied locally, as for a refinement).
7. Compile, adding a final sort key so that pages are stable.

:class:`SearchBackend` executes plans in the sans-IO protocol of :mod:`ads.query`.
"""

from __future__ import annotations

import weakref
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from . import _config, evaluate, solr
from .client import Request, Response
from .exceptions import APIError, NotLocallyEvaluable, QueryError, UnsupportedQueryError
from .expr import (
    EVERYTHING,
    NOTHING,
    And,
    Constant,
    Expr,
    Func,
    In,
    InLibrary,
    Like,
    Match,
    Not,
    Or,
    Order,
    Quantified,
    Range,
    Raw,
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
    "POSITIONAL_FIELDS",
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

#: Document fields that ADS searches by position with ``pos()``, so that a
#: per-author condition on one of them at given positions
#: (``Document.authors[0].aff == ...``) is sent as ``pos(aff:..., 1)``. A
#: positional condition on any other field is sent without ``pos()`` and checked
#: client-side. Read at planning time. To be confirmed against the live API.
POSITIONAL_FIELDS = frozenset({"author", "aff", "aff_id"})

QUERY_PATH = "/search/query"
BIGQUERY_PATH = "/search/bigquery"

_STRATEGIES = ("docs", "bibcodes")

#: Fields besides the identifiers (``Document._identifiers``) whose values name
#: specific documents: a top-level condition on one exempts a search from the
#: default collection.
_ALSO_NAMING = ("identifier", "doi", "alternate_bibcode")

#: Fields through which a filter names the collection itself, and how raw Solr text does.
_COLLECTION_FIELDS = ("database", "collection")
_COLLECTION_TEXT = ("database:", "collection:")

#: Fields whose values are unique, so that sorting by one needs no tiebreaker.
#: (``scix_id`` is left out until it is known to be sortable on the server.)
_UNIQUE = frozenset({"id", "bibcode"})


@dataclass(frozen=True)
class SearchPlan:
    """How a document query is sent to ADS.

    ``q``, ``fl`` (Solr field names) and ``sort`` are the search parameters.
    ``bigquery_bibcodes`` is set when the search goes to ``/search/bigquery``
    with those bibcodes. Results are filtered locally while streaming (and
    offset and limit then applied locally) when ``exclude_bibcodes`` are to be
    dropped or a ``refinement`` (a condition ``q`` only approximates) must
    hold for each document; ``refinement_fl`` are the fields it reads. An
    ``empty`` plan sends no request: nothing can match. ``notes`` explain the
    planner's decisions.
    """

    q: str
    fl: tuple[str, ...]
    sort: str
    bigquery_bibcodes: tuple[str, ...] | None = None
    exclude_bibcodes: frozenset[str] | None = None
    empty: bool = False
    notes: tuple[str, ...] = ()
    refinement: Expr | None = None
    refinement_fl: tuple[str, ...] = ()

    @property
    def filters_locally(self) -> bool:
        """Whether results are filtered while streaming, so offset and limit are applied locally."""
        return self.exclude_bibcodes is not None or self.refinement is not None

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
    uploaded bibcodes, without them; for one with a per-record condition
    checked client-side, it holds only that condition's necessary part. A filter that can never match gives the
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
            message = (
                f"{owner.__name__}.{expr.field.name} is not a field of {model.__name__}, and "
                f"{model.__name__} has no relation to {owner.__name__}"
            )
            for collection in model._collections.values():
                if collection.record_model is owner:
                    message += (
                        f"; {owner.__name__} fields are used inside {collection.label()}.any(...) or "
                        f".all(...), e.g. {collection.label()}.any({owner.__name__}.{expr.field.name} == ...)"
                    )
            raise QueryError(message)
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
    return _keys_condition(relation, node.predicate, model._fields[relation.key], f"{model.__name__}.{relation.name}", notes)


def _keys_condition(
    relation: Relation[Any], predicate: Expr, key_field: Field[Any, Any], label: str, notes: list[str]
) -> Expr:
    """``In(key_field, keys)`` for the related rows matching ``predicate`` (``NOTHING`` if none), with a note.

    Shared by document relations and relations of a collection's records
    (``Author.affiliation``), so both resolve and are limited alike.
    """
    keys, how = _related_keys(relation, predicate, label)
    if not keys:
        notes.append(f"{label}: {how}; no {relation.related_model.__name__} matched, so this condition matches nothing")
        return NOTHING
    if len(keys) > MAX_RELATED_VALUES:
        raise UnsupportedQueryError(
            f"{label}: {len(keys)} {relation.related_model.__name__} rows matched "
            f"{_describe(predicate)}, but at most {MAX_RELATED_VALUES} {relation.key} values fit in "
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
    """Passes 3 to 7: libraries, simplification, collection, large bibcode sets, compilation, fields."""
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
    expr, refinements, read = _per_record(expr, model, notes)
    fl, fl_note = _field_list(query)
    sort, sort_note = _sort(query)
    notes.append(fl_note)
    added = [name for name in read if model._fields[name].solr_name not in fl]
    if added:
        fl = tuple(f.solr_name for name, f in model._fields.items() if f.solr_name in fl or name in added)
        notes.append(f"also fetching {', '.join(added)} for the client-side check")
    notes.append(sort_note)
    if expr == NOTHING:
        notes.append("the filter can never match, so no request is made")
        return SearchPlan(solr.compile(NOTHING), fl, sort, empty=True, notes=tuple(notes))
    scope, scope_note = _collection_scope(expr, model)
    if scope_note is not None:
        notes.append(scope_note)

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
    if scope is not None:
        remaining = simplify(And((remaining, scope)))
    refinement = refinements[0] if len(refinements) == 1 else And(refinements) if refinements else None
    return SearchPlan(
        q=solr.compile(remaining),
        fl=fl,
        sort=sort,
        bigquery_bibcodes=bigquery,
        exclude_bibcodes=exclude,
        notes=tuple(notes),
        refinement=refinement,
        refinement_fl=tuple(f.solr_name for name, f in model._fields.items() if name in read),
    )


# Per-record conditions.


@dataclass(frozen=True)
class _Server:
    """What ADS is sent for (part of) a per-record condition.

    ``expr`` is a condition on the owner's fields; it decides the condition
    exactly if ``exact``, else it is only necessary and ``reason`` says why.
    """

    expr: Expr
    exact: bool
    reason: str = ""


def _per_record(expr: Expr, model: type[Model], notes: list[str]) -> tuple[Expr, tuple[Expr, ...], tuple[str, ...]]:
    """Pass 4b: translate per-record conditions (:class:`~ads.expr.Quantified`) for ADS.

    Returns the filter to send, the refinements (top-level conditions to check
    client-side on each returned document) and the owner fields they read.

    Each condition is translated with :func:`_translate`. One translated
    exactly is replaced in place, anywhere in the filter. Any other must be a
    top-level ``&`` condition, optionally negated: its necessary condition (or
    nothing, when negated) stays in the filter and the condition itself
    becomes a refinement; elsewhere it raises ``UnsupportedQueryError``.
    ``expr`` is simplified (so ``~~c`` and nested groups are already flat);
    the result is simplified again.
    """
    if not any(isinstance(node, Quantified) for node in walk(expr)):
        return expr, (), ()
    kept: list[Expr] = []
    refinements: list[Expr] = []
    read: dict[str, None] = {}
    for conjunct in expr.operands if isinstance(expr, And) else (expr,):
        negated = isinstance(conjunct, Not) and isinstance(conjunct.operand, Quantified)
        node = conjunct.operand if isinstance(conjunct, Not) else conjunct
        if not isinstance(node, Quantified):
            kept.append(transform(conjunct, lambda n: _replace_exact(n, model, notes)))
            continue
        server = _translate(node, model, notes)
        label = _label(node)
        if server.exact:
            kept.append(~server.expr if negated else server.expr)
            notes.append(f"{label}: expressed exactly in the query as {solr.compile(server.expr)}")
            continue
        try:
            evaluate.predicate(conjunct)
        except NotLocallyEvaluable as exc:
            raise NotLocallyEvaluable(
                f"{label} needs a client-side check ({server.reason}), but cannot be evaluated in Python: {exc}"
            ) from exc
        collection = node.collection
        fields = collection.owner_fields(collection.fields_read(node.predicate))
        read.update(dict.fromkeys(fields))
        refinements.append(conjunct)
        if negated:
            sent = "nothing is sent to ADS for it (it is negated)"
        else:
            kept.append(server.expr)
            sent = f"ADS is sent the necessary condition {solr.compile(server.expr)}"
        notes.append(
            f"{'~' if negated else ''}{label}: checked client-side on each returned document "
            f"({server.reason}), reading {', '.join(fields)}; {sent}. Values are compared in Python: "
            f"names with the documented initial/full-name rule and text by its words, without the "
            f"curated synonyms known only to ADS. Offset and limit are then applied locally"
        )
    return simplify(And(tuple(kept))), tuple(refinements), tuple(read)


def _replace_exact(node: Expr, model: type[Model], notes: list[str]) -> Expr:
    """A per-record condition below the top level: its exact translation, or ``UnsupportedQueryError``."""
    if not isinstance(node, Quantified):
        return node
    server = _translate(node, model, notes)
    label = _label(node)
    if not server.exact:
        raise UnsupportedQueryError(
            f"{label} needs a client-side check ({server.reason}), so it must be a top-level & condition "
            f"of the query, optionally negated with ~, e.g. .where({label} & (...)); here it is inside "
            f"an |, a nested ~ or an operator. Restructure the query so that it is a top-level & "
            f"condition, or make it exact: a single condition on {node.collection.record_model.__name__} "
            f"fields, several joined with |, or conditions on one position ({node.collection.label()}[0].any(...))."
        )
    notes.append(f"{label}: expressed exactly in the query as {solr.compile(server.expr)}")
    return server.expr


def _label(node: Quantified) -> str:
    return f"{node.collection.label(node.positions)}.{node.quantifier}(...)"


def _translate(node: Quantified, model: type[Model], notes: list[str]) -> _Server:
    """The condition on ``model``'s fields that ADS is sent for ``node``, and whether it is exact.

    Leaves on record fields are moved to the owner field each is searched
    through (the collection's sources); a relation of the record model resolves
    to its keys as a document relation does. With positions, a leaf on a field in
    :data:`POSITIONAL_FIELDS` is wrapped in ``pos()``, any other is sent without
    it (not exact). ``|`` is exact if every part is; ``&`` only if one record is
    selected and every part is exact (otherwise different records could
    satisfy different parts); ``~`` and record fields ADS cannot search send
    nothing; ``all(...)`` is never exact. A condition that can never match is
    ``NOTHING``, exactly.
    """
    collection = node.collection
    if not issubclass(model, collection.owner):
        raise QueryError(f"{_label(node)} is a condition on {collection.owner.__name__}, not on {model.__name__}")
    server = _server(node.predicate, node, model, notes)
    record = collection.record_model.__name__.lower()
    if node.quantifier == "all" and server.exact:
        server = _Server(server.expr, False, f"all(...) needs every {record} checked")
    expr = simplify(server.expr)
    if expr == NOTHING:
        return _Server(NOTHING, True)
    return _Server(expr, server.exact, server.reason)


def _server(predicate: Expr, node: Quantified, model: type[Model], notes: list[str]) -> _Server:
    """Translate one part of ``node``'s predicate (see :func:`_translate`)."""
    collection, positions = node.collection, node.positions
    record = collection.record_model
    if isinstance(predicate, Constant):
        where = "" if positions is None else " at those positions"
        return _Server(predicate, predicate == NOTHING, f"whether there is any {record.__name__.lower()}{where}")
    if isinstance(predicate, (Or, And)):
        parts = [_server(operand, node, model, notes) for operand in predicate.operands]
        reasons = [part.reason for part in parts if not part.exact]
        if isinstance(predicate, Or):
            return _Server(Or(tuple(p.expr for p in parts)), not reasons, reasons[0] if reasons else "")
        single = positions is not None and positions[0] == positions[1]
        if not single:
            reasons.insert(0, f"conditions joined with & may be met by different {record.__name__.lower()}s")
        return _Server(And(tuple(p.expr for p in parts)), not reasons, reasons[0] if reasons else "")
    if isinstance(predicate, Not):
        return _Server(EVERYTHING, False, "a ~ inside it")
    if isinstance(predicate, Related):
        relation = predicate.relation
        source = collection.sources.get(relation.key)
        if source is None or source.search is None:
            return _Server(EVERYTHING, False, f"{record.__name__}.{relation.name} cannot be searched by ADS")
        label = f"{_label(node)}: {record.__name__}.{relation.name}"
        condition = _keys_condition(relation, predicate.predicate, model._fields[source.search], label, notes)
        if condition == NOTHING:
            return _Server(NOTHING, True)
        return _position(simplify(condition), source.search, positions)
    if isinstance(predicate, (Match, In, Like, Range)):
        field = predicate.field
        source = collection.sources.get(field.name)
        if source is None or source.search is None:
            return _Server(EVERYTHING, False, f"{record.__name__}.{field.name} cannot be searched by ADS")
        leaf: Expr = replace(predicate, field=model._fields[source.search])
        if isinstance(leaf, (Match, In)) and field.match_kind not in solr.EXPANDED_MATCH_KINDS:
            leaf = replace(leaf, exact=False)
        return _position(simplify(leaf), source.search, positions)
    raise QueryError(f"{_label(node)} cannot contain {predicate!r}")


def _position(leaf: Expr, field_name: str, positions: tuple[int, int] | None) -> _Server:
    """``leaf`` restricted to ``positions``: in ``pos()`` if ADS searches the field by position."""
    if positions is None:
        return _Server(leaf, True)
    if field_name in POSITIONAL_FIELDS:
        return _Server(Func("pos", (leaf, *positions)), True)
    return _Server(leaf, False, f"{field_name} is not searched by position (search.POSITIONAL_FIELDS)")


def _collection_scope(expr: Expr, model: type[Model]) -> tuple[Expr | None, str | None]:
    """Pass 5: the condition limiting ``expr`` to the default collection, and a note.

    ``(None, None)`` when no collection is configured. ``(None, note)`` when
    the search is exempt: it names the collection itself (a condition on
    ``database`` or ``collection`` anywhere, or raw Solr text containing
    ``database:`` or ``collection:``), or it names specific documents (a
    top-level ``&`` condition that is a positive ``==``/``in_`` on an
    identifying field, or a library). ``expr`` is the simplified filter.
    """
    names = _config.config.resolve_collections()
    database = model._fields.get("database")
    if not names or database is None:
        return None, None
    setting = names[0] if len(names) == 1 else list(names)
    skipped = f"collection {setting!r} (ads.config.collection) not applied"
    for node in walk(expr):
        if isinstance(node, (Match, In, Like, Range)) and _is_field(model, node.field, _COLLECTION_FIELDS):
            return None, f"{skipped}: the filter names a collection itself ({node.field.name})"
        if isinstance(node, Raw) and any(text in node.query.lower() for text in _COLLECTION_TEXT):
            return None, f"{skipped}: the raw query names a collection itself"
    naming = (*_identifiers(model), *_ALSO_NAMING)
    for conjunct in expr.operands if isinstance(expr, And) else (expr,):
        if isinstance(conjunct, InLibrary):
            return None, f"{skipped}: the filter asks for the documents of library {conjunct.library_id}"
        if isinstance(conjunct, (Match, In)) and _is_field(model, conjunct.field, naming):
            return None, f"{skipped}: the filter names specific documents by {conjunct.field.name}"
    scope = Match(database, names[0]) if len(names) == 1 else In(database, names)
    return scope, f"limited to collection {setting!r} (ads.config.collection): {solr.compile(scope)} is added"


def _is_field(model: type[Model], field: Field[Any, Any], names: Sequence[str]) -> bool:
    """Whether ``field`` is the field of ``model`` with one of ``names``."""
    return any(model._fields.get(name) is field for name in names)


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
        wanted = {field.name for field in query.fields} | set(_identifiers(model))
        note = f"fetching {len(wanted)} selected field(s); others load on first access"
    else:
        wanted = {name for name, field in model._fields.items() if field.stored and name not in deferred}
        note = (
            f"fetching all {len(wanted)} stored fields except the deferred ones "
            f"({', '.join(sorted(deferred))}), which load on first access"
        )
    fl = tuple(field.solr_name for name, field in model._fields.items() if name in wanted)
    return fl, note


def _identifiers(model: type[Model]) -> tuple[str, ...]:
    """The fields that identify a document, always fetched (``Document._identifiers``, else the model's key)."""
    identifiers: tuple[str, ...] | None = getattr(model, "_identifiers", None)
    return (model._key,) if identifiers is None else identifiers


def _sort(query: Query[Any]) -> tuple[str, str]:
    """Pass 6: the ``sort`` parameter, always ending in a unique key so paging is stable."""
    model = query.model
    for order in query.order:
        field = order.field
        if isinstance(field, VirtualField):
            kind = "search-only"
        elif field.multivalued:
            kind = "multivalued"
        elif not field.indexed:
            kind = "not indexed: it can be returned but not searched or sorted by"
        else:
            continue
        raise QueryError(f"Cannot sort by {model.__name__}.{field.name}: it is {kind}")
    tiebreaker = solr.compile_order((Order(model._fields["id"]),))
    given = solr.compile_order(query.order)
    if given is None:
        return (
            f"score desc,{tiebreaker}",
            f"no order given: sorted by relevance (score desc), then {tiebreaker} so pages are stable",
        )
    if any(order.field.name in _UNIQUE for order in query.order):
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
