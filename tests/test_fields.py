"""Tests for field definitions, value conversion and field handles."""

from __future__ import annotations

import warnings
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from ads.exceptions import DataWarning, QueryError
from ads.expr import Expr, Func, In, Like, Match, Not, Order, Range, SolrLiteral, days_ago
from ads.fields import (
    BooleanField,
    DateField,
    DateTimeField,
    Field,
    FieldExpr,
    FieldInfo,
    FloatField,
    IntegerField,
    NumericExpr,
    StringField,
    TextExpr,
    TextField,
    VirtualField,
)

UTC = timezone.utc


class Rec:
    """A plain owner class: fields work as descriptors on any class."""

    bibcode = StringField("Bibcode")
    title = TextField("Title", multivalued=True)
    author = TextField("Authors", multivalued=True)
    year = IntegerField("Year of publication")
    norm = FloatField(solr_name="citation_count_norm")
    entry_date = DateTimeField()
    pubdate = DateField()
    public = BooleanField()
    full = VirtualField("Full text")

    def __init__(self, **data: Any):
        self.data = data

    def _get_value(self, field: Field) -> Any:
        return self.data.get(field.name)

    def _set_value(self, field: Field, value: Any) -> None:
        self.data[field.name] = value


F = Rec.__dict__  # the Field definitions themselves


def raises_set_name_error(fn, match: str) -> None:
    """Errors in __set_name__ are wrapped in RuntimeError before Python 3.12."""
    with pytest.raises((TypeError, RuntimeError)) as info:
        fn()
    error = info.value if isinstance(info.value, TypeError) else info.value.__cause__
    assert isinstance(error, TypeError)
    assert match in str(error)


# --- definitions ------------------------------------------------------------


def test_set_name_records_name_and_model():
    field = F["year"]
    assert field.name == "year"
    assert field.model is Rec
    assert field.solr_name == "year"
    assert F["norm"].solr_name == "citation_count_norm"
    assert field.__doc__ == "Year of publication"
    assert repr(field) == "<IntegerField Rec.year>"


def test_field_cannot_be_shared_between_classes():
    shared = StringField()

    class A:
        x = shared

    def define_b():
        class B:
            y = shared

    raises_set_name_error(define_b, "separate field instance")


def test_fields_use_identity_equality():
    assert StringField() != StringField()
    assert F["year"] == F["year"]
    assert len({F["year"], F["year"], F["norm"]}) == 2


def test_virtual_field_is_never_stored():
    assert F["full"].stored is False
    assert TextField().stored is True
    assert TextField(stored=False).stored is False


def test_class_access_returns_new_handles_of_the_right_type():
    assert type(Rec.year) is NumericExpr
    assert type(Rec.title) is TextExpr
    assert type(Rec.bibcode) is TextExpr
    assert type(Rec.full) is TextExpr
    assert type(Rec.entry_date) is NumericExpr
    assert type(Rec.pubdate) is NumericExpr
    assert type(Rec.public) is FieldExpr
    assert Rec.year is not Rec.year
    assert Rec.year.field is F["year"]
    assert Rec.year.__doc__ == "Year of publication"


def test_instance_access_and_assignment_delegate_to_instance():
    rec = Rec(year=2020)
    assert rec.year == 2020
    rec.year = 2021
    assert rec.data["year"] == 2021
    assert rec.title is None


def test_virtual_field_instance_access_raises():
    rec = Rec()
    with pytest.raises(AttributeError, match="search-only"):
        rec.full  # noqa: B018
    with pytest.raises(AttributeError, match="search-only"):
        rec.full = "x"


# --- to_python --------------------------------------------------------------


