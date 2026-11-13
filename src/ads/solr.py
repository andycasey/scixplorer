"""Compile expressions to Solr query strings.

This module is the only place that knows Solr quoting, escaping and range
syntax. It is pure: no I/O, and the output depends only on the expression.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, NamedTuple

from .exceptions import QueryError
from .expr import (
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
    SolrLiteral,
    Wildcard,
    parse_pattern,
)
from .fields import DateTimeField, Field, NameField, StringField, TextField, VirtualField

__all__ = ["QUOTED_FIELD_TYPES", "compile", "compile_order", "format_value", "quote"]

#: Field types whose values are written as double-quoted phrases. Values of all
#: other field types are written bare. ``SolrLiteral`` values are always bare.
QUOTED_FIELD_TYPES: tuple[type[Field[Any, Any]], ...] = (
    TextField,
    StringField,
    NameField,
    VirtualField,
    DateTimeField,
)

#: Match kinds that ADS expands by default, so that ``exact`` must be sent as
#: ``=field:``. On other kinds ``=`` is unnecessary (and breaks ``=bibcode:``).
EXPANDED_MATCH_KINDS = frozenset({"text", "name"})

_LUCENE_SPECIALS = re.compile(r'([+\-&|!(){}\[\]^"~:\\/*?])')
_WHITESPACE = re.compile(r"\s")


class _Fragment(NamedTuple):
    """Compiled text, and whether it is a boolean group needing parentheses when nested."""

    text: str
    compound: bool = False


_EVERYTHING = _Fragment("*:*")
_NOTHING = _Fragment("*:* AND -*:*", compound=True)


def compile(expr: Expr) -> str:
    """Compile a resolved expression to a Solr query string.

    Raises :class:`~ads.exceptions.QueryError` for unresolved ``Related`` and
    ``Quantified`` nodes.
    """
    return _compile(expr).text


def compile_order(orders: Iterable[Order]) -> str | None:
    """Return the Solr ``sort`` parameter for ``orders``, or ``None`` if there are none."""
    keys = [f"{order.field.solr_name} {'desc' if order.descending else 'asc'}" for order in orders]
    return ",".join(keys) or None


def quote(text: str) -> str:
    """Double-quote ``text``, escaping backslashes and then double quotes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_value(field: Field[Any, Any], value: Any) -> str:
    """Format one value of ``field`` for a query: the single place quoting is decided."""
    if isinstance(value, SolrLiteral):
        return value.text
    text = field.to_solr(value)
    return quote(text) if isinstance(field, QUOTED_FIELD_TYPES) else text


def _compile(expr: Expr) -> _Fragment:
    if isinstance(expr, And):
        return _compile_and(expr.operands)
    if isinstance(expr, Or):
        return _compile_or(expr.operands)
    if isinstance(expr, Not):
        return _compile_and((expr,))
    if isinstance(expr, Constant):
        return _EVERYTHING if expr.value else _NOTHING
    if isinstance(expr, Match):
        prefix = "=" if _sends_exact(expr.field, expr.exact) else ""
        return _Fragment(f"{prefix}{expr.field.solr_name}:{format_value(expr.field, expr.value)}")
    if isinstance(expr, In):
        return _compile_in(expr)
    if isinstance(expr, Like):
        return _Fragment(f"{expr.field.solr_name}:{_compile_pattern(expr.pattern)}")
    if isinstance(expr, Range):
        return _compile_range(expr)
    if isinstance(expr, Func):
        return _compile_func(expr)
    if isinstance(expr, InLibrary):
        return _Fragment(f"docs(library/{expr.library_id})")
    if isinstance(expr, Raw):
        return _Fragment(expr.query, compound=True)
    if isinstance(expr, Related):
        raise QueryError(
            f"Cannot compile an unresolved relation ({expr.relation!r}); "
            f"the planner must resolve it first"
        )
    if isinstance(expr, Quantified):
        raise QueryError(
            f"Cannot compile an unresolved per-record condition ({expr.collection.label(expr.positions)}."
            f"{expr.quantifier}(...)); the planner must translate it first"
        )
    raise TypeError(f"Cannot compile {expr!r}")


def _operand(expr: Expr) -> str:
    """Compile an operand of a boolean group, parenthesising nested groups."""
    fragment = _compile(expr)
    return f"({fragment.text})" if fragment.compound else fragment.text


