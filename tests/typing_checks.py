"""Static type expectations for fields, handles, models and relations.

This module is never run, only type-checked (see ``tests/test_typing.py``).
Each ``assert_type`` states a type the checker must infer. Each line marked
``# type: ignore[code]`` must NOT type-check: with ``warn_unused_ignores`` on,
a missing error fails the check.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, NoReturn

from ads.expr import Expr, Order
from ads.fields import (
    BooleanField,
    DateField,
    DateTimeField,
    Field,
    FieldExpr,
    FloatField,
    IntegerField,
    NumericExpr,
    StringField,
    TextExpr,
    TextField,
    VirtualField,
)
from ads.model import Model, Relation, RelationExpr

if TYPE_CHECKING:
    if sys.version_info >= (3, 11):
        from typing import assert_type
    else:
        from typing_extensions import assert_type


class Journal(Model):
    _key = "abbreviation"
    abbreviation = StringField("Abbreviation")
    title = StringField("Title")


def resolve_journal(paper: Paper) -> Journal | None:
    return None


def resolve_journals(paper: Paper) -> list[Journal]:
    return []


class Paper(Model):
    _key = "bibcode"
    bibcode = StringField("Bibcode")
    title = TextField("Title", multivalued=True)
    year = IntegerField("Year")
    years = IntegerField(multivalued=True)
    norm = FloatField(solr_name="citation_count_norm")
    entry_date = DateTimeField()
    pubdate = DateField()
    public = BooleanField()
    flags = BooleanField(multivalued=True)
    full = VirtualField()
    journal = Relation(Journal, key="bibcode", related_key="abbreviation", resolve=resolve_journal)
    journals = Relation(lambda: Journal, key="bibcode", related_key="abbreviation", resolve=resolve_journals)


def instance_access(paper: Paper) -> None:
    assert_type(paper.bibcode, str | None)
    assert_type(paper.title, list[str] | None)
    assert_type(paper.year, int | None)
    assert_type(paper.years, list[int] | None)
    assert_type(paper.norm, float | None)
    assert_type(paper.entry_date, datetime | None)
    assert_type(paper.pubdate, date | None)
    assert_type(paper.public, bool | None)
    assert_type(paper.flags, list[bool] | None)
    assert_type(paper.journal, Journal | None)
    assert_type(paper.journals, list[Journal])


def class_access() -> None:
    assert_type(Paper.bibcode, TextExpr)
    assert_type(Paper.title, TextExpr)
    assert_type(Paper.full, TextExpr)
    assert_type(Paper.year, NumericExpr)
    assert_type(Paper.norm, NumericExpr)
    assert_type(Paper.entry_date, NumericExpr)
    assert_type(Paper.pubdate, NumericExpr)
    assert_type(Paper.public, FieldExpr)
    assert_type(Paper.journal, RelationExpr)
    assert_type(Paper.journal.title, FieldExpr)
    assert_type(Paper.title[0], TextExpr)


def expressions() -> None:
    assert_type(Paper.year == 2020, Expr)
    assert_type(Paper.year != 2020, Expr)
    assert_type(Paper.year > 2020, Expr)
    assert_type(Paper.year.between(1, 2), Expr)
    assert_type(Paper.year.in_([2019, 2020]), Expr)
    assert_type(Paper.title.like("JWST*"), Expr)
    assert_type(Paper.title.exact("x"), Expr)
    assert_type(Paper.journal == "ApJ", Expr)
    assert_type((Paper.year == 2020) & ~(Paper.public == True), Expr)  # noqa: E712
    assert_type(Paper.year.desc(), Order)
    assert_type(Paper.bibcode.asc(), Order)


def definitions_and_models(row: dict[str, Any]) -> None:
    assert_type(IntegerField(), IntegerField[int])
    assert_type(IntegerField("doc", multivalued=False), IntegerField[int])
    assert_type(IntegerField(multivalued=True), IntegerField[list[int]])
    assert_type(TextField(multivalued=True, solr_name="t"), TextField[list[str]])
    assert_type(DateField(stored=False), DateField[date])
    assert_type(StringField().to_python("x"), str | None)
    assert_type(DateTimeField(multivalued=True).to_python([]), list[datetime] | None)
    relation = Relation(Journal, key="k", related_key="abbreviation", resolve=resolve_journal)
    assert_type(relation, Relation[Journal | None])
    assert_type(Paper._from_row(row), Paper)
    assert_type(Paper._fields, dict[str, Field[Any, Any]])
    assert_type(relation.related_field, Field[Any, Any])


def must_not_type_check(paper: Paper) -> None:
    Paper.title.between(1, 2)  # type: ignore[attr-defined]
    Paper.title < 3  # type: ignore[operator]  # noqa: B015
    Paper.public > True  # type: ignore[operator]  # noqa: B015
    Paper.year.like("19*")  # type: ignore[attr-defined]
    Paper.year.exact(2020)  # type: ignore[attr-defined]
    Paper.year[0]  # type: ignore[index]  # noqa: B018
    paper.year = "2020"  # type: ignore[assignment]
    paper.title = "x"  # type: ignore[assignment]
    paper.bibcode.upper()  # type: ignore[union-attr]
    _year: int = paper.year  # type: ignore[assignment]
    TextField(multivalued=True, stord=False)  # type: ignore[call-overload]
    IntegerField(multivalued=1)  # type: ignore[call-overload]


def search_only_field(paper: Paper) -> None:
    # Last: code after a NoReturn expression is unreachable and would go unchecked.
    assert_type(paper.full, NoReturn)