@pytest.mark.parametrize(
    "field, raw, expected",
    [
        (F["bibcode"], "2020ApJ...1..1C", "2020ApJ...1..1C"),
        (F["year"], "2015", 2015),
        (F["year"], 2015, 2015),
        (F["norm"], 1, 1.0),
        (F["public"], True, True),
        (F["public"], "false", False),
        (F["entry_date"], "2021-12-01T10:20:30Z", datetime(2021, 12, 1, 10, 20, 30, tzinfo=UTC)),
        (F["entry_date"], "2021-12-01T10:20:30.123Z", datetime(2021, 12, 1, 10, 20, 30, 123000, tzinfo=UTC)),
        (F["entry_date"], datetime(2021, 12, 1), datetime(2021, 12, 1, tzinfo=UTC)),
        (F["pubdate"], "2013-07-00", date(2013, 7, 1)),
        (F["pubdate"], "2013-00-00", date(2013, 1, 1)),
        (F["pubdate"], "2013-07-15", date(2013, 7, 15)),
        (F["pubdate"], "2013-07", date(2013, 7, 1)),
        (F["pubdate"], date(2013, 7, 2), date(2013, 7, 2)),
        (F["title"], ["A", "B"], ["A", "B"]),
        (F["year"], None, None),
        (F["title"], None, None),
    ],
)
def test_to_python(field, raw, expected):
    value = field.to_python(raw)
    assert value == expected
    assert type(value) is type(expected)


def test_to_python_is_element_wise_for_multivalued_fields():
    field = IntegerField(multivalued=True)
    assert field.to_python(["1", "2", 3.0]) == [1, 2, 3]


def test_to_python_turns_unconvertible_values_into_none_with_a_warning():
    """Irregular API data must never fail a whole result set, nor break the value type."""
    with pytest.warns(DataWarning, match=r"Rec\.entry_date: cannot convert 'yesterday' to datetime"):
        assert F["entry_date"].to_python("yesterday") is None
    with pytest.warns(DataWarning, match="to date"):
        assert F["pubdate"].to_python("July 2013") is None
    with pytest.warns(DataWarning, match=r"Rec\.year: cannot convert '508A' to int"):
        assert F["year"].to_python("508A") is None
    with pytest.warns(DataWarning, match=r"to list\[int\]"):
        assert IntegerField(multivalued=True).to_python(["1", "x", None]) is None


@pytest.mark.parametrize(
    "raw",
    [
        "2021-03-12T04:19:06.469872Z",
        "2021-03-12T04:19:06.469872",
        "2021-03-12T04:19:06Z",
        "2021-03-12T04:19:06",
    ],
)
def test_datetime_to_python_accepts_timestamps_with_or_without_z_and_fraction(raw):
    value = F["entry_date"].to_python(raw)
    assert (value.year, value.month, value.day, value.hour, value.second) == (2021, 3, 12, 4, 6)
    assert value.tzinfo is timezone.utc


@pytest.mark.parametrize("value", ["", "   "])
def test_string_fields_reject_empty_values(value):
    with pytest.raises(QueryError):
        Rec.title == value
    with pytest.raises(QueryError):
        Rec.bibcode.in_(["2020A", value])


def test_step_gives_the_adjacent_value_of_discrete_fields():
    assert F["year"].step(2020, 1) == 2021
    assert F["year"].step(2020, -1) == 2019
    assert F["pubdate"].step(date(2020, 12, 15), 1) == date(2021, 1, 1)
    assert F["pubdate"].step("2020-01", -1) == date(2019, 12, 1)
    assert F["norm"].step(0.5, 1) is None
    assert F["entry_date"].step(datetime(2020, 1, 1), 1) is None
    assert F["year"].step(SolrLiteral("X"), 1) is None
    assert F["pubdate"].step(SolrLiteral("NOW"), 1) is None


