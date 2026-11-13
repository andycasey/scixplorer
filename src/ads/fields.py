"""Field definitions (descriptors on model classes) and field handles.

A :class:`Field` is the definition: it lives on the model class, converts
values to and from the API, and is what expression nodes refer to. Accessing a
field on the model class (``Document.year``) returns a :class:`FieldExpr`
handle, whose overloaded operators build expressions. The two are kept separate
so that fields keep plain identity equality while handles overload ``==``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from .exceptions import QueryError
from .expr import (
    BOOL_ERROR,
    Expr,
    Func,
    In,
    Like,
    Match,
    Order,
    Range,
    Related,
    SolrLiteral,
    escape_pattern,
    format_pattern,
    parse_pattern,
)

if TYPE_CHECKING:
    from .model import Relation

__all__ = [
    "BooleanField",
    "DateField",
    "DateTimeField",
    "Field",
    "FieldExpr",
    "FloatField",
    "IntegerField",
    "NumericExpr",
    "StringField",
    "TextExpr",
    "TextField",
    "VirtualField",
]


class FieldExpr:
    """A handle on a field, used to build expressions with Python operators.

    :param field: the field definition.
    :param via: the relation through which the field is reached, if any; the
        resulting expressions are wrapped in :class:`~ads.expr.Related`.
    :param position: 1-indexed inclusive ``(start, end)`` positions within a
        multivalued field; the resulting expressions are wrapped in ``pos()``.
    """

    def __init__(
        self,
        field: Field,
        *,
        via: Relation | None = None,
        position: tuple[int, int] | None = None,
    ):
        self.field = field
        self.via = via
        self.position = position
        self.__doc__ = field.__doc__

    def _wrap(self, node: Expr) -> Expr:
        """Apply this handle's position and relation to a node built on its field."""
        if self.position is not None:
            node = Func("pos", (node, *self.position))
        if self.via is not None:
            node = Related(self.via, node)
        return node

    def _check(self, value: Any) -> Any:
        """Validate a value for this field and return it unchanged."""
        if value is None:
            raise QueryError(f"Comparing {self!r} with None is not supported")
        if isinstance(value, (FieldExpr, Expr)):
            raise TypeError(f"{self!r} can only be compared with values, not with {value!r}")
        self.field.to_solr(value)
        return value

    def __eq__(self, value: object) -> Expr:  # type: ignore[override]
        return self._wrap(Match(self.field, self._check(value)))

    def __ne__(self, value: object) -> Expr:  # type: ignore[override]
        return ~(self == value)

    __hash__ = object.__hash__

    def in_(self, values: Iterable[Any]) -> Expr:
        """Match any of ``values``."""
        if isinstance(values, (str, bytes)):
            raise TypeError(
                f"{self!r}.in_() needs a collection of values, not a single string; "
                f"use == to compare with one value"
            )
        if isinstance(values, (FieldExpr, Expr)) or not isinstance(values, Iterable):
            raise TypeError(f"{self!r}.in_() needs a collection of values, not {values!r}")
        return self._wrap(In(self.field, tuple(self._check(v) for v in values)))

    def not_in(self, values: Iterable[Any]) -> Expr:
        """Match none of ``values``."""
        return ~self.in_(values)

    def _order(self, descending: bool) -> Order:
        if self.via is not None or self.position is not None:
            raise QueryError(f"Cannot sort by {self!r}; sort by a field of the model itself")
        return Order(self.field, descending)

    def asc(self) -> Order:
        """Sort key, ascending."""
        return self._order(False)

    def desc(self) -> Order:
        """Sort key, descending."""
        return self._order(True)

    def __bool__(self) -> bool:
        raise TypeError(BOOL_ERROR.format(name=f"Field {self!r}"))

    def __iter__(self) -> Any:
        raise TypeError(f"{self!r} is not iterable; use .in_(values) to match several values")

    def __repr__(self) -> str:
        owner = self.via.owner.__name__ + "." + self.via.name if self.via else _model_name(self.field)
        text = f"{owner}.{self.field.name}"
        if self.position is not None:
            start, end = self.position
            text += f"[{start - 1}]" if start == end else f"[{start - 1}:{end}]"
        return f"<{text}>"


