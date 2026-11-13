"""Models (classes of records with fields) and relations between them."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar, cast, overload

from . import exceptions
from .expr import BOOL_ERROR, Expr
from .fields import Field, FieldExpr, FieldInfo

if TYPE_CHECKING:
    from .query import Backend, Query

__all__ = ["Model", "ModelMeta", "Relation", "RelationExpr"]

M = TypeVar("M", bound="Model")
R = TypeVar("R")


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
    ``Document.journal == journal`` matches by the related object's key.
    """

    __slots__ = ("_relation",)

    def __init__(self, relation: Relation[Any]) -> None:
        self._relation = relation

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
            raise AttributeError(
                f"{model.__name__} has no field {name!r}; fields are: {', '.join(model._fields)}"
            )
        return field.expr_class(field, via=self._relation)

    def __dir__(self) -> Iterable[str]:
        return sorted({*object.__dir__(self), *self._relation.related_model._fields})

    def _key_handle(self) -> FieldExpr:
        field = self._relation.related_field
        return field.expr_class(field, via=self._relation)

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
        return f"<{self._relation.owner.__name__}.{self._relation.name}>"


class ModelMeta(type):
    """Metaclass collecting a model's fields and relations.

    Sets ``_fields`` and ``_relations`` (dicts in definition order, inherited
    entries first) and a per-model ``DoesNotExist`` exception class.
    """

    _fields: dict[str, Field[Any, Any]]
    _relations: dict[str, Relation[Any]]
    DoesNotExist: type[exceptions.DoesNotExist]

    def __init__(cls, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(name, bases, namespace, **kwargs)
        fields: dict[str, Field[Any, Any]] = {}
        relations: dict[str, Relation[Any]] = {}
        for base in reversed(bases):
            fields.update(getattr(base, "_fields", {}))
            relations.update(getattr(base, "_relations", {}))
        for attr, value in namespace.items():
            if isinstance(value, Field):
                fields[attr] = value
            elif isinstance(value, Relation):
                relations[attr] = value
        cls._fields = fields
        cls._relations = relations

        key = namespace.get("_key")
        if key is not None and key not in fields:
            raise TypeError(f"{name}._key names {key!r}, which is not a field of {name}")
        for relation in relations.values():
            if relation.key not in fields:
                raise TypeError(
                    f"{name}.{relation.name} uses key {relation.key!r}, which is not a field of {name}"
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
            if ref.via is not None or ref.position is not None:
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
