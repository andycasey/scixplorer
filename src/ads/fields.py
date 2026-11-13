"""Field definitions (descriptors on model classes) and field handles.

A :class:`Field` is the definition: it lives on the model class, converts
values to and from the API, and is what expression nodes refer to. Accessing a
field on the model class (``Document.year``) returns a :class:`FieldExpr`
handle, whose overloaded operators build expressions. The two are kept separate
so that fields keep plain identity equality while handles overload ``==``.

Fields are generic in their value type, so type checkers know that
``paper.year`` is ``int | None`` and ``paper.title`` (multivalued) is
``list[str] | None``. Values read from the API are converted to exactly that
type or to ``None`` (see :meth:`Field.to_python`).

Matching. Each field class declares how ADS matches its values
(``Field.match_kind``):

- ``"keyword"`` (:class:`StringField` and all numeric, boolean and date fields):
  the value is matched literally; there is nothing to expand.
- ``"text"`` (:class:`TextField`, :class:`VirtualField`): tokenized; ``==``
  matches the phrase, and ADS also matches its synonyms and acronyms.
- ``"name"`` (:class:`NameField`): personal names, ``"Last, First M"``; ``==``
  also matches name variants, so ``author == "Murray, S"`` matches
  ``"Murray, Stephen S."`` and ``"Murray, Sarah"``.

``.exact(value)`` and ``.in_(values, exact=True)`` mean "match this value with
no expansion" (Solr ``=field:``). On a keyword field that is the same as ``==``.
"""

from __future__ import annotations

import math
import re
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, NoReturn, Protocol, TypeVar, cast, overload

from .exceptions import DataWarning, QueryError
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
    "FieldInfo",
    "FloatField",
    "IntegerField",
    "NameField",
    "NumericExpr",
    "NumericStringField",
    "Scope",
    "StringField",
    "TextExpr",
    "TextField",
    "VirtualField",
    "parse_positions",
]


class FieldExpr:
    """A handle on a field, used to build expressions with Python operators.

    :param field: the field definition.
    :param via: the relation through which the field is reached, if any; the
        resulting expressions are wrapped in :class:`~ads.expr.Related`.
    :param position: 1-indexed inclusive ``(start, end)`` positions within a
        multivalued field; the resulting expressions are wrapped in ``pos()``.
    :param scope: the record(s) of a collection the field belongs to, such as
        ``Document.authors[0]`` for ``Document.authors[0].name``; the resulting
        expressions are wrapped in :class:`~ads.expr.Quantified` by it.
    """

    def __init__(
        self,
        field: Field[Any, Any],
        *,
        via: Relation[Any] | None = None,
        position: tuple[int, int] | None = None,
        scope: Scope | None = None,
    ):
        self.field = field
        self.via = via
        self.position = position
        self.scope = scope
        self.__doc__ = field.__doc__

    def _searchable(self) -> None:
        """Refuse a field that is not indexed: it can be returned but not searched or sorted by."""
        if not self.field.indexed:
            label = self.field._label()
            raise QueryError(
                f"{label} can be returned but not searched or sorted by: SciX does not index it. "
                f"To read it, select it: {_model_name(self.field)}.select({label}, ...)"
            )

    def _wrap(self, node: Expr) -> Expr:
        """Apply this handle's position and relation to a node built on its field.

        Every expression a handle builds passes through here, so this is also
        where a field that cannot be searched is refused.
        """
        self._searchable()
        if self.position is not None:
            node = Func("pos", (node, *self.position))
        if self.via is not None:
            node = Related(self.via, node)
        if self.scope is not None:
            node = self.scope._enclose(node)
        return node

    def _check(self, value: Any) -> Any:
        """Validate a value for this field and return it unchanged."""
        if value is None:
            raise QueryError(f"Comparing {self!r} with None is not supported")
        if isinstance(value, (FieldExpr, Expr)):
            raise TypeError(f"{self!r} can only be compared with values, not with {value!r}")
        self.field.to_solr(value)
        return self.field.normalise(value)

    def __eq__(self, value: object) -> Expr:  # type: ignore[override]  # builds an Expr, not a bool
        return self._wrap(Match(self.field, self._check(value)))

    def __ne__(self, value: object) -> Expr:  # type: ignore[override]  # builds an Expr, not a bool
        return ~(self == value)

    __hash__ = object.__hash__

    def _values(self, values: Iterable[Any]) -> tuple[Any, ...]:
        """Validate the collection given to ``in_`` and return its values."""
        if isinstance(values, (str, bytes)):
            raise TypeError(
                f"{self!r}.in_() needs a collection of values, not a single string; "
                f"use == to compare with one value"
            )
        if isinstance(values, (FieldExpr, Expr)) or not isinstance(values, Iterable):
            raise TypeError(f"{self!r}.in_() needs a collection of values, not {values!r}")
        return tuple(self._check(v) for v in values)

    def in_(self, values: Iterable[Any]) -> Expr:
        """Match any of ``values``."""
        return self._wrap(In(self.field, self._values(values)))

    def not_in(self, values: Iterable[Any]) -> Expr:
        """Match none of ``values``."""
        return ~self.in_(values)

    def _order(self, descending: bool) -> Order:
        self._searchable()
        if self.via is not None or self.position is not None or self.scope is not None:
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
        if self.via is not None:
            owner = self.via.owner.__name__ + "." + self.via.name
            if self.scope is not None:
                owner = f"{self.scope._label()}.{self.via.name}"
        else:
            owner = _model_name(self.field) if self.scope is None else self.scope._label()
        text = f"{owner}.{self.field.name}"
        if self.position is not None:
            start, end = self.position
            text += f"[{start - 1}]" if start == end else f"[{start - 1}:{end}]"
        return f"<{text}>"