def _compile_and(operands: tuple[Expr, ...]) -> _Fragment:
    """Compile a conjunction. ``Not`` operands become ``-clause``.

    Solr cannot evaluate a purely negative group, so if every operand is a
    ``Not`` the group is anchored with ``*:*``. A ``Not`` anywhere else is
    compiled as the one-operand conjunction ``*:* AND -clause``.
    """
    if not operands:
        return _EVERYTHING
    if len(operands) == 1 and not isinstance(operands[0], Not):
        return _compile(operands[0])
    clauses = [
        "-" + _operand(op.operand) if isinstance(op, Not) else _operand(op) for op in operands
    ]
    if all(isinstance(op, Not) for op in operands):
        clauses.insert(0, _EVERYTHING.text)
    return _Fragment(" AND ".join(clauses), compound=True)


def _compile_or(operands: tuple[Expr, ...]) -> _Fragment:
    if not operands:
        return _NOTHING
    if len(operands) == 1:
        return _compile(operands[0])
    return _Fragment(" OR ".join(_operand(op) for op in operands), compound=True)


def _sends_exact(field: Field[Any, Any], exact: bool) -> bool:
    """Whether an exact match on ``field`` needs the ``=`` prefix."""
    return exact and field.match_kind in EXPANDED_MATCH_KINDS


def _compile_in(expr: In) -> _Fragment:
    if not expr.values:
        return _NOTHING
    if len(expr.values) == 1:
        return _compile(Match(expr.field, expr.values[0], expr.exact))
    if _sends_exact(expr.field, expr.exact):
        return _compile_or(tuple(Match(expr.field, value, True) for value in expr.values))
    values = " OR ".join(format_value(expr.field, value) for value in expr.values)
    return _Fragment(f"{expr.field.solr_name}:({values})")


def _compile_range(expr: Range) -> _Fragment:
    """Compile a range. Solr ranges here are inclusive, so exclusive bounds are lowered.

    Discrete fields (see ``Field.step``) move the bound to the adjacent value: an
    integer by one, a month-precision date by a month. Any other exclusive bound
    becomes an inclusive range that excludes the bound value, compiled as
    ``And(inclusive range, Not(Match(field, bound)))``.
    """
    field = expr.field
    lo, hi = expr.lo, expr.hi
    excluded: list[Any] = []
    if lo is not None and not expr.include_lo:
        stepped = field.step(lo, 1)
        if stepped is None:
            excluded.append(lo)
        else:
            lo = stepped
    if hi is not None and not expr.include_hi:
        stepped = field.step(hi, -1)
        if stepped is None:
            excluded.append(hi)
        else:
            hi = stepped
    if excluded:
        return _compile_and((Range(field, lo, hi), *(Not(Match(field, v)) for v in excluded)))
    lo_text = "*" if lo is None else format_value(field, lo)
    hi_text = "*" if hi is None else format_value(field, hi)
    return _Fragment(f"{field.solr_name}:[{lo_text} TO {hi_text}]")


def _compile_func(expr: Func) -> _Fragment:
    args = expr.args
    if expr.name == "pos" and len(args) == 3 and args[1] == args[2]:
        args = args[:2]
    return _Fragment(f"{expr.name}({', '.join(_func_arg(arg) for arg in args)})")


def _func_arg(arg: Any) -> str:
    if isinstance(arg, Expr):
        return _compile(arg).text
    if isinstance(arg, Order):
        return f"{arg.field.solr_name} {'desc' if arg.descending else 'asc'}"
    return str(arg)


def _compile_pattern(pattern: str) -> str:
    """Compile a wildcard pattern.

    A pattern containing whitespace is written as a quoted phrase (escaping
    backslashes and double quotes); otherwise as a single term with Lucene
    special characters backslash-escaped. Literal ``*`` and ``?`` are escaped in
    both forms; wildcards are kept.
    """
    tokens = parse_pattern(pattern)
    phrase = any(isinstance(t, str) and _WHITESPACE.search(t) for t in tokens)
    parts = []
    for token in tokens:
        if isinstance(token, Wildcard):
            parts.append(token.value)
        elif phrase:
            parts.append(re.sub(r'([\\"*?])', r"\\\1", token))
        else:
            parts.append(_LUCENE_SPECIALS.sub(r"\\\1", token))
    body = "".join(parts)
    return f'"{body}"' if phrase else body
