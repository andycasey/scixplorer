"""Models (classes of records with fields), relations between them, and collections of records."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, NoReturn, TypeVar, cast, overload

from . import exceptions
from .expr import BOOL_ERROR, And, Constant, Expr, Func, In, InLibrary, Like, Match, Not, Or, Quantified, Range, Raw, Related
from .fields import Field, FieldExpr, FieldInfo, Scope, _quantifier_hint, noun, parse_positions

if TYPE_CHECKING:
    from .query import Backend, Query

__all__ = [
    "Collection",
    "CollectionExpr",
    "Model",
    "ModelMeta",
    "RecordExpr",
    "Relation",
    "RelationExpr",
    "Source",
    "check_predicate",
]

M = TypeVar("M", bound="Model")
R = TypeVar("R")
H = TypeVar("H", bound="RecordExpr")


class Relation(Generic[R]):
    """A link from one model (the owner) to another (the related model).

    ``R`` is what ``resolve`` returns: instance access gives an ``R``, class
    access gives a :class:`RelationExpr`.

    :param model: the related model, or a zero-argument callable returning it
        (for models defined later).
    :param key: the owner's field holding the key(s) of the related object(s).
    :param related_key: the related model's field those keys refer to.
    :param resolve: called with an owner instance; returns the related
        object(s) or ``None``. Called at most once per instance.
    :param doc: help text.
    """

    name: str
    owner: type[Model]

    def __init__(
        self,
        model: type[Model] | Callable[[], type[Model]],
        *,
        key: str,
        related_key: str,
        resolve: Callable[[Any], R],
        doc: str = "",
    ) -> None:
        self._model = model
        self.key = key
        self.related_key = related_key
        self.resolve = resolve
        self.__doc__ = doc

    def __set_name__(self, owner: type, name: str) -> None:
        if hasattr(self, "owner"):
            raise TypeError(
                f"Relation {self.owner.__name__}.{self.name} cannot also be "
                f"{owner.__name__}.{name}; create a separate relation for each class"
            )
        self.owner = owner
        self.name = name

    @property
    def related_model(self) -> type[Model]:
        """The related model class."""
        if not isinstance(self._model, type):
            self._model = self._model()
        return self._model

    @property
    def related_field(self) -> Field[Any, Any]:
        """The field of the related model that ``key`` values refer to."""
        model = self.related_model
        try:
            return model._fields[self.related_key]
        except KeyError:
            raise TypeError(
                f"{self!r}: related_key {self.related_key!r} is not a field of {model.__name__}"
            ) from None

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> RelationExpr: ...

    @overload
    def __get__(self, instance: object, owner: type | None = None) -> R: ...

    def __get__(self, instance: Any, owner: type | None = None) -> RelationExpr | R:
        if instance is None:
            return RelationExpr(self)
        cache: dict[str, Any] = instance._related
        if self.name not in cache:
            cache[self.name] = self.resolve(instance)
        return cast(R, cache[self.name])

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError(f"{self!r} is derived from {self.key!r} and cannot be set")

    def __repr__(self) -> str:
        owner = getattr(self, "owner", None)
        return f"<Relation {owner.__name__ if owner else '?'}.{getattr(self, 'name', '?')}>"


class RelationExpr:
    """A handle on a relation, used to build expressions over related objects.

    ``Document.journal.title`` is a handle on the related model's field, and
    ``Document.journal == journal`` matches by the related object's key. Each
    such condition is about any related object; :meth:`any` combines several
    conditions about the same one.
    """

    __slots__ = ("_relation", "_scope")

    def __init__(self, relation: Relation[Any], *, scope: Scope | None = None) -> None:
        self._relation = relation
        self._scope = scope

    @property
    def relation(self) -> Relation[Any]:
        """The relation this handle is on."""
        return self._relation

    def __getattr__(self, name: str) -> FieldExpr:
        if name.startswith("_"):
            raise AttributeError(name)
        model = self._relation.related_model
        field = model._fields.get(name)
        if field is None:
            if name in ("any", "all"):
                raise AttributeError(_quantifier_hint(None, name, self._relation))
            raise AttributeError(
                f"{model.__name__} has no field {name!r}; fields are: {', '.join(model._fields)}"
            )
        return field.expr_class(field, via=self._relation, scope=self._scope)

    def __dir__(self) -> Iterable[str]:
        return sorted({*object.__dir__(self), *self._relation.related_model._fields})

    def any(self, predicate: Expr) -> Expr:
        """Rows with at least one related object satisfying ``predicate``, a condition on the related model's fields.

        Everything inside is about the same related object, so ::

            Document.planetary_feature.any(
                (PlanetaryFeature.target == "Mars") & (PlanetaryFeature.feature_type == "Crater")
            )

        finds documents mentioning a Mars crater, whereas
        ``(Document.planetary_feature.target == "Mars") & (Document.planetary_feature.feature_type == "Crater")``
        finds those mentioning some Mars feature and some crater (perhaps elsewhere).
        A condition on "every related object" cannot be searched, so there is no ``all``.
        """
        call = f"{self._label()}.any"
        check_predicate(predicate, self._relation.related_model, owner=self._relation.owner, call=call)
        node: Expr = Related(self._relation, predicate)
        return node if self._scope is None else self._scope._enclose(node)

    def _label(self) -> str:
        owner = self._relation.owner.__name__ if self._scope is None else self._scope._label()
        return f"{owner}.{self._relation.name}"

    def _key_handle(self) -> FieldExpr:
        field = self._relation.related_field
        return field.expr_class(field, via=self._relation, scope=self._scope)

    def _key_of(self, other: Any) -> Any:
        model = self._relation.related_model
        if isinstance(other, Model):
            if not isinstance(other, model):
                raise TypeError(f"{self!r} relates to {model.__name__}, not {type(other).__name__}")
            key = other._data.get(self._relation.related_key)
            if key is None:
                raise exceptions.QueryError(
                    f"{other!r} has no {self._relation.related_key!r}, so it cannot be matched"
                )
            return key
        if isinstance(other, (RelationExpr, FieldExpr, Expr)):
            raise TypeError(f"{self!r} can only be compared with {model.__name__} objects or keys")
        return other

    def __eq__(self, other: object) -> Expr:  # type: ignore[override]  # builds an Expr, not a bool
        key: object = self._key_of(other)
        return self._key_handle() == key

    def __ne__(self, other: object) -> Expr:  # type: ignore[override]  # builds an Expr, not a bool
        return ~(self == other)

    __hash__ = object.__hash__

    def in_(self, objs: Iterable[Any]) -> Expr:
        """Match related objects that are any of ``objs`` (objects or keys)."""
        if isinstance(objs, (str, bytes)) or not isinstance(objs, Iterable):
            raise TypeError(f"{self!r}.in_() needs a collection of objects or keys, not {objs!r}")
        return self._key_handle().in_([self._key_of(obj) for obj in objs])

    def not_in(self, objs: Iterable[Any]) -> Expr:
        """Match unless the related object is one of ``objs``."""
        return ~self.in_(objs)

    def __bool__(self) -> bool:
        raise TypeError(BOOL_ERROR.format(name=f"Relation {self!r}"))

    def __iter__(self) -> Any:
        raise TypeError(f"{self!r} is not iterable; use .in_(objects) to match several objects")

    def __repr__(self) -> str:
        return f"<{self._label()}>"


def check_predicate(predicate: Any, model: type[Model], *, owner: type | None, call: str, positions: str = "") -> Expr:
    """``predicate``, checked to be a condition on one ``model`` object, for ``call`` (``"Document.authors.any"``).

    Its leaves must be on ``model``'s fields, or go through ``model``'s
    relations. Raises ``TypeError`` for something other than an expression
    (with the and/or/not hint for a ``bool``) and
    :class:`~ads.exceptions.QueryError` for a condition on another model's
    field, or an operator, ``pos()``, raw query or library condition.
    ``owner`` is the model whose rows the call selects, named in the message
    about its fields; ``positions`` is advice on choosing positions, if any.
    """
    name = model.__name__
    if isinstance(predicate, bool):
        raise TypeError(
            f"{call}(...) got the bool {predicate!r} instead of an expression. This usually means "
            f"and/or/not, `is` or a comparison between plain values was used: combine expressions "
            f"with & | ~ and wrap each comparison in parentheses, e.g. "
            f"{call}(({name}.<field> == x) & ({name}.<field> == y))."
        )
    if not isinstance(predicate, Expr):
        raise TypeError(f"{call}(...) needs an expression over {name} fields, not {predicate!r}")
    _check_node(predicate, model, owner, f"{call}(...)", positions)
    return predicate


def _check_node(node: Expr, model: type[Model], owner: type | None, call: str, positions: str) -> None:
    if isinstance(node, (And, Or)):
        for operand in node.operands:
            _check_node(operand, model, owner, call, positions)
    elif isinstance(node, Not):
        _check_node(node.operand, model, owner, call, positions)
    elif isinstance(node, Related):
        if model._relations.get(node.relation.name) is not node.relation:
            raise exceptions.QueryError(
                f"{call}: {node.relation!r} is not a relation of {model.__name__}; inside it, "
                f"use {model.__name__}'s fields and relations ({', '.join([*model._fields, *model._relations])})"
            )
    elif isinstance(node, (Match, In, Like, Range)):
        if model._fields.get(node.field.name) is not node.field:
            raise exceptions.QueryError(_foreign(node.field, model, owner, call))
    elif isinstance(node, Func) and node.name == "pos":
        raise exceptions.QueryError(
            f"{call} cannot restrict positions within one {noun(model)}'s values" + (f"; {positions}" if positions else "")
        )
    elif not isinstance(node, Constant):
        kind = {Func: "an operator", Raw: "raw Solr text", InLibrary: "a library condition"}
        raise exceptions.QueryError(
            f"{call} cannot contain {kind.get(type(node), 'a nested .any()/.all()')} ({node!r}): "
            f"everything inside it is a condition on one {noun(model)}, built from "
            f"{model.__name__} fields and relations."
        )


def _foreign(field: Field[Any, Any], model: type[Model], owner: type | None, call: str) -> str:
    """Why a leaf on ``field`` (not a field of ``model``) cannot be in ``call``."""
    other = getattr(field, "model", None)
    name = f"{other.__name__ if other else '?'}.{field.name}"
    if other is not None and owner is not None and issubclass(owner, other):
        return (
            f"{name} is a field of {other.__name__}, not of {model.__name__}: everything inside "
            f"{call} is about one {noun(model)}. Put conditions on the "
            f"{noun(other)} outside it, e.g. {call} & ({name} == ...)"
        )
    through = [rel for rel in model._relations.values() if rel.related_model is other]
    if through:
        return (
            f"{name} is not a field of {model.__name__}; reach it through the relation "
            f"{model.__name__}.{through[0].name}, e.g. {model.__name__}.{through[0].name}.{field.name} == ..."
        )
    return f"{name} is not a field of {model.__name__}; inside {call} use: {', '.join(model._fields)}"


@dataclass(frozen=True)
class Source:
    """Where the value of one record field of a :class:`Collection` comes from on the owner.

    :param fields: the owner's array fields holding the value, index-aligned
        with the records; with several, the first that has a value for the
        record wins.
    :param search: the owner field that a condition on the record field is
        searched through, or ``None`` if the server cannot search it (such a
        condition is then checked client-side).
    """

    fields: tuple[str, ...]
    search: str | None = None


class Collection(Generic[M, H]):
    """Records that an owner holds as index-aligned arrays, such as the authors of a document.

    ADS stores per-author values as parallel arrays on the document (author
    names, affiliations, ORCIDs, ...), entry ``i`` of each describing author
    ``i``. A collection turns them into one record (an instance of ``model``)
    per entry, and lets a query say several things about the *same* record:
    ``Document.authors.any((Author.name == x) & (Author.aff == y))``.

    Instance access gives ``list[M]`` (cached on the instance until any of its
    fields changes); class access gives a :class:`CollectionExpr`.

    :param model: the record model (a model with no backend of its own).
    :param sources: for each record field, the owner field it comes from (a
        name: read and searched), or a :class:`Source`. Record fields left out
        have no value, except ``index``.
    :param length: the owner field whose number of values is the number of records.
    :param handle: the handle class for one record (``Document.authors[0]``): a
        :class:`RecordExpr` subclass, which may declare the handle type of each
        record field for type checkers.
    :param index: the record field set to the record's 0-based position, if any.
    :param placeholder: an array entry meaning "no value".
    :param separator: separates several values in one entry, for a multivalued record field.
    :param doc: help text.
    """

    name: str
    owner: type[Model]

    def __init__(
        self,
        model: type[M],
        *,
        sources: Mapping[str, str | Source],
        length: str,
        handle: type[H],
        index: str | None = None,
        placeholder: str = "-",
        separator: str = ";",
        doc: str = "",
    ) -> None:
        self.record_model = model
        self.sources = {
            name: Source((source,), source) if isinstance(source, str) else source for name, source in sources.items()
        }
        self.length = length
        self.handle = handle
        self.index = index
        self.placeholder = placeholder
        self.separator = separator
        self.__doc__ = doc
        for name in (*self.sources, *([index] if index else [])):
            if name not in model._fields:
                raise TypeError(f"Collection of {model.__name__}: {name!r} is not a field of {model.__name__}")

    def __set_name__(self, owner: type, name: str) -> None:
        if hasattr(self, "owner"):
            raise TypeError(
                f"Collection {self.owner.__name__}.{self.name} cannot also be "
                f"{owner.__name__}.{name}; create a separate collection for each class"
            )
        self.owner = owner
        self.name = name

    def owner_fields(self, names: Iterable[str] | None = None) -> tuple[str, ...]:
        """The owner fields read to build records with the record fields ``names`` (all if ``None``).

        Always includes :attr:`length`; each name appears once, in first-use order.
        """
        wanted = self.sources if names is None else {n: self.sources[n] for n in names if n in self.sources}
        return tuple(dict.fromkeys([self.length, *(f for source in wanted.values() for f in source.fields)]))

    def fields_read(self, predicate: Expr) -> tuple[str, ...]:
        """The record fields that evaluating ``predicate`` on a record reads (a relation reads its key)."""
        found: dict[str, None] = {}

        def visit(node: Expr) -> None:
            if isinstance(node, (And, Or)):
                for operand in node.operands:
                    visit(operand)
            elif isinstance(node, Not):
                visit(node.operand)
            elif isinstance(node, Related):
                found.setdefault(node.relation.key)
            elif isinstance(node, (Match, In, Like, Range)):
                found.setdefault(node.field.name)

        visit(predicate)
        return tuple(found)

    def label(self, positions: tuple[int, int] | None = None) -> str:
        """How the collection (restricted to ``positions``) is written: ``Document.authors[0]``."""
        owner = getattr(self, "owner", None)
        text = f"{owner.__name__ if owner else '?'}.{getattr(self, 'name', '?')}"
        if positions is not None:
            start, end = positions
            text += f"[{start - 1}]" if start == end else f"[{start - 1}:{end}]"
        return text

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> CollectionExpr[H]: ...

    @overload
    def __get__(self, instance: object, owner: type | None = None) -> list[M]: ...

    def __get__(self, instance: Any, owner: type | None = None) -> CollectionExpr[H] | list[M]:
        if instance is None:
            return CollectionExpr(self)
        cache: dict[str, Any] = instance._related
        if self.name not in cache:
            records = self.records(instance)
            instance._related[self.name] = records
        return cast("list[M]", instance._related[self.name])

    def __set__(self, instance: Any, value: Any) -> None:
        raise AttributeError(f"{self!r} is derived from {', '.join(self.owner_fields())} and cannot be set")

    def records(self, owner: Model, names: Iterable[str] | None = None) -> list[M]:
        """The records of ``owner``, in order, with the record fields ``names`` set (all if ``None``).

        Missing owner fields are loaded first, all at once (see
        ``Model._load_fields``). Entry ``i`` of each source array gives record
        ``i``'s value; a missing or short array, or the placeholder, gives no
        value (``None``, or an empty list for a multivalued record field, whose
        entries are split on the separator). Records already built for the
        instance (by attribute access) are returned as they are.
        """
        if not isinstance(owner, self.owner):
            raise TypeError(f"{self!r} holds records of {self.owner.__name__} objects, not of {owner!r}")
        if self.name in owner._related:
            return cast("list[M]", owner._related[self.name])
        wanted = self.sources if names is None else {n: self.sources[n] for n in names if n in self.sources}
        fields = [owner._fields[name] for name in self.owner_fields(wanted)]
        owner._load_fields(fields)
        arrays = {field.name: owner._get_value(field) for field in fields}
        lengths = arrays[self.length]
        model = self.record_model
        records: list[M] = []
        for i in range(len(lengths) if isinstance(lengths, (list, tuple)) else 0):
            record = model()
            if self.index is not None:
                record._data[self.index] = i
            for name, source in wanted.items():
                field = model._fields[name]
                values = [self._convert(field, _entry(arrays[f], i)) for f in source.fields]
                present = [value for value in values if value is not None]
                record._data[name] = next((v for v in present if v != []), present[0] if present else None)
            records.append(record)
        return records

    def _convert(self, field: Field[Any, Any], raw: Any) -> Any:
        """One array entry as the value of the record field ``field``."""
        if raw is None:
            return None
        if isinstance(raw, str):
            if field.multivalued:
                parts = (part.strip() for part in raw.split(self.separator))
                raw = [part for part in parts if part and part != self.placeholder]
            elif raw.strip() in ("", self.placeholder):
                return None
        return field.to_python(raw)

    def quantify(self, predicate: Any, quantifier: Literal["any", "all"], positions: tuple[int, int] | None) -> Expr:
        """``Quantified(self, predicate, quantifier, positions)``, after checking ``predicate``.

        Raises ``TypeError`` for something other than an expression and
        :class:`~ads.exceptions.QueryError` for an expression that is not
        about one record: a condition on another model's field (other than
        through a relation of the record model), or an operator, raw query or
        library condition.
        """
        record = noun(self.record_model)
        advice = (
            f"choose {record}s by position by indexing the collection: "
            f"{self.label()}[0].any(...), {self.label()}[0:3].any(...)"
        )
        call = f"{self.label(positions)}.{quantifier}"
        check_predicate(predicate, self.record_model, owner=self.owner, call=call, positions=advice)
        return Quantified(self, predicate, quantifier, positions)

    def __repr__(self) -> str:
        return f"<Collection {self.label()}>"


def _entry(array: Any, i: int) -> Any:
    """Entry ``i`` of an owner's array value, or ``None`` if it is missing or too short."""
    if isinstance(array, (list, tuple)) and i < len(array):
        return array[i]
    return None