# --- to_solr ----------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value, expected",
    [
        (F["bibcode"], 'a "b" \\', 'a "b" \\'),
        (F["year"], 2015, "2015"),
        (F["year"], -5, "-5"),
        (F["norm"], 0.5, "0.5"),
        (F["norm"], 2, "2"),
        (F["public"], True, "true"),
        (F["public"], False, "false"),
        (F["entry_date"], datetime(2021, 12, 1), "2021-12-01T00:00:00Z"),
        (F["entry_date"], date(2021, 12, 1), "2021-12-01T00:00:00Z"),
        (
            F["entry_date"],
            datetime(2021, 12, 1, 10, 0, tzinfo=timezone(timedelta(hours=10))),
            "2021-12-01T00:00:00Z",
        ),
        (F["entry_date"], datetime(2021, 12, 1, 0, 0, 0, 999999, tzinfo=UTC), "2021-12-01T00:00:00Z"),
        (F["pubdate"], date(2020, 6, 30), "2020-06"),
        (F["pubdate"], datetime(2020, 6, 30, 23), "2020-06"),
        (F["pubdate"], "2020-06", "2020-06"),
        (F["year"], SolrLiteral("NOW"), "NOW"),
        (F["entry_date"], days_ago(3), "NOW-3DAYS"),
    ],
)
def test_to_solr(field, value, expected):
    assert field.to_solr(value) == expected


@pytest.mark.parametrize(
    "field, value",
    [
        (F["bibcode"], 1),
        (F["year"], "2015"),
        (F["year"], "abc"),
        (F["year"], True),
        (F["year"], 2015.0),
        (F["norm"], True),
        (F["norm"], "0.5"),
        (F["norm"], float("nan")),
        (F["norm"], float("inf")),
        (F["public"], 1),
        (F["public"], "true"),
        (F["entry_date"], "2021-12-01"),
        (F["pubdate"], "2020-6"),
        (F["pubdate"], "2020-13"),
        (F["pubdate"], "2020-06-01"),
        (F["pubdate"], 2020),
        (F["year"], None),
    ],
)
def test_to_solr_rejects_wrong_types(field, value):
    with pytest.raises(QueryError):
        field.to_solr(value)


# --- handles: equality, membership, ordering --------------------------------


def test_equality_builds_match():
    assert (Rec.year == 2020) == Match(F["year"], 2020)
    assert (Rec.title == "stars") == Match(F["title"], "stars")
    assert (Rec.public == True) == Match(F["public"], True)  # noqa: E712
    assert (Rec.year != 2020) == Not(Match(F["year"], 2020))


def test_nodes_from_handles_compare_structurally():
    assert ((Rec.year == 2020) == (Rec.year == 2020)) is True
    assert ((Rec.year == 2020) == (Rec.year == 2021)) is False
    assert ((Rec.year == 2020) == (Rec.norm == 2020)) is False
    assert ((Rec.title == "x") == (Rec.author == "x")) is False
    assert hash(Rec.year == 2020) == hash(Rec.year == 2020)
    assert len({Rec.year == 2020, Rec.year == 2020, Rec.norm == 2020}) == 2


def test_nodes_store_the_original_value():
    when = datetime(2021, 12, 1)
    assert (Rec.entry_date >= when).lo is when


def test_handles_hash_by_identity():
    handle = Rec.year
    assert hash(handle) == object.__hash__(handle)
    assert {handle: 1}[handle] == 1


@pytest.mark.parametrize(
    "build, error, match",
    [
        (lambda: Rec.year == "abc", QueryError, "needs an int"),
        (lambda: Rec.year == None, QueryError, "None"),  # noqa: E711
        (lambda: Rec.year != None, QueryError, "None"),  # noqa: E711
        (lambda: Rec.bibcode == 5, QueryError, "needs a str"),
        (lambda: Rec.year == Rec.year, TypeError, "only be compared with values"),
        (lambda: Rec.year == (Rec.year == 1), TypeError, "only be compared with values"),
        (lambda: Rec.title < 3, TypeError, "not supported"),
        (lambda: Rec.public > True, TypeError, "not supported"),
        (lambda: Rec.year.in_("2020"), TypeError, "single string"),
        (lambda: Rec.year.in_(2020), TypeError, "collection"),
        (lambda: Rec.year.in_([2020, "x"]), QueryError, "needs an int"),
        (lambda: Rec.year.in_([2020, None]), QueryError, "None"),
        (lambda: Rec.year.between(2020, None), QueryError, "None"),
        (lambda: Rec.year > 2.5, QueryError, "needs an int"),
        (lambda: Rec.title.like(3), TypeError, "str pattern"),
        (lambda: Rec.title.startswith(3), TypeError, "needs a str"),
        (lambda: Rec.title.contains(None), TypeError, "needs a str"),
    ],
)
def test_invalid_expressions_fail_when_built(build, error, match):
    with pytest.raises(error, match=match):
        build()