class TextExpr(FieldExpr):
    """Handle on a text, name or string field: equality, wildcard and position queries."""

    def __eq__(self, value: object) -> Expr:  # type: ignore[override]  # builds an Expr, not a bool
        """Match ``value`` with ADS's default expansion for the field's kind.

        A text field matches the phrase and its synonyms; a name field also
        matches name variants (``author == "Murray, S"`` matches
        ``"Murray, Stephen S."`` and ``"Murray, Sarah"``); a string field
        matches the whole value literally.
        """
        return super().__eq__(value)

    __hash__ = FieldExpr.__hash__

    def exact(self, value: str) -> Expr:
        """Match ``value`` with no expansion (Solr ``=field:"value"``).

        A text field then matches the phrase without synonyms; a name field
        matches only that form of the name, ignoring case and the periods after
        initials (``author.exact("Murray, S")`` matches ``"Murray, S."`` but not
        ``"Murray, Stephen"``). On a string field this is the same as ``==``.
        """
        return self._wrap(Match(self.field, self._check(value), exact=True))

    def in_(self, values: Iterable[Any], *, exact: bool = False) -> Expr:
        """Match any of ``values``: each as ``==`` would, or as :meth:`exact` would if ``exact``."""
        return self._wrap(In(self.field, self._values(values), exact=exact))

    def not_in(self, values: Iterable[Any], *, exact: bool = False) -> Expr:
        """Match none of ``values`` (compared as by :meth:`in_`)."""
        return ~self.in_(values, exact=exact)

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
        return type(self)(self.field, via=self.via, position=parse_positions(index), scope=self.scope)


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


def parse_positions(index: int | slice) -> tuple[int, int]:
    """The 1-indexed inclusive ``(start, end)`` positions selected by ``index``: ``[0]``, ``[0:3]``.

    Raises ``TypeError`` for an index that is not an int or a slice of ints, and
    :class:`~ads.exceptions.QueryError` for a negative or open-ended one (ADS
    cannot search by position from the end), an empty slice or a step.
    """
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
        return (start + 1, stop)
    if isinstance(index, int) and not isinstance(index, bool):
        if index < 0:
            raise QueryError("ADS cannot search by position from the end")
        return (index + 1, index + 1)
    raise TypeError(f"Positions must be ints or slices, not {index!r}")


def _model_name(field: Field[Any, Any]) -> str:
    model = getattr(field, "model", None)
    return model.__name__ if model is not None else "?"


T = TypeVar("T")
H = TypeVar("H", bound=FieldExpr)

_UNCONVERTIBLE = (ValueError, TypeError, ArithmeticError)


class _Record(Protocol):
    """What a field's owner instances provide (``Model`` does)."""

    def _get_value(self, field: Field[Any, Any], /) -> Any: ...


class Scope(Protocol):
    """Record(s) of a collection that a field handle is restricted to (``Document.authors[0]``)."""

    def _enclose(self, node: Expr, /) -> Expr:
        """Wrap a condition on one record's field into a condition on the owner."""
        ...

    def _label(self) -> str:
        """How the scope is written, e.g. ``"Document.authors[0]"``."""
        ...


