"""Evaluate expressions in Python against objects.

Used for models held in memory (journals, affiliations, libraries). The
semantics follow Solr where that makes sense: logic is two-valued, and a
missing (``None``) value never satisfies a positive predicate, so its negation
is true. Multivalued values (lists/tuples, flattened recursively) satisfy a
predicate if any element does.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from typing import Any, TypeVar

from .exceptions import NotLocallyEvaluable
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
    Range,
    Raw,
    Related,
    SolrLiteral,
    Wildcard,
    parse_pattern,
)
from .fields import DateField, DateTimeField, Field, StringField, TextField, as_utc

__all__ = ["matches", "predicate", "sort_rows"]

T = TypeVar("T")
Predicate = Callable[[Any], bool]

_WORD = re.compile(r"\w+")


def matches(expr: Expr, obj: Any) -> bool:
    """Whether ``obj`` satisfies ``expr``.

    Raises :class:`~ads.exceptions.NotLocallyEvaluable` if any part of ``expr``
    can only be evaluated by ADS.
    """
    return predicate(expr)(obj)


def predicate(expr: Expr) -> Predicate:
    """Compile ``expr`` once into a function ``obj -> bool``.

    The whole expression is checked up front, so ``NotLocallyEvaluable`` is
    raised here rather than depending on which objects are tested.
    """
    if isinstance(expr, And):
        parts = [predicate(op) for op in expr.operands]
        return lambda obj: all(part(obj) for part in parts)
    if isinstance(expr, Or):
        parts = [predicate(op) for op in expr.operands]
        return lambda obj: any(part(obj) for part in parts)
    if isinstance(expr, Not):
        inner = predicate(expr.operand)
        return lambda obj: not inner(obj)
    if isinstance(expr, Constant):
        value = expr.value
        return lambda obj: value
    if isinstance(expr, Related):
        return _related(expr)
    if isinstance(expr, (Match, In, Like, Range)):
        field = expr.field
        if not field.stored:
            raise NotLocallyEvaluable(f"{field!r} is search-only, so {expr!r} needs ADS")
        test = _value_test(expr)
        name = field.name
        return lambda obj: any(test(value) for value in _flatten(getattr(obj, name)))
    if isinstance(expr, (Func, Raw, InLibrary)):
        raise NotLocallyEvaluable(f"{expr!r} can only be evaluated by ADS")
    raise TypeError(f"Cannot evaluate {expr!r}")


def _flatten(value: Any) -> Iterator[Any]:
    """Yield the non-``None`` scalar values in ``value``, flattening lists and tuples."""
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten(item)
    elif value is not None:
        yield value


def _related(expr: Related) -> Predicate:
    inner = predicate(expr.predicate)
    name = expr.relation.name
    return lambda obj: any(inner(related) for related in _flatten(getattr(obj, name)))


def _value_test(expr: Match | In | Like | Range) -> Predicate:
    """Return a test of a single (non-``None``, non-list) value of ``expr.field``."""
    field = expr.field
    if isinstance(expr, Match):
        return _match_test(field, expr.value, expr.exact)
    if isinstance(expr, In):
        tests = [_match_test(field, value, False) for value in expr.values]
        return lambda value: any(test(value) for test in tests)
    if isinstance(expr, Like):
        regex = _pattern_regex(expr.pattern)
        return lambda value: regex.fullmatch(str(value)) is not None
    return _range_test(expr)


def _match_test(field: Field[Any, Any], target: Any, exact: bool) -> Predicate:
    _require_local(target)
    if isinstance(field, TextField):
        if exact:
            wanted = _normalise_space(target)
            return lambda value: _normalise_space(value) == wanted
        needle = _tokens(target)
        return lambda value: _contains_run(_tokens(value), needle)
    if isinstance(field, StringField):
        return lambda value: value == target
    key = _comparable(field, target)
    return lambda value: _comparable(field, value) == key


def _range_test(expr: Range) -> Predicate:
    field = expr.field
    _require_local(expr.lo)
    _require_local(expr.hi)
    lo = None if expr.lo is None else _comparable(field, expr.lo)
    hi = None if expr.hi is None else _comparable(field, expr.hi)

    def test(value: Any) -> bool:
        value = _comparable(field, value)
        if lo is not None and (value < lo if expr.include_lo else value <= lo):
            return False
        if hi is not None and (value > hi if expr.include_hi else value >= hi):
            return False
        return True

    return test


def _require_local(value: Any) -> None:
    if isinstance(value, SolrLiteral):
        raise NotLocallyEvaluable(f"{value!r} is evaluated by Solr and cannot be compared locally")


def _comparable(field: Field[Any, Any], value: Any) -> Any:
    """Normalise a value of a date field for comparison; other values pass through.

    ``DateTimeField`` values become aware UTC datetimes (dates are midnight UTC);
    ``DateField`` values become ``(year, month)``, matching its month precision.
    """
    if isinstance(field, DateTimeField) and isinstance(value, date):
        return as_utc(value)
    if isinstance(field, DateField):
        if isinstance(value, datetime):
            value = as_utc(value)
        if isinstance(value, date):
            return (value.year, value.month)
        if isinstance(value, str):
            parsed = field._to_python(value)
            return (parsed.year, parsed.month)
    return value


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in _WORD.findall(str(text))]


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    if not needle:
        return False
    width = len(needle)
    return any(haystack[i : i + width] == needle for i in range(len(haystack) - width + 1))


def _normalise_space(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    for token in parse_pattern(pattern):
        if token is Wildcard.ANY:
            parts.append(".*")
        elif token is Wildcard.ONE:
            parts.append(".")
        else:
            parts.append(re.escape(token))
    return re.compile("".join(parts), re.IGNORECASE | re.DOTALL)


def sort_rows(rows: Iterable[T], orders: Iterable[Order]) -> list[T]:
    """Sort ``rows`` by ``orders`` (first key most significant).

    Each key has its own direction, ``None`` values always sort last, and rows
    with equal keys keep their input order.
    """
    result = list(rows)
    for order in reversed(list(orders)):
        field = order.field
        if not field.stored:
            raise NotLocallyEvaluable(f"Cannot sort by search-only field {field!r} locally")
        name = field.name
        present = [row for row in result if getattr(row, name) is not None]
        missing = [row for row in result if getattr(row, name) is None]
        present.sort(key=lambda row: _comparable(field, getattr(row, name)), reverse=order.descending)
        result = present + missing
    return result