class CollectionExpr(Generic[H]):
    """A handle on a :class:`Collection` (``Document.authors``), used to build per-record conditions.

    ``any(predicate)`` / ``all(predicate)`` match owners for which some / every
    record satisfies ``predicate`` (``all`` needs at least one record).
    Everything inside one call is about the same record. Index to restrict the
    records by position: ``[0]`` (one record: also allows attribute shorthand,
    ``Document.authors[0].name == x``) or ``[0:3]``.
    """

    __slots__ = ("_collection", "_positions")

    def __init__(self, collection: Collection[Any, H], positions: tuple[int, int] | None = None) -> None:
        self._collection = collection
        self._positions = positions

    @property
    def collection(self) -> Collection[Any, H]:
        """The collection this handle is on."""
        return self._collection

    @property
    def positions(self) -> tuple[int, int] | None:
        """The 1-indexed inclusive positions selected, or ``None`` for all records."""
        return self._positions

    def any(self, predicate: Expr) -> Expr:
        """Owners for which at least one (selected) record satisfies ``predicate``."""
        return self._collection.quantify(predicate, "any", self._positions)

    def all(self, predicate: Expr) -> Expr:
        """Owners that have (selected) records and for which every one satisfies ``predicate``."""
        return self._collection.quantify(predicate, "all", self._positions)

    @overload
    def __getitem__(self, index: int) -> H: ...

    @overload
    def __getitem__(self, index: slice) -> CollectionExpr[H]: ...

    def __getitem__(self, index: int | slice) -> H | CollectionExpr[H]:
        """Restrict to positions: ``[0]`` (one record) or ``[0:3]``; as for field positions."""
        if self._positions is not None:
            raise TypeError(f"{self!r} is already restricted to positions")
        selected = parse_positions(index)
        if isinstance(index, slice):
            return CollectionExpr(self._collection, selected)
        return self._collection.handle(self._collection, selected)

    if not TYPE_CHECKING:
        # Runtime only, so that type checkers still flag unknown attributes.
        def __getattr__(self, name: str) -> NoReturn:
            if name.startswith("_"):
                raise AttributeError(name)
            model = self._collection.record_model
            if name not in model._fields and name not in model._relations:
                raise AttributeError(f"{self!r} has no attribute {name!r}; use .any(...) or .all(...)")
            label, record = self._label(), model.__name__
            raise AttributeError(
                f"{label}.{name}: say which {record.lower()}s the condition is about. Use "
                f"{label}.any({record}.{name} == ...) for any {record.lower()}, "
                f"{label}.all({record}.{name} == ...) for every one, or index one position, "
                f"e.g. {self._collection.label()}[0].{name} == ..."
            )

    def _label(self) -> str:
        if self._positions is None:
            return self._collection.label()
        start, end = self._positions
        return f"{self._collection.label()}[{start - 1}:{end}]"

    def __dir__(self) -> Iterable[str]:
        return sorted({*object.__dir__(self), "any", "all"})

    def __eq__(self, other: object) -> NoReturn:
        raise TypeError(
            f"{self!r} cannot be compared with a value; compare a field of one record inside "
            f".any(...) or .all(...), e.g. {self._label()}.any({self._collection.record_model.__name__}.<field> == x)"
        )

    def __ne__(self, other: object) -> NoReturn:
        self.__eq__(other)

    __hash__ = object.__hash__

    def __bool__(self) -> bool:
        raise TypeError(BOOL_ERROR.format(name=f"Collection {self!r}"))

    def __iter__(self) -> Any:
        raise TypeError(f"{self!r} is not iterable; use .any(...) or .all(...) to build a condition")

    def __repr__(self) -> str:
        return f"<{self._label()}>"


