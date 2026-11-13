"""Tests for models, relations and relation handles.

``Model.select`` and ``Model.get`` need ``ads.query`` and are tested with it.
"""

from __future__ import annotations

import warnings
from datetime import date, datetime, timezone

import pytest
from helpers import INSTITUTES, JOURNALS, Institute, Journal, Paper

from ads import exceptions
from ads.exceptions import DataWarning, QueryError
from ads.expr import And, Func, In, Like, Match, Not, Related
from ads.fields import Field, FieldInfo, IntegerField, StringField, TextField
from ads.model import Model, Relation, RelationExpr


def raises_set_name_error(fn, match: str) -> None:
    """Errors in __set_name__ are wrapped in RuntimeError before Python 3.12."""
    with pytest.raises((TypeError, RuntimeError)) as info:
        fn()
    error = info.value if isinstance(info.value, TypeError) else info.value.__cause__
    assert isinstance(error, TypeError)
    assert match in str(error)


# --- metaclass --------------------------------------------------------------


def test_fields_and_relations_collected_in_definition_order():
    assert list(Journal._fields) == ["abbreviation", "title"]
    assert list(Paper._fields)[:4] == ["bibcode", "title", "author", "year"]
    assert all(isinstance(f, Field) for f in Paper._fields.values())
    assert Paper._fields["year"] is Paper.__dict__["year"]
    assert list(Paper._relations) == ["journal", "institute"]
    assert Model._fields == {} and Model._relations == {}


def test_inherited_fields_come_first():
    class Base(Model):
        a = StringField()

    class Child(Base):
        b = StringField()

    assert list(Child._fields) == ["a", "b"]
    assert Child._fields["a"] is Base._fields["a"]
    assert list(Base._fields) == ["a"]


def test_each_model_has_its_own_does_not_exist():
    assert issubclass(Journal.DoesNotExist, exceptions.DoesNotExist)
    assert issubclass(Journal.DoesNotExist, LookupError)
    assert Journal.DoesNotExist is not Paper.DoesNotExist
    assert not issubclass(Journal.DoesNotExist, Paper.DoesNotExist)
    assert Journal.DoesNotExist.__qualname__ == "Journal.DoesNotExist"

    class Sub(Journal):
        pass

    assert issubclass(Sub.DoesNotExist, Journal.DoesNotExist)


def test_key_must_name_a_field():
    with pytest.raises(TypeError, match="_key names 'nope'"):

        class Bad(Model):
            _key = "nope"
            a = StringField()


def test_relation_key_must_name_an_owner_field():
    with pytest.raises(TypeError, match="uses key 'nope'"):

        class Bad(Model):
            a = StringField()
            j = Relation(Journal, key="nope", related_key="abbreviation", resolve=lambda o: None)


def test_relation_cannot_be_shared_between_classes():
    shared = Relation(Journal, key="a", related_key="abbreviation", resolve=lambda o: None)

    class A(Model):
        a = StringField()
        j = shared

    def define_b():
        class B(Model):
            a = StringField()
            j = shared

    raises_set_name_error(define_b, "separate relation")


# --- instances --------------------------------------------------------------


def test_init_stores_values():
    paper = Paper(bibcode="X", year=2020)
    assert paper._data == {"bibcode": "X", "year": 2020}
    assert paper.year == 2020
    assert paper.title is None


def test_init_rejects_unknown_and_search_only_fields():
    with pytest.raises(TypeError, match="unexpected field 'nope'.*bibcode"):
        Paper(nope=1)
    with pytest.raises(TypeError, match="search-only"):
        Paper(full="x")
    with pytest.raises(TypeError, match="unexpected field 'journal'"):
        Paper(journal=JOURNALS["ApJ"])


def test_assignment_goes_through_set_value():
    paper = Paper()
    paper.year = 1999
    assert paper._data["year"] == 1999


def test_from_row_converts_by_solr_name_and_sets_only_present_keys():
    paper = Paper._from_row(
        {
            "bibcode": "2020ApJ...1..1C",
            "year": "2020",
            "entry_date": "2021-12-01T00:00:00Z",
            "pubdate": "2020-07-00",
            "citation_count": 5,
            "author": ["Casey, A", "Ness, M"],
            "unknown": "ignored",
            "full": "never stored",
        }
    )
    assert paper._data == {
        "bibcode": "2020ApJ...1..1C",
        "year": 2020,
        "entry_date": datetime(2021, 12, 1, tzinfo=timezone.utc),
        "pubdate": date(2020, 7, 1),
        "citation_count": 5,
        "author": ["Casey, A", "Ness, M"],
    }
    assert isinstance(paper, Paper)


