"""Immutable expression trees describing search predicates.

Expressions are built by operator overloading on field handles (see
:mod:`ads.fields`) and interpreted by :mod:`ads.solr` (compile to a Solr query)
and :mod:`ads.evaluate` (evaluate in Python). Every node is a frozen, hashable
dataclass that compares structurally.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from .exceptions import QueryError

if TYPE_CHECKING:
    from .fields import Field
    from .model import Relation

__all__ = [
    "EVERYTHING",
    "NOTHING",
    "And",
    "Constant",
    "Expr",
    "Func",
    "In",
    "InLibrary",
    "Like",
    "Match",
    "Not",
    "Or",
    "Order",
    "Range",
    "Raw",
    "Related",
    "SolrLiteral",
    "Wildcard",
    "days_ago",
    "escape_pattern",
    "format_pattern",
    "now",
    "parse_pattern",
    "simplify",
    "transform",
    "walk",
]

BOOL_ERROR = (
    "{name} cannot be used as a truth value. Combine expressions with & (and), "
    "| (or) and ~ (not) instead of the keywords and/or/not, wrap each comparison "
    "in parentheses, e.g. (Document.year == 2020) & (Document.author == 'Ness, M'), "
    "and use .in_(values) instead of the `in` operator."
)


class Expr:
    """Base class of all expression nodes.

    ``&``, ``|`` and ``~`` build :class:`And`, :class:`Or` and :class:`Not`.
    Expressions deliberately have no truth value, so that ``and``/``or``/``not``
    and chained comparisons fail loudly instead of silently dropping a clause.
    """

    __slots__ = ()

    def __and__(self, other: object) -> Expr:
        if not isinstance(other, Expr):
            return NotImplemented
        return And(_flatten(And, (self, other)))

    def __or__(self, other: object) -> Expr:
        if not isinstance(other, Expr):
            return NotImplemented
        return Or(_flatten(Or, (self, other)))

    def __invert__(self) -> Expr:
        if isinstance(self, Not):
            return self.operand
        if isinstance(self, Constant):
            return NOTHING if self.value else EVERYTHING
        return Not(self)

    def __bool__(self) -> bool:
        raise TypeError(BOOL_ERROR.format(name="A query expression"))


def _flatten(kind: type[And] | type[Or], operands: tuple[Expr, ...]) -> tuple[Expr, ...]:
    """Splice the operands of any ``kind`` node into a flat tuple."""
    flat: list[Expr] = []
    for operand in operands:
        if isinstance(operand, kind):
            flat.extend(operand.operands)
        else:
            flat.append(operand)
    return tuple(flat)


def _expr_tuple(owner: str, items: Any) -> tuple[Expr, ...]:
    items = tuple(items)
    for item in items:
        if not isinstance(item, Expr):
            raise TypeError(f"{owner} operands must be expressions, not {item!r}")
    return items


@dataclass(frozen=True, slots=True)
class And(Expr):
    """True when every operand is true. An empty ``And`` is always true."""

    operands: tuple[Expr, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "operands", _expr_tuple("And", self.operands))


@dataclass(frozen=True, slots=True)
class Or(Expr):
    """True when any operand is true. An empty ``Or`` is always false."""

    operands: tuple[Expr, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "operands", _expr_tuple("Or", self.operands))


@dataclass(frozen=True, slots=True)
class Not(Expr):
    """Negation of ``operand``."""

    operand: Expr

    def __post_init__(self) -> None:
        if not isinstance(self.operand, Expr):
            raise TypeError(f"Not operand must be an expression, not {self.operand!r}")


@dataclass(frozen=True, slots=True)
class Match(Expr):
    """``field`` matches ``value``; ``exact`` requests an exact (``=field:``) match."""

    field: Field
    value: Any
    exact: bool = False


@dataclass(frozen=True, slots=True)
class Like(Expr):
    """Wildcard match: ``*`` is any run of characters, ``?`` is one character.

    A backslash makes the next character literal (``\\*``, ``\\?``, ``\\\\``).
    """

    field: Field
    pattern: str

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str):
            raise TypeError(f"Like pattern must be a str, not {self.pattern!r}")
        if not self.pattern:
            raise QueryError("Like pattern must not be empty")
        parse_pattern(self.pattern)


@dataclass(frozen=True, slots=True)
class Range(Expr):
    """``lo <= field <= hi`` (bounds exclusive where requested); ``None`` is unbounded."""

    field: Field
    lo: Any
    hi: Any
    include_lo: bool = True
    include_hi: bool = True

    def __post_init__(self) -> None:
        if self.lo is None and self.hi is None:
            raise QueryError("Range needs at least one bound")


@dataclass(frozen=True, slots=True)
class In(Expr):
    """``field`` matches any of ``values``."""

    field: Field
    values: tuple[Any, ...]

    def __post_init__(self) -> None:
        if isinstance(self.values, (str, bytes)):
            raise TypeError("In values must be a collection of values, not a single string")
        object.__setattr__(self, "values", tuple(self.values))


@dataclass(frozen=True, slots=True)
class Func(Expr):
    """A Solr function or second-order operator, e.g. ``citations(...)`` or ``pos(...)``.

    Arguments are expressions, ints, strs (emitted verbatim) or :class:`Order`.
    """

    name: str
    args: tuple[Any, ...]

    def __post_init__(self) -> None:
        args = tuple(self.args)
        for arg in args:
            if isinstance(arg, bool) or not isinstance(arg, (Expr, int, str, Order)):
                raise TypeError(f"{self.name}() argument must be an Expr, int, str or Order, not {arg!r}")
        object.__setattr__(self, "args", args)


@dataclass(frozen=True, slots=True)
class Related(Expr):
    """Rows whose related object(s) through ``relation`` satisfy ``predicate``.

    ``predicate`` is an expression over the fields of the related model.
    """

    relation: Relation
    predicate: Expr

    def __post_init__(self) -> None:
        if not isinstance(self.predicate, Expr):
            raise TypeError(f"Related predicate must be an expression, not {self.predicate!r}")


@dataclass(frozen=True, slots=True)
class InLibrary(Expr):
    """The document belongs to the ADS library with id ``library_id``."""

    library_id: str


@dataclass(frozen=True, slots=True)
class Raw(Expr):
    """Verbatim Solr query syntax, for anything the expression language lacks."""

    query: str


@dataclass(frozen=True, slots=True)
class Constant(Expr):
    """Always true (:data:`EVERYTHING`) or always false (:data:`NOTHING`)."""

    value: bool


EVERYTHING = Constant(True)
NOTHING = Constant(False)


@dataclass(frozen=True, slots=True)
class Order:
    """A sort key: ``field`` ascending, or descending if ``descending``."""

    field: Field
    descending: bool = False


@dataclass(frozen=True, slots=True)
class SolrLiteral:
    """A value emitted verbatim and unquoted, such as ``NOW-7DAYS``."""

    text: str


def now() -> SolrLiteral:
    """The current time, as evaluated by Solr."""
    return SolrLiteral("NOW")


def days_ago(n: int) -> SolrLiteral:
    """The time ``n`` days before now, as evaluated by Solr."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError(f"days_ago() needs an int, not {n!r}")
    if n < 0:
        raise ValueError(f"days_ago() needs a non-negative number of days, not {n}")
    return now() if n == 0 else SolrLiteral(f"NOW-{n}DAYS")