class TextExpr(FieldExpr):
    """Handle on a text or string field: equality, phrase, wildcard and position queries."""

    def exact(self, value: str) -> Expr:
        """Match ``value`` exactly (Solr ``=field:"value"``)."""
        return self._wrap(Match(self.field, self._check(value), exact=True))

    def like(self, pattern: str) -> Expr:
        """Wildcard match: ``*`` (or ``%``) is any run of characters, ``?`` is one character.

        Precede a wildcard character or a backslash with a backslash to match it literally.
        """
        if not isinstance(pattern, str):
            raise TypeError(f"{self!r}.like() needs a str pattern, not {pattern!r}")
        return self._wrap(Like(self.field, format_pattern(parse_pattern(pattern, percent=True))))

    def startswith(self, prefix: str) -> Expr:
        """Match values starting with ``prefix`` (taken literally)."""
        if not isinstance(prefix, str):
            raise TypeError(f"{self!r}.startswith() needs a str, not {prefix!r}")
        return self._wrap(Like(self.field, escape_pattern(prefix) + "*"))

    def contains(self, text: str) -> Expr:
        """Match values containing ``text`` (taken literally)."""
        if not isinstance(text, str):
            raise TypeError(f"{self!r}.contains() needs a str, not {text!r}")
        return self._wrap(Like(self.field, "*" + escape_pattern(text) + "*"))

    def __getitem__(self, index: int | slice) -> TextExpr:
        """Restrict to positions of a multivalued field: ``author[0]``, ``author[0:3]``."""
        if not self.field.multivalued:
            raise TypeError(f"{self!r} is not multivalued, so it cannot be indexed")
        if self.position is not None:
            raise TypeError(f"{self!r} is already restricted to a position")
        if isinstance(index, slice):
            start, stop, step = index.start, index.stop, index.step
            if step not in (None, 1):
                raise QueryError("Position slices cannot have a step")
            start = 0 if start is None else start
            if not isinstance(start, int) or not isinstance(stop, (int, type(None))):
                raise TypeError(f"Position slice bounds must be ints, not {index!r}")
            if start < 0 or stop is None or stop < 0:
                raise QueryError("ADS cannot search by position from the end")
            if stop <= start:
                raise QueryError(f"Empty position slice [{start}:{stop}]")
            position = (start + 1, stop)
        elif isinstance(index, int) and not isinstance(index, bool):
            if index < 0:
                raise QueryError("ADS cannot search by position from the end")
            position = (index + 1, index + 1)
        else:
            raise TypeError(f"Positions must be ints or slices, not {index!r}")
        return type(self)(self.field, via=self.via, position=position)


class NumericExpr(FieldExpr):
    """Handle on a numeric or date field: equality and range queries."""

    def __lt__(self, value: Any) -> Expr:
        return self._wrap(Range(self.field, None, self._check(value), include_hi=False))

    def __le__(self, value: Any) -> Expr:
        return self._wrap(Range(self.field, None, self._check(value)))

    def __gt__(self, value: Any) -> Expr:
        return self._wrap(Range(self.field, self._check(value), None, include_lo=False))

    def __ge__(self, value: Any) -> Expr:
        return self._wrap(Range(self.field, self._check(value), None))

    def between(self, lo: Any, hi: Any) -> Expr:
        """Match values from ``lo`` to ``hi`` inclusive."""
        return self._wrap(Range(self.field, self._check(lo), self._check(hi)))


def _model_name(field: Field) -> str:
    model = getattr(field, "model", None)
    return model.__name__ if model is not None else "?"


class Field:
    """A field of a model: a data descriptor and the definition used in expressions.

    :param doc: help text, shown by ``help()``.
    :param multivalued: the field holds a list of values.
    :param stored: the field can be returned by the API (``False`` for search-only fields).
    :param solr_name: the field's name in Solr, if different from the attribute name.
    """

    expr_class: ClassVar[type[FieldExpr]] = FieldExpr
    name: str
    model: type

    def __init__(
        self,
        doc: str = "",
        *,
        multivalued: bool = False,
        stored: bool = True,
        solr_name: str | None = None,
    ):
        self.__doc__ = doc
        self.multivalued = multivalued
        self.stored = stored
        self._solr_name = solr_name

    def __set_name__(self, owner: type, name: str) -> None:
        if hasattr(self, "model"):
            raise TypeError(
                f"{type(self).__name__} {self.model.__name__}.{self.name} cannot also be "
                f"{owner.__name__}.{name}; create a separate field instance for each class"
            )
        self.model = owner
        self.name = name

    @property
    def solr_name(self) -> str:
        """The field's name in Solr queries and API responses."""
        return self._solr_name or self.name

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self.expr_class(self)
        return instance._get_value(self)

    def __set__(self, instance: Any, value: Any) -> None:
        instance._set_value(self, value)

    def to_python(self, raw: Any) -> Any:
        """Convert a value from the API to Python, element-wise for lists.

        ADS data is not perfectly regular, so a value that cannot be converted
        is returned unchanged rather than failing the whole result set.
        """
        if raw is None:
            return None
        if self.multivalued and isinstance(raw, (list, tuple)):
            return [self._convert(item) for item in raw]
        return self._convert(raw)

    def _convert(self, raw: Any) -> Any:
        if raw is None:
            return None
        try:
            return self._to_python(raw)
        except (ValueError, TypeError):
            return raw

    def _to_python(self, raw: Any) -> Any:
        return raw

    def step(self, value: Any, direction: int) -> Any:
        """The adjacent value above (``direction=1``) or below (``-1``) ``value``.

        Returns ``None`` for fields whose values are not discrete; discrete
        fields override this so exclusive range bounds can be made inclusive.
        """
        return None

    def to_solr(self, value: Any) -> str:
        """Format one value as bare Solr text (unquoted, unescaped).

        Raises :class:`~ads.exceptions.QueryError` if the value has the wrong type.
        """
        if isinstance(value, SolrLiteral):
            return value.text
        if value is None:
            raise QueryError(f"{self._label()} cannot be compared with None")
        return self._to_solr(value)

    def _to_solr(self, value: Any) -> str:
        return str(value)

    def _reject(self, value: Any, expected: str) -> QueryError:
        return QueryError(f"{self._label()} needs {expected}, not {value!r}")

    def _label(self) -> str:
        return f"{_model_name(self)}.{getattr(self, 'name', '?')}"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._label()}>"