def test_from_row_uses_solr_name():
    class Renamed(Model):
        norm = IntegerField(solr_name="citation_count_norm")

    assert Renamed._from_row({"citation_count_norm": "3", "norm": "ignored"})._data == {"norm": 3}


def test_missing_values_go_through_load_missing():
    loaded = []

    class Lazy(Model):
        a = StringField()
        b = StringField()

        def _load_missing(self, field):
            loaded.append(field.name)
            return f"loaded {field.name}"

    obj = Lazy(a="x")
    assert obj.a == "x"
    assert obj.b == "loaded b"
    assert loaded == ["b"]


def test_equality_and_hash_by_key():
    assert Journal(abbreviation="ApJ") == Journal(abbreviation="ApJ", title="other")
    assert hash(Journal(abbreviation="ApJ")) == hash(Journal(abbreviation="ApJ"))
    assert Journal(abbreviation="ApJ") != Journal(abbreviation="MNRAS")
    assert len({Journal(abbreviation="ApJ"), Journal(abbreviation="ApJ"), Journal(abbreviation="AJ")}) == 2
    assert Journal(abbreviation="ApJ") != "ApJ"
    assert Institute(id="ApJ") != Journal(abbreviation="ApJ")


def test_equality_falls_back_to_identity_without_key():
    a, b = Journal(title="T"), Journal(title="T")
    assert a == a
    assert a != b
    assert hash(a) != hash(b)

    class Keyless(Model):
        x = StringField()

    k = Keyless(x="1")
    assert k == k and k != Keyless(x="1")


def test_repr():
    assert repr(Journal(abbreviation="ApJ")) == "<Journal: abbreviation='ApJ'>"
    assert repr(Journal()) == "<Journal>"


def test_field_resolution():
    year = Paper._fields["year"]
    assert Paper._field("year") is year
    assert Paper._field(year) is year
    assert Paper._field(Paper.year) is year
    with pytest.raises(AttributeError, match="no field 'nope'"):
        Paper._field("nope")
    with pytest.raises(TypeError, match="not a field of Paper"):
        Paper._field(Journal.title)
    with pytest.raises(TypeError, match="not a plain field"):
        Paper._field(Paper.author[0])
    with pytest.raises(TypeError, match="not a plain field"):
        Paper._field(Paper.journal.title)
    with pytest.raises(TypeError):
        Paper._field(3)


# --- relations: definitions and instance access ------------------------------


def test_relation_attributes():
    relation = Paper.__dict__["journal"]
    assert relation.name == "journal"
    assert relation.owner is Paper
    assert relation.related_model is Journal
    assert relation.related_field is Journal._fields["abbreviation"]
    assert repr(relation) == "<Relation Paper.journal>"


def test_related_model_can_be_given_lazily():
    relation = Paper.__dict__["institute"]
    assert relation.related_model is Institute
    assert relation.related_field is Institute._fields["id"]


def test_bad_related_key_is_reported_on_use():
    class Owner(Model):
        k = StringField()
        j = Relation(Journal, key="k", related_key="nope", resolve=lambda o: None)

    with pytest.raises(TypeError, match="related_key 'nope'"):
        Owner.j == "x"  # noqa: B015


def test_instance_access_resolves_once_and_caches():
    calls = []

    def resolve(owner):
        calls.append(owner)
        return JOURNALS.get(owner.k)

    class Owner(Model):
        k = StringField()
        j = Relation(Journal, key="k", related_key="abbreviation", resolve=resolve)

    owner = Owner(k="ApJ")
    assert owner.j is JOURNALS["ApJ"]
    assert owner.j is JOURNALS["ApJ"]
    assert calls == [owner]

    owner.k = "MNRAS"
    assert owner.j is JOURNALS["MNRAS"]
    assert len(calls) == 2


def test_resolved_none_is_cached_too():
    calls = []

    class Owner(Model):
        k = StringField()
        j = Relation(Journal, key="k", related_key="abbreviation", resolve=lambda o: calls.append(o))

    owner = Owner()
    assert owner.j is None
    assert owner.j is None
    assert len(calls) == 1