class Wildcard(Enum):
    """Wildcard tokens of a :class:`Like` pattern."""

    ANY = "*"
    ONE = "?"


def parse_pattern(pattern: str, *, percent: bool = False) -> tuple[str | Wildcard, ...]:
    """Split a wildcard pattern into literal strings and :class:`Wildcard` tokens.

    A backslash makes the following character literal; adjacent literal
    characters are merged into one string. With ``percent=True`` an unescaped
    ``%`` is read as ``*``. This is the only parser of pattern syntax: the Solr
    compiler and the Python evaluator both use it.
    """
    tokens: list[str | Wildcard] = []
    literal: list[str] = []
    chars = iter(pattern)
    for char in chars:
        if char == "\\":
            escaped = next(chars, None)
            if escaped is None:
                raise QueryError(f"Pattern {pattern!r} ends with an unpaired backslash")
            literal.append(escaped)
            continue
        if char == "%" and percent:
            char = "*"
        if char in "*?":
            if literal:
                tokens.append("".join(literal))
                literal.clear()
            tokens.append(Wildcard(char))
        else:
            literal.append(char)
    if literal:
        tokens.append("".join(literal))
    return tuple(tokens)


def format_pattern(tokens: tuple[str | Wildcard, ...]) -> str:
    """Render tokens from :func:`parse_pattern` back to canonical pattern syntax."""
    return "".join(
        token.value if isinstance(token, Wildcard) else escape_pattern(token) for token in tokens
    )


def escape_pattern(text: str) -> str:
    """Escape ``text`` so that it matches itself literally inside a pattern."""
    return text.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")


def walk(expr: Expr) -> Iterator[Expr]:
    """Yield ``expr`` and every nested expression, in pre-order."""
    yield expr
    for child in _children(expr):
        yield from walk(child)


def _children(expr: Expr) -> tuple[Expr, ...]:
    if isinstance(expr, (And, Or)):
        return expr.operands
    if isinstance(expr, Not):
        return (expr.operand,)
    if isinstance(expr, Func):
        return tuple(arg for arg in expr.args if isinstance(arg, Expr))
    if isinstance(expr, Related):
        return (expr.predicate,)
    return ()


def transform(expr: Expr, fn: Callable[[Expr], Expr]) -> Expr:
    """Rebuild ``expr`` bottom-up: transform the children, then apply ``fn`` to the node."""
    if isinstance(expr, (And, Or)):
        expr = type(expr)(tuple(transform(operand, fn) for operand in expr.operands))
    elif isinstance(expr, Not):
        expr = Not(transform(expr.operand, fn))
    elif isinstance(expr, Func):
        args = tuple(transform(arg, fn) if isinstance(arg, Expr) else arg for arg in expr.args)
        expr = Func(expr.name, args)
    elif isinstance(expr, Related):
        expr = Related(expr.relation, transform(expr.predicate, fn))
    return fn(expr)


def simplify(expr: Expr) -> Expr:
    """Return an equivalent, simpler expression.

    Flattens nested ``And``/``Or``, drops duplicate operands, folds constants,
    rewrites ``In`` with zero or one values, unwraps single-operand groups and
    removes double negation, throughout the tree.
    """
    return transform(expr, _simplify_node)


def _simplify_node(expr: Expr) -> Expr:
    if isinstance(expr, (And, Or)):
        return _simplify_group(expr)
    if isinstance(expr, Not):
        if isinstance(expr.operand, (Not, Constant)):
            return ~expr.operand
        return expr
    if isinstance(expr, In):
        if not expr.values:
            return NOTHING
        if len(expr.values) == 1:
            return Match(expr.field, expr.values[0])
    return expr


def _simplify_group(expr: And | Or) -> Expr:
    kind = type(expr)
    absorbing, identity = (NOTHING, EVERYTHING) if kind is And else (EVERYTHING, NOTHING)
    operands: dict[Expr, None] = {}
    for operand in _flatten(kind, expr.operands):
        if operand == absorbing:
            return absorbing
        if operand != identity:
            operands.setdefault(operand)
    if not operands:
        return identity
    if len(operands) == 1:
        return next(iter(operands))
    return kind(tuple(operands))