@dataclass(frozen=True)
class FieldInfo:
    """A readable description of one field of a model (see ``Model.describe()``)."""

    name: str
    type: str
    multivalued: bool
    stored: bool
    solr_name: str
    doc: str
    match_kind: str
    indexed: bool = True


class Field(Generic[T, H]):
    """A field of a model: a data descriptor and the definition used in expressions.

    ``T`` is the type of the value on an instance (``list[...]`` for a
    multivalued field) and ``H`` the type of handle returned by class access.
    Instance access gives ``T | None``; class access gives an ``H``.

    :param doc: help text, shown by ``help()``.
    :param multivalued: the field holds a list of values.
    :param stored: the field can be returned by the API (``False`` for search-only fields).
    :param indexed: the field can be searched and sorted by (``False`` for fields that
        are only returned); building an expression or sort key on it raises
        :class:`~ads.exceptions.QueryError`.
    :param solr_name: the field's name in Solr, if different from the attribute name.
    """

    expr_class: ClassVar[type[FieldExpr]] = FieldExpr
    #: The Python type of one value (one element, for a multivalued field).
    python_type: ClassVar[type] = object
    #: How ADS matches values: ``"keyword"`` (literally), ``"text"`` (tokenized,
    #: with synonyms) or ``"name"`` (with name variants). See the module docstring.
    match_kind: ClassVar[str] = "keyword"
    name: str
    model: type

    def __init__(
        self,
        doc: str = "",
        *,
        multivalued: bool = False,
        stored: bool = True,
        indexed: bool = True,
        solr_name: str | None = None,
    ) -> None:
        self.__doc__ = doc
        self.multivalued = multivalued
        self.stored = stored
        self.indexed = indexed
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

    @property
    def type_name(self) -> str:
        """The value type as text, e.g. ``"int"`` or ``"list[str]"``."""
        name = self.python_type.__name__
        return f"list[{name}]" if self.multivalued else name

    def info(self) -> FieldInfo:
        """Describe this field."""
        return FieldInfo(
            name=self.name,
            type=self.type_name,
            multivalued=self.multivalued,
            stored=self.stored,
            solr_name=self.solr_name,
            doc=self.__doc__ or "",
            match_kind=self.match_kind,
            indexed=self.indexed,
        )

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> H: ...

    @overload
    def __get__(self, instance: _Record, owner: type | None = None) -> T | None: ...

    # Only for type checkers: mypy applies ``__get__`` to the value of a property
    # that returns a field (e.g. ``Relation.related_field``); this keeps that a field.
    @overload
    def __get__(self, instance: object, owner: type | None = None) -> Field[T, H]: ...

    def __get__(self, instance: Any, owner: type | None = None) -> H | T | None | Field[T, H]:
        if instance is None:
            return cast(H, self.expr_class(self))
        return cast("T | None", instance._get_value(self))

    def __set__(self, instance: Any, value: T | None) -> None:
        instance._set_value(self, value)

    def to_python(self, raw: Any) -> T | None:
        """Convert a value from the API to ``T | None``; never returns anything else.

        Safe, lossless coercions are applied (see each field class). A scalar
        given for a multivalued field becomes a one-element list, and a
        one-element list given for a scalar field becomes its element. A value
        that still cannot be converted, including a list with any unconvertible
        or ``None`` element, becomes ``None`` with a
        :class:`~ads.exceptions.DataWarning`.
        """
        if raw is None:
            return None
        try:
            if self.multivalued:
                items = raw if isinstance(raw, (list, tuple)) else [raw]
                return cast(T, [self._convert(item) for item in items])
            if isinstance(raw, (list, tuple)):
                if len(raw) != 1:
                    raise ValueError("expected a single value")
                if raw[0] is None:
                    return None
                raw = raw[0]
            return cast(T, self._convert(raw))
        except _UNCONVERTIBLE:
            text = repr(raw)
            if len(text) > 80:
                text = text[:77] + "..."
            warnings.warn(
                f"{self._label()}: cannot convert {text} to {self.type_name}; using None",
                DataWarning,
                stacklevel=2,
            )
            return None

    def _convert(self, raw: Any) -> Any:
        """Convert one non-list value, raising ValueError/TypeError if it cannot be."""
        if raw is None:
            raise TypeError("None is not a value")
        value = self._to_python(raw)
        if not isinstance(value, self.python_type):
            raise TypeError(f"converted to {type(value).__name__}")
        return value

    def _to_python(self, raw: Any) -> Any:
        return raw

    def normalise(self, value: Any) -> Any:
        """The form in which this field holds ``value``, in expressions and on instances.

        Most fields hold values exactly as given; a field accepting several
        spellings of one value (:class:`NumericStringField`) returns its canonical form.
        """
        return value

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