class _StringLikeField(Field):
    """Shared behaviour of fields holding strings."""

    expr_class = TextExpr

    def _to_solr(self, value: Any) -> str:
        if not isinstance(value, str):
            raise self._reject(value, "a str")
        if not value.strip():
            raise self._reject(value, "a non-empty str")
        return value


class TextField(_StringLikeField):
    """Tokenized full text (title, abstract, ...): ``==`` is a phrase match."""


class StringField(_StringLikeField):
    """An untokenized keyword (bibcode, doctype, ...): ``==`` is an exact value match."""


class VirtualField(_StringLikeField):
    """A search-only field (such as ``full``); it never has a value on an instance."""

    def __init__(self, doc: str = "", *, multivalued: bool = False, solr_name: str | None = None):
        super().__init__(doc, multivalued=multivalued, stored=False, solr_name=solr_name)

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        if instance is None:
            return self.expr_class(self)
        raise AttributeError(f"{self._label()} is search-only and has no value on an instance")

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError(f"{self._label()} is search-only and cannot be set")


class IntegerField(Field):
    """An integer."""

    expr_class = NumericExpr

    def _to_python(self, raw: Any) -> Any:
        return int(raw)

    def _to_solr(self, value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._reject(value, "an int")
        return str(value)

    def step(self, value: Any, direction: int) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value + direction


class FloatField(Field):
    """A floating-point number."""

    expr_class = NumericExpr

    def _to_python(self, raw: Any) -> Any:
        return float(raw)

    def _to_solr(self, value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise self._reject(value, "a finite number")
        return repr(value)


class BooleanField(Field):
    """A boolean."""

    def _to_python(self, raw: Any) -> Any:
        if isinstance(raw, str):
            return raw.lower() == "true"
        return bool(raw)

    def _to_solr(self, value: Any) -> str:
        if not isinstance(value, bool):
            raise self._reject(value, "a bool")
        return "true" if value else "false"


def as_utc(value: date) -> datetime:
    """Return ``value`` as an aware UTC datetime; dates and naive datetimes are taken as UTC."""
    if not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class DateTimeField(Field):
    """A timestamp, held as an aware UTC ``datetime``."""

    expr_class = NumericExpr

    def _to_python(self, raw: Any) -> Any:
        if isinstance(raw, date):
            return as_utc(raw)
        text = raw[:-1] if raw.endswith("Z") else raw
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        raise ValueError(f"{self._label()}: unrecognised timestamp {raw!r}")

    def _to_solr(self, value: Any) -> str:
        if not isinstance(value, date):
            raise self._reject(value, "a date or datetime")
        return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


_PUBDATE = re.compile(r"(\d{4})-(\d{2})(?:-(\d{2}))?")


class DateField(Field):
    """A date with month precision in queries (ADS ``pubdate``), held as a ``date``."""

    expr_class = NumericExpr

    def _to_python(self, raw: Any) -> Any:
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
        match = _PUBDATE.fullmatch(raw) if isinstance(raw, str) else None
        if match is None:
            raise ValueError(f"{self._label()}: unrecognised date {raw!r}")
        year, month, day = (int(part or 0) for part in match.groups())
        return date(year, month or 1, day or 1)

    def _to_solr(self, value: Any) -> str:
        if isinstance(value, datetime):
            value = as_utc(value)
        if isinstance(value, date):
            return f"{value.year:04d}-{value.month:02d}"
        if isinstance(value, str):
            match = _PUBDATE.fullmatch(value)
            if match and match.group(3) is None and 1 <= int(match.group(2)) <= 12:
                return value
        raise self._reject(value, "a date, a datetime or a 'YYYY-MM' string")

    def step(self, value: Any, direction: int) -> Any:
        if isinstance(value, SolrLiteral):
            return None
        year, month = (int(part) for part in self.to_solr(value).split("-"))
        months = year * 12 + (month - 1) + direction
        return date(months // 12, months % 12 + 1, 1)