def test_relation_cannot_be_assigned():
    with pytest.raises(AttributeError, match="cannot be set"):
        Paper(bibcode="X").journal = JOURNALS["ApJ"]


def test_helper_resolvers():
    assert Paper(bibcode="2020ApJ...900..1C").journal is JOURNALS["ApJ"]
    assert Paper(bibcode="2020Natur.900..1C").journal is None
    paper = Paper(aff_id=["A1", "-", "A1; A2", "A9"])
    assert paper.institute == [INSTITUTES["A1"], None, (INSTITUTES["A1"], INSTITUTES["A2"]), None]


# --- relation handles -------------------------------------------------------


journal_rel = Paper.__dict__["journal"]
abbreviation = Journal._fields["abbreviation"]


def test_class_access_returns_relation_expr():
    handle = Paper.journal
    assert isinstance(handle, RelationExpr)
    assert handle.relation is journal_rel
    assert repr(handle) == "<Paper.journal>"


def test_related_field_handle():
    handle = Paper.journal.title
    assert handle.field is Journal._fields["title"]
    assert handle.via is journal_rel
    assert repr(handle) == "<Paper.journal.title>"
    assert (Paper.journal.title == "x") == Related(journal_rel, Match(Journal._fields["title"], "x"))
    assert Paper.journal.title.contains("astro") == Related(
        journal_rel, Like(Journal._fields["title"], "*astro*")
    )


def test_related_field_negation_wraps_outside_the_relation():
    assert (Paper.journal.title != "x") == Not(Related(journal_rel, Match(Journal._fields["title"], "x")))


def test_equality_by_object_or_key():
    expected = Related(journal_rel, Match(abbreviation, "ApJ"))
    assert (Paper.journal == Journal(abbreviation="ApJ")) == expected
    assert (Paper.journal == "ApJ") == expected
    assert (Paper.journal != "ApJ") == Not(expected)


def test_in_and_not_in():
    expected = Related(journal_rel, In(abbreviation, ("ApJ", "MNRAS")))
    assert Paper.journal.in_([JOURNALS["ApJ"], "MNRAS"]) == expected
    assert Paper.journal.not_in(["ApJ", "MNRAS"]) == Not(expected)


@pytest.mark.parametrize(
    "build, error, match",
    [
        (lambda: Paper.journal.nope, AttributeError, "Journal has no field 'nope'; fields are: abbreviation, title"),
        (lambda: Paper.journal == None, QueryError, "None"),  # noqa: E711
        (lambda: Paper.journal == 3, QueryError, "needs a str"),
        (lambda: Paper.journal == Institute(id="A1"), TypeError, "relates to Journal, not Institute"),
        (lambda: Paper.journal == Journal(title="no key"), QueryError, "no 'abbreviation'"),
        (lambda: Paper.journal == Paper.journal, TypeError, "can only be compared"),
        (lambda: Paper.journal == Paper.bibstem, TypeError, "can only be compared"),
        (lambda: Paper.journal.in_("ApJ"), TypeError, "collection"),
        (lambda: Paper.journal.in_(None), TypeError, "collection"),
        (lambda: bool(Paper.journal), TypeError, "truth value"),
        (lambda: list(Paper.journal), TypeError, "not iterable"),
        (lambda: Paper.journal._private, AttributeError, "_private"),
    ],
)
def test_invalid_relation_expressions(build, error, match):
    with pytest.raises(error, match=match):
        build()


def test_relation_handle_hash_and_dir():
    handle = Paper.journal
    assert {handle: 1}[handle] == 1
    names = dir(handle)
    assert {"abbreviation", "title", "in_", "not_in"} <= set(names)


# --- positions through relations ---------------------------------------------


class Group(Model):
    _key = "name"
    name = StringField()
    members = TextField(multivalued=True)


class Team(Model):
    group_name = StringField()
    group = Relation(Group, key="group_name", related_key="name", resolve=lambda t: None)


team_rel = Team.__dict__["group"]
members = Group._fields["members"]