# Each concrete field class repeats two ``__init__`` overloads for the type
# checker only: ``multivalued=True`` makes the value type ``list[...]``. The
# overloads must name the concrete class, because type checkers cannot infer a
# subclass's type parameter from self-typed overloads inherited from a base.


class _StringLikeField(Field[T, TextExpr]):
    """Shared behaviour of fields holding strings."""

    expr_class = TextExpr
    python_type = str

    def _to_python(self, raw: Any) -> Any:
        """Accept a str, or an int/float (ADS sometimes returns numbers for string fields)."""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return str(raw)
        raise TypeError("not a string")

    def _to_solr(self, value: Any) -> str:
        if not isinstance(value, str):
            raise self._reject(value, "a str")
        if not value.strip():
            raise self._reject(value, "a non-empty str")
        return value


class TextField(_StringLikeField[T]):
    """Tokenized full text (title, abstract, ...); match kind ``"text"``.

    ``==`` matches the phrase, and ADS also matches its synonyms and acronyms
    (``title == "star"`` also finds synonyms of "star"); ``exact`` turns that
    expansion off.
    """

    match_kind = "text"

    if TYPE_CHECKING:

        @overload
        def __init__(
            self: TextField[str],
            doc: str = ...,
            *,
            multivalued: Literal[False] = ...,
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        @overload
        def __init__(
            self: TextField[list[str]],
            doc: str = ...,
            *,
            multivalued: Literal[True],
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...


class StringField(_StringLikeField[T]):
    """An untokenized keyword (bibcode, doctype, ...); match kind ``"keyword"``.

    ``==`` matches the whole value literally, so ``exact`` changes nothing
    (and no ``=`` is sent: ADS returns nothing for ``=bibcode:...``).
    """

    match_kind = "keyword"

    if TYPE_CHECKING:

        @overload
        def __init__(
            self: StringField[str],
            doc: str = ...,
            *,
            multivalued: Literal[False] = ...,
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        @overload
        def __init__(
            self: StringField[list[str]],
            doc: str = ...,
            *,
            multivalued: Literal[True],
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...


class NumericStringField(StringField[str]):
    """A string of digits, such as ``Document.id``; queries also accept a non-negative int.

    The value is a ``str`` (as the API returns it), and an int given in a query
    or set on an instance is held as its decimal string, so
    ``Document.id == 12312494`` is the same condition as ``Document.id == "12312494"``.
    Bools, floats and negative ints are refused.
    """

    def __init__(
        self,
        doc: str = "",
        *,
        stored: bool = True,
        indexed: bool = True,
        solr_name: str | None = None,
    ) -> None:
        super().__init__(doc, stored=stored, indexed=indexed, solr_name=solr_name)

    def normalise(self, value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return str(value)
        return value

    def _to_solr(self, value: Any) -> str:
        value = self.normalise(value)
        if not isinstance(value, str):
            raise self._reject(value, "a str or a non-negative int")
        return super()._to_solr(value)


class NameField(_StringLikeField[T]):
    """A personal name, ``"Last, First M"`` (author, editor, ...); match kind ``"name"``.

    ``==`` matches name variants as ADS does: ``"Murray, S"`` matches
    ``"Murray, Stephen S."`` and ``"Murray, Sarah"``, while ``"Murray, Stephen"``
    matches ``"Murray, S. D."`` but not ``"Murray, Sarah"``. ``exact`` matches
    only that form of the name, ignoring case and the periods after initials:
    ``exact("Murray, S")`` matches ``"Murray, S."`` and nothing else above.
    """

    match_kind = "name"

    if TYPE_CHECKING:

        @overload
        def __init__(
            self: NameField[str],
            doc: str = ...,
            *,
            multivalued: Literal[False] = ...,
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        @overload
        def __init__(
            self: NameField[list[str]],
            doc: str = ...,
            *,
            multivalued: Literal[True],
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...


class VirtualField(_StringLikeField[NoReturn]):
    """A search-only text field (such as ``full``); it never has a value on an instance.

    Matched like a :class:`TextField` (match kind ``"text"``).
    """

    match_kind = "text"

    def __init__(self, doc: str = "", *, multivalued: bool = False, solr_name: str | None = None) -> None:
        super().__init__(doc, multivalued=multivalued, stored=False, solr_name=solr_name)

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> TextExpr: ...

    @overload
    def __get__(self, instance: object, owner: type | None = None) -> NoReturn: ...

    def __get__(self, instance: Any, owner: type | None = None) -> TextExpr:
        if instance is None:
            return self.expr_class(self)
        raise AttributeError(f"{self._label()} is search-only and has no value on an instance")

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError(f"{self._label()} is search-only and cannot be set")


class IntegerField(Field[T, NumericExpr]):
    """An integer. Integral floats and strings from the API (``12.0``, ``"12"``) are accepted."""

    expr_class = NumericExpr
    python_type = int

    if TYPE_CHECKING:

        @overload
        def __init__(
            self: IntegerField[int],
            doc: str = ...,
            *,
            multivalued: Literal[False] = ...,
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        @overload
        def __init__(
            self: IntegerField[list[int]],
            doc: str = ...,
            *,
            multivalued: Literal[True],
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    def _to_python(self, raw: Any) -> Any:
        if isinstance(raw, bool):
            raise TypeError("a bool is not an int")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float):
            if raw.is_integer():
                return int(raw)
            raise ValueError("not integral")
        if isinstance(raw, str):
            number = Decimal(raw)
            if number.is_finite() and number == number.to_integral_value():
                return int(number)
            raise ValueError("not integral")
        raise TypeError("not a number")

    def _to_solr(self, value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, int):
            raise self._reject(value, "an int")
        return str(value)

    def step(self, value: Any, direction: int) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value + direction


class FloatField(Field[T, NumericExpr]):
    """A floating-point number. Ints and numeric strings from the API are accepted."""

    expr_class = NumericExpr
    python_type = float

    if TYPE_CHECKING:

        @overload
        def __init__(
            self: FloatField[float],
            doc: str = ...,
            *,
            multivalued: Literal[False] = ...,
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        @overload
        def __init__(
            self: FloatField[list[float]],
            doc: str = ...,
            *,
            multivalued: Literal[True],
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    def _to_python(self, raw: Any) -> Any:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise TypeError("not a number")
        return float(raw)

    def _to_solr(self, value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise self._reject(value, "a finite number")
        return repr(value)


class BooleanField(Field[T, FieldExpr]):
    """A boolean. ``"true"``/``"false"`` strings (any case) from the API are accepted."""

    python_type = bool

    if TYPE_CHECKING:

        @overload
        def __init__(
            self: BooleanField[bool],
            doc: str = ...,
            *,
            multivalued: Literal[False] = ...,
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        @overload
        def __init__(
            self: BooleanField[list[bool]],
            doc: str = ...,
            *,
            multivalued: Literal[True],
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    def _to_python(self, raw: Any) -> Any:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        raise TypeError("not a boolean")

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


class DateTimeField(Field[T, NumericExpr]):
    """A timestamp, held as an aware UTC ``datetime``."""

    expr_class = NumericExpr
    python_type = datetime

    if TYPE_CHECKING:

        @overload
        def __init__(
            self: DateTimeField[datetime],
            doc: str = ...,
            *,
            multivalued: Literal[False] = ...,
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        @overload
        def __init__(
            self: DateTimeField[list[datetime]],
            doc: str = ...,
            *,
            multivalued: Literal[True],
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    def _to_python(self, raw: Any) -> Any:
        if isinstance(raw, date):
            return as_utc(raw)
        if not isinstance(raw, str):
            raise TypeError("not a timestamp")
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


class DateField(Field[T, NumericExpr]):
    """A date with month precision in queries (ADS ``pubdate``), held as a ``date``."""

    expr_class = NumericExpr
    python_type = date

    if TYPE_CHECKING:

        @overload
        def __init__(
            self: DateField[date],
            doc: str = ...,
            *,
            multivalued: Literal[False] = ...,
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        @overload
        def __init__(
            self: DateField[list[date]],
            doc: str = ...,
            *,
            multivalued: Literal[True],
            stored: bool = ...,
            indexed: bool = ...,
            solr_name: str | None = ...,
        ) -> None: ...

        def __init__(self, *args: Any, **kwargs: Any) -> None: ...

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