class RecordExpr(CollectionExpr[Any]):
    """A handle on one record of a collection, by position: ``Document.authors[0]``.

    Attribute access gives handles on the record's fields and relations, whose
    conditions are about that record: ``Document.authors[0].name == x`` is
    ``Document.authors[0].any(Author.name == x)``. Subclasses may declare those
    attributes' types for type checkers.
    """

    __slots__ = ()

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> FieldExpr | RelationExpr:
            if name.startswith("_"):
                raise AttributeError(name)
            model = self._collection.record_model
            field = model._fields.get(name)
            if field is not None:
                return field.expr_class(field, scope=self)
            relation = model._relations.get(name)
            if relation is not None:
                return RelationExpr(relation, scope=self)
            raise AttributeError(
                f"{model.__name__} has no field {name!r}; fields are: "
                f"{', '.join([*model._fields, *model._relations])}"
            )

    def _enclose(self, node: Expr) -> Expr:
        """A condition on this record's field, as a condition on the owner."""
        return self._collection.quantify(node, "any", self._positions)

    def _label(self) -> str:
        return self._collection.label(self._positions)

    def __dir__(self) -> Iterable[str]:
        model = self._collection.record_model
        return sorted({*super().__dir__(), *model._fields, *model._relations})


class ModelMeta(type):
    """Metaclass collecting a model's fields and relations.

    Sets ``_fields``, ``_relations`` and ``_collections`` (dicts in definition
    order, inherited entries first) and a per-model ``DoesNotExist`` exception class.
    """

    _fields: dict[str, Field[Any, Any]]
    _relations: dict[str, Relation[Any]]
    _collections: dict[str, Collection[Any, Any]]
    DoesNotExist: type[exceptions.DoesNotExist]

    def __init__(cls, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(name, bases, namespace, **kwargs)
        fields: dict[str, Field[Any, Any]] = {}
        relations: dict[str, Relation[Any]] = {}
        collections: dict[str, Collection[Any, Any]] = {}
        for base in reversed(bases):
            fields.update(getattr(base, "_fields", {}))
            relations.update(getattr(base, "_relations", {}))
            collections.update(getattr(base, "_collections", {}))
        for attr, value in namespace.items():
            if isinstance(value, Field):
                fields[attr] = value
            elif isinstance(value, Relation):
                relations[attr] = value
            elif isinstance(value, Collection):
                collections[attr] = value
        cls._fields = fields
        cls._relations = relations
        cls._collections = collections

        key = namespace.get("_key")
        if key is not None and key not in fields:
            raise TypeError(f"{name}._key names {key!r}, which is not a field of {name}")
        for relation in relations.values():
            if relation.key not in fields:
                raise TypeError(
                    f"{name}.{relation.name} uses key {relation.key!r}, which is not a field of {name}"
                )
        for collection in collections.values():
            used = [*collection.owner_fields(), *(s.search for s in collection.sources.values() if s.search)]
            for field_name in used:
                if field_name not in fields:
                    raise TypeError(
                        f"{name}.{collection.name} reads {field_name!r}, which is not a field of {name}"
                    )

        parent = next(
            (base.DoesNotExist for base in bases if isinstance(base, ModelMeta)),
            exceptions.DoesNotExist,
        )
        cls.DoesNotExist = type(
            "DoesNotExist",
            (parent,),
            {"__module__": cls.__module__, "__qualname__": f"{cls.__qualname__}.DoesNotExist"},
        )


class Model(metaclass=ModelMeta):
    """Base class for models. Field values are passed as keyword arguments."""

    _key: ClassVar[str]
    _backend: ClassVar[Backend]
    _fields: ClassVar[dict[str, Field[Any, Any]]]
    _relations: ClassVar[dict[str, Relation[Any]]]
    _collections: ClassVar[dict[str, Collection[Any, Any]]]
    DoesNotExist: ClassVar[type[exceptions.DoesNotExist]]

    def __init__(self, **values: Any) -> None:
        self._data: dict[str, Any] = {}
        self._related: dict[str, Any] = {}
        for name, value in values.items():
            field = self._fields.get(name)
            if field is None:
                raise TypeError(
                    f"{type(self).__name__}() got an unexpected field {name!r}; "
                    f"fields are: {', '.join(self._fields)}"
                )
            if not field.stored:
                raise TypeError(f"{type(self).__name__}.{name} is search-only and cannot be set")
            self._set_value(field, value)

    @classmethod
    def _from_row(cls: type[M], row: Mapping[str, Any]) -> M:
        """Build an instance from an API mapping keyed by Solr field names.

        Values are converted with ``Field.to_python``; unknown keys are ignored
        and only keys present in ``row`` are set.
        """
        instance = cls()
        for field in cls._fields.values():
            if field.stored and field.solr_name in row:
                instance._data[field.name] = field.to_python(row[field.solr_name])
        return instance

    @classmethod
    def _field(cls, ref: str | Field[Any, Any] | FieldExpr) -> Field[Any, Any]:
        """Resolve a field name, definition or plain handle to a field of this model."""
        if isinstance(ref, FieldExpr):
            if ref.via is not None or ref.position is not None or ref.scope is not None:
                raise TypeError(f"{ref!r} is not a plain field of {cls.__name__}")
            ref = ref.field
        if isinstance(ref, str):
            if ref not in cls._fields:
                raise AttributeError(
                    f"{cls.__name__} has no field {ref!r}; fields are: {', '.join(cls._fields)}"
                )
            return cls._fields[ref]
        if isinstance(ref, Field):
            if cls._fields.get(getattr(ref, "name", "")) is not ref:
                raise TypeError(f"{ref!r} is not a field of {cls.__name__}")
            return ref
        raise TypeError(f"Expected a field of {cls.__name__} or its name, not {ref!r}")

    def _get_value(self, field: Field[Any, Any]) -> Any:
        """Return the stored value of ``field``, loading it if it is missing."""
        try:
            return self._data[field.name]
        except KeyError:
            return self._load_missing(field)

    def _load_missing(self, field: Field[Any, Any]) -> Any:
        """Return the value of a field that was never set. The default is ``None``."""
        return None

    def _load_fields(self, fields: Iterable[Field[Any, Any]]) -> None:
        """Load, at once where possible, those of ``fields`` that were never set.

        Used before reading several fields together (as a :class:`Collection`
        does). The default loads nothing: unset fields read as ``None``.
        """

    def _set_value(self, field: Field[Any, Any], value: Any) -> None:
        """Store ``value`` (as the field normalises it) for ``field``, discarding cached related objects."""
        self._data[field.name] = field.normalise(value)
        self._related.clear()

    def _key_value(self) -> Any:
        key = getattr(type(self), "_key", None)
        return None if key is None else self._data.get(key)

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        key = self._key_value()
        if key is None:
            return self is other
        return bool(key == other._key_value())

    def __hash__(self) -> int:
        key = self._key_value()
        return id(self) if key is None else hash((type(self), key))

    def to_dict(self) -> dict[str, Any]:
        """The values loaded on this instance, by field name in definition order.

        Never loads missing values, and does not include relations.
        """
        return {name: self._data[name] for name in self._fields if name in self._data}

    @classmethod
    def describe(cls) -> list[FieldInfo]:
        """Describe the fields of this model, in definition order."""
        return [field.info() for field in cls._fields.values()]

    def __repr__(self) -> str:
        key = self._key_value()
        if key is None:
            return f"<{type(self).__name__}>"
        return f"<{type(self).__name__}: {self._key}={key!r}>"

    @classmethod
    def select(cls: type[M], *fields: str | Field[Any, Any] | FieldExpr) -> Query[M]:
        """Start a query over this model, fetching ``fields`` (all stored fields if none)."""
        from .query import Query

        return Query(cls, tuple(cls._field(field) for field in fields))

    @classmethod
    def get(cls: type[M], *exprs: Expr, **equals: Any) -> M:
        """Return the single object matching ``exprs`` and ``field=value`` keywords.

        Raises ``cls.DoesNotExist`` if nothing matches.
        """
        for name in equals:
            if name not in cls._fields and name not in cls._relations:
                raise TypeError(f"{cls.__name__}.get() got an unexpected field {name!r}")
        conditions = [*exprs, *(getattr(cls, name) == value for name, value in equals.items())]
        result: M | None = cls.select().where(*conditions).first()
        if result is None:
            raise cls.DoesNotExist(f"No {cls.__name__} matched {conditions!r}")
        return result