def test_handles_have_no_truth_value_and_are_not_iterable():
    with pytest.raises(TypeError, match=r"\.in_\(values\)"):
        bool(Rec.public)
    with pytest.raises(TypeError, match="not iterable"):
        list(Rec.author)
    with pytest.raises(TypeError, match="not iterable"):
        "x" in Rec.author  # noqa: B015
    with pytest.raises(TypeError):
        2015 < Rec.year < 2020  # noqa: B015


def test_reflected_comparisons():
    assert (2015 < Rec.year) == (Rec.year > 2015)
    assert (2015 >= Rec.year) == (Rec.year <= 2015)


def test_in_and_not_in():
    assert Rec.year.in_([2019, 2021]) == In(F["year"], (2019, 2021))
    assert Rec.year.in_(range(2019, 2021)) == In(F["year"], (2019, 2020))
    assert Rec.year.in_(v for v in (1, 2)) == In(F["year"], (1, 2))
    assert Rec.year.in_([]) == In(F["year"], ())
    assert Rec.bibcode.not_in(["A"]) == Not(In(F["bibcode"], ("A",)))


def test_ranges():
    year = F["year"]
    assert (Rec.year < 2020) == Range(year, None, 2020, include_hi=False)
    assert (Rec.year <= 2020) == Range(year, None, 2020)
    assert (Rec.year > 2020) == Range(year, 2020, None, include_lo=False)
    assert (Rec.year >= 2020) == Range(year, 2020, None)
    assert Rec.year.between(2018, 2019) == Range(year, 2018, 2019)
    assert (Rec.entry_date >= days_ago(7)) == Range(F["entry_date"], days_ago(7), None)


def test_order():
    assert Rec.year.asc() == Order(F["year"])
    assert Rec.year.desc() == Order(F["year"], True)
    with pytest.raises(QueryError, match="Cannot sort"):
        Rec.author[0].desc()


def test_repr():
    assert repr(Rec.year) == "<Rec.year>"
    assert repr(Rec.author[0]) == "<Rec.author[0]>"
    assert repr(Rec.author[1:3]) == "<Rec.author[1:3]>"


# --- text handles -----------------------------------------------------------


def test_exact():
    assert Rec.author.exact("Ness, M") == Match(F["author"], "Ness, M", exact=True)


@pytest.mark.parametrize(
    "build, pattern",
    [
        (lambda: Rec.title.like("JWST*"), "JWST*"),
        (lambda: Rec.title.like("JWST%"), "JWST*"),
        (lambda: Rec.title.like("50\\%"), "50%"),
        (lambda: Rec.title.like("a?c\\*"), "a?c\\*"),
        (lambda: Rec.title.startswith("what?*"), "what\\?\\**"),
        (lambda: Rec.title.startswith("50%"), "50%*"),
        (lambda: Rec.title.contains("astro"), "*astro*"),
        (lambda: Rec.title.contains("a\\b"), "*a\\\\b*"),
    ],
)
def test_wildcards(build, pattern):
    assert build() == Like(F["title"], pattern)


def test_like_rejects_bad_patterns():
    with pytest.raises(QueryError, match="unpaired backslash"):
        Rec.title.like("abc\\")