def test_position_then_relation_wrapping():
    def pos_via(node, start=1, end=1):
        return Related(team_rel, Func("pos", (node, start, end)))

    assert (Team.group.members[0] == "Casey, A") == pos_via(Match(members, "Casey, A"))
    assert (Team.group.members[1:3] == "Casey, A") == pos_via(Match(members, "Casey, A"), 2, 3)
    assert Team.group.members[0].like("Casey*") == pos_via(Like(members, "Casey*"))
    assert Team.group.members[0].in_(["A", "B"]) == pos_via(In(members, ("A", "B")))
    assert Team.group.members[0].exact("A") == pos_via(Match(members, "A", exact=True))
    assert (Team.group.members[0] != "A") == Not(pos_via(Match(members, "A")))
    assert repr(Team.group.members[0]) == "<Team.group.members[0]>"
    assert Team.group.members[0].via is team_rel


def test_relation_expressions_combine():
    expr = (Paper.year == 2020) & (Paper.journal == "ApJ")
    assert expr == And((Match(Paper._fields["year"], 2020), Related(journal_rel, Match(abbreviation, "ApJ"))))


# --- values from the API have their declared type ---------------------------


WRONG_SHAPES = {
    "bibcode": ["2020A", "2020B"],
    "title": {"not": "a list"},
    "author": ["Casey, A", None],
    "year": "508A",
    "citation_count_norm": [0.5, 0.6],
    "entry_date": 1638316800,
    "pubdate": "July 2013",
    "public": "yes",
    "bibstem": [["ApJ"]],
    "aff_id": [True],
    "citation_count": 2.5,
    "full": "never stored",
}


def test_from_row_never_raises_and_values_have_their_declared_type():
    with pytest.warns(DataWarning) as record:
        paper = Paper._from_row(WRONG_SHAPES)
    assert len(record) == 11
    for name, field in Paper._fields.items():
        value = paper._data.get(name)
        if value is None:
            continue
        if field.multivalued:
            assert all(isinstance(item, field.python_type) for item in value), name
        else:
            assert isinstance(value, field.python_type), name
    assert all(paper._data[name] is None for name in WRONG_SHAPES if name != "full")


def test_from_row_coerces_shapes_silently():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        paper = Paper._from_row({"bibcode": ["2020A"], "author": "Casey, A", "year": 2020.0, "public": "TRUE"})
    assert paper._data == {"bibcode": "2020A", "author": ["Casey, A"], "year": 2020, "public": True}


# --- to_dict, describe -------------------------------------------------------


def test_to_dict_returns_loaded_values_in_field_order():
    loaded = []

    class Lazy(Model):
        a = StringField()
        b = StringField()
        c = IntegerField()

        def _load_missing(self, field):
            loaded.append(field.name)
            return "loaded"

    obj = Lazy(c=3, a="x")
    assert obj.to_dict() == {"a": "x", "c": 3}
    assert list(obj.to_dict()) == ["a", "c"]
    assert loaded == []
    assert Lazy().to_dict() == {}


def test_to_dict_excludes_relations_and_is_a_copy():
    paper = Paper(bibcode="2020ApJ...900..1C", year=2020)
    assert paper.journal is JOURNALS["ApJ"]
    data = paper.to_dict()
    assert data == {"bibcode": "2020ApJ...900..1C", "year": 2020}
    data["year"] = 1
    assert paper.year == 2020


def test_describe_lists_fields_in_definition_order():
    info = Paper.describe()
    assert [i.name for i in info] == list(Paper._fields)
    by_name = {i.name: i for i in info}
    assert by_name["bibcode"] == FieldInfo(
        name="bibcode",
        type="str",
        multivalued=False,
        stored=True,
        solr_name="bibcode",
        doc="Bibcode",
        match_kind="keyword",
    )
    assert by_name["title"].type == "list[str]"
    assert by_name["title"].multivalued is True
    assert by_name["year"].type == "int"
    assert by_name["citation_count_norm"].type == "float"
    assert by_name["entry_date"].type == "datetime"
    assert by_name["pubdate"].type == "date"
    assert by_name["public"].type == "bool"
    assert by_name["full"].stored is False
    assert by_name["author"].match_kind == "name"
    assert by_name["title"].match_kind == "text"
    assert by_name["full"].match_kind == "text"
    assert by_name["year"].match_kind == "keyword"
    assert by_name["doctype"].stored is False
    assert "journal" not in by_name
    assert Journal.describe()[1] == FieldInfo(
        name="title",
        type="str",
        multivalued=False,
        stored=True,
        solr_name="title",
        doc="Title",
        match_kind="keyword",
    )


def test_field_info_is_frozen():
    with pytest.raises(AttributeError):
        Paper.describe()[0].name = "x"  # type: ignore[misc]