def test_position_indexing():
    author = F["author"]
    match = Match(author, "Casey, A")
    assert (Rec.author[0] == "Casey, A") == Func("pos", (match, 1, 1))
    assert (Rec.author[1:3] == "Casey, A") == Func("pos", (match, 2, 3))
    assert (Rec.author[:2] == "Casey, A") == Func("pos", (match, 1, 2))
    assert (Rec.author[0:3:1] == "Casey, A") == Func("pos", (match, 1, 3))
    assert Rec.author[2].position == (3, 3)


def test_position_wraps_every_builder():
    pos = lambda node: Func("pos", (node, 1, 1))  # noqa: E731
    author = F["author"]
    first = Rec.author[0]
    assert first.like("Casey*") == pos(Like(author, "Casey*"))
    assert first.startswith("Casey") == pos(Like(author, "Casey*"))
    assert first.contains("asey") == pos(Like(author, "*asey*"))
    assert first.exact("Casey, A") == pos(Match(author, "Casey, A", exact=True))
    assert first.in_(["A", "B"]) == pos(In(author, ("A", "B")))
    assert first.not_in(["A"]) == Not(pos(In(author, ("A",))))
    assert (first != "A") == Not(pos(Match(author, "A")))


@pytest.mark.parametrize(
    "index, error, match",
    [
        (-1, QueryError, "from the end"),
        (slice(-2, None), QueryError, "from the end"),
        (slice(0, -1), QueryError, "from the end"),
        (slice(1, None), QueryError, "from the end"),
        (slice(2, 2), QueryError, "Empty"),
        (slice(3, 1), QueryError, "Empty"),
        (slice(0, 4, 2), QueryError, "step"),
        ("0", TypeError, "ints or slices"),
        (True, TypeError, "ints or slices"),
        (slice("a", 2), TypeError, "must be ints"),
    ],
)
def test_bad_positions(index, error, match):
    with pytest.raises(error, match=match):
        Rec.author[index]


def test_only_multivalued_fields_can_be_indexed():
    with pytest.raises(TypeError, match="not multivalued"):
        Rec.bibcode[0]
    with pytest.raises(TypeError, match="already restricted"):
        Rec.author[0][0]
    with pytest.raises(TypeError):
        Rec.year[0]


def test_expressions_from_handles_are_exprs():
    assert isinstance(Rec.year == 1, Expr)
    assert isinstance(Rec.title.like("x"), Expr)


# --- to_python: the value is always T | None ---------------------------------

SCALAR_FIELDS = {
    "str": StringField(),
    "text": TextField(),
    "int": IntegerField(),
    "float": FloatField(),
    "bool": BooleanField(),
    "datetime": DateTimeField(),
    "date": DateField(),
}


def test_python_type_of_each_field_class():
    assert StringField.python_type is str
    assert TextField.python_type is str
    assert VirtualField.python_type is str
    assert IntegerField.python_type is int
    assert FloatField.python_type is float
    assert BooleanField.python_type is bool
    assert DateTimeField.python_type is datetime
    assert DateField.python_type is date


def test_type_name_and_info():
    assert F["year"].type_name == "int"
    assert F["title"].type_name == "list[str]"
    assert F["entry_date"].type_name == "datetime"
    assert F["full"].info() == FieldInfo(
        name="full", type="str", multivalued=False, stored=False, solr_name="full", doc="Full text"
    )
    assert F["norm"].info().solr_name == "citation_count_norm"


@pytest.mark.parametrize(
    "field, raw, expected",
    [
        (IntegerField(), "12", 12),
        (IntegerField(), " 12 ", 12),
        (IntegerField(), "12.0", 12),
        (IntegerField(), 12.0, 12),
        (IntegerField(), "-3", -3),
        (FloatField(), 2, 2.0),
        (FloatField(), "0.5", 0.5),
        (FloatField(), "3", 3.0),
        (StringField(), 2020, "2020"),
        (TextField(), 0.5, "0.5"),
        (BooleanField(), "TRUE", True),
        (BooleanField(), "False", False),
        (BooleanField(), False, False),
        (DateTimeField(), "2021-12-01", datetime(2021, 12, 1, tzinfo=UTC)),
        (DateTimeField(), date(2021, 12, 1), datetime(2021, 12, 1, tzinfo=UTC)),
        (DateField(), datetime(2021, 12, 1, 5), date(2021, 12, 1)),
        (StringField(multivalued=True), "A", ["A"]),
        (IntegerField(multivalued=True), "7", [7]),
        (IntegerField(multivalued=True), ("1", 2), [1, 2]),
        (StringField(multivalued=True), [], []),
        (StringField(), ["only"], "only"),
        (IntegerField(), ("5",), 5),
        (IntegerField(), [None], None),
    ],
)
def test_safe_coercions_are_silent(field, raw, expected):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        value = field.to_python(raw)
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize(
    "field, raw",
    [
        (IntegerField(), "12.5"),
        (IntegerField(), 12.5),
        (IntegerField(), True),
        (IntegerField(), "abc"),
        (IntegerField(), "NaN"),
        (IntegerField(), float("inf")),
        (IntegerField(), {}),
        (FloatField(), True),
        (FloatField(), "fast"),
        (FloatField(), {"a": 1}),
        (FloatField(), 10**400),
        (StringField(), True),
        (StringField(), {"a": 1}),
        (TextField(), b"bytes"),
        (BooleanField(), 1),
        (BooleanField(), "yes"),
        (BooleanField(), "1"),
        (DateTimeField(), "yesterday"),
        (DateTimeField(), 1638316800),
        (DateField(), "July 2013"),
        (DateField(), "2013-13-00"),
        (DateField(), 2013),
        (StringField(), ["a", "b"]),
        (StringField(), []),
        (IntegerField(), [1, 2]),
        (StringField(), [["a"]]),
        (StringField(multivalued=True), ["a", None, "b"]),
        (StringField(multivalued=True), [None]),
        (IntegerField(multivalued=True), ["1", "x", "3"]),
        (StringField(multivalued=True), ["a", ["b"]]),
        (StringField(multivalued=True), {"a": "b"}),
        (BooleanField(multivalued=True), [True, "maybe"]),
    ],
)
def test_unconvertible_values_become_none_with_a_data_warning(field, raw):
    with pytest.warns(DataWarning, match=f"to {field.type_name.replace('[', '.').replace(']', '.')}; using None"):
        assert field.to_python(raw) is None


def test_data_warning_names_model_field_value_and_type():
    with pytest.warns(DataWarning) as record:
        F["year"].to_python("508A")
    (warning,) = record
    assert str(warning.message) == "Rec.year: cannot convert '508A' to int; using None"


def test_data_warning_truncates_long_values():
    raw = ["x" * 50, "y" * 50, None]
    with pytest.warns(DataWarning) as record:
        F["title"].to_python(raw)
    message = str(record[0].message)
    shown = message.split("cannot convert ")[1].split(" to list[str]")[0]
    assert len(shown) == 80
    assert shown.endswith("...")
    assert shown.startswith("['xxxx")


JSON_SHAPES = [
    None,
    True,
    False,
    0,
    -7,
    12.0,
    12.5,
    float("nan"),
    "",
    "12",
    "true",
    "2021-12-01T00:00:00Z",
    "2013-07-00",
    "garbage",
    [],
    [None],
    ["12"],
    [1, 2],
    ["a", None],
    [[1]],
    {},
    {"a": 1},
]


@pytest.mark.parametrize("name", SCALAR_FIELDS)
@pytest.mark.parametrize("multivalued", [False, True])
def test_any_json_value_converts_to_the_declared_type_or_none(name, multivalued):
    field = type(SCALAR_FIELDS[name])(multivalued=multivalued)
    for raw in JSON_SHAPES:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DataWarning)
            value = field.to_python(raw)
        if value is None:
            continue
        if multivalued:
            assert isinstance(value, list), raw
            assert all(type(item) is field.python_type for item in value), raw
        else:
            assert type(value) is field.python_type, raw
