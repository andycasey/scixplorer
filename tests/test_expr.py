"""Tests for expression nodes, pattern helpers, walk, transform and simplify."""

from __future__ import annotations

import pytest

from ads.exceptions import QueryError
from ads.expr import (
    EVERYTHING,
    NOTHING,
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
    days_ago,
    escape_pattern,
    format_pattern,
    now,
    parse_pattern,
    simplify,
    transform,
    walk,
)


class FakeField:
    """Stands in for a Field: identity equality, like the real thing."""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


X, Y = FakeField("x"), FakeField("y")
a, b, c, d = (Match(X, v) for v in "abcd")


# --- operators -------------------------------------------------------------


def test_and_or_build_flat_groups():
    assert a & b == And((a, b))
    assert (a & b) & (c & d) == And((a, b, c, d))
    assert a | (b | c) == Or((a, b, c))
    assert (a & b) | c == Or((And((a, b)), c))


def test_invert():
    assert ~a == Not(a)
    assert ~~a is a
    assert ~EVERYTHING is NOTHING
    assert ~NOTHING is EVERYTHING
    assert ~Constant(True) is NOTHING


@pytest.mark.parametrize("other", [True, 1, "x", None])
def test_and_or_with_non_expr_is_type_error(other):
    with pytest.raises(TypeError):
        a & other
    with pytest.raises(TypeError):
        other & a
    with pytest.raises(TypeError):
        a | other


def test_bool_raises_helpful_type_error():
    with pytest.raises(TypeError, match=r"& \(and\).*\.in_\(values\)"):
        bool(a)
    with pytest.raises(TypeError):
        a and b  # noqa: B018
    with pytest.raises(TypeError):
        not a  # noqa: B018
    with pytest.raises(TypeError):
        if a:
            pass


# --- equality and hashing ---------------------------------------------------


def test_structural_equality_and_hash():
    assert Match(X, 1) == Match(X, 1)
    assert hash(Match(X, 1)) == hash(Match(X, 1))
    assert Match(X, 1) != Match(Y, 1)
    assert Match(X, 1) != Match(X, 1, exact=True)
    assert (Match(X, 1) == Match(Y, 1)) is False
    assert (Match(X, 1) == Like(X, "1")) is False
    assert (Match(X, 1) == "x") is False
    assert And((a, b)) == And((a, b))
    assert And((a, b)) != Or((a, b))


def test_nodes_work_in_containers_despite_bool_raising():
    nodes = (a, b, And((a, b)), Not(c), EVERYTHING)
    assert And((a, b)) in nodes
    assert c not in nodes
    assert nodes.index(Not(Match(X, "c"))) == 3
    assert len({a, Match(X, "a"), b, And((a, b)), And((a, b))}) == 3
    assert {And((a, b)): 1}[And((Match(X, "a"), b))] == 1
    assert [a, b].count(Match(X, "b")) == 1


def test_nodes_are_immutable():
    with pytest.raises(AttributeError):
        a.value = "z"  # type: ignore[misc]


def test_constructors_normalise_and_validate():
    assert And([a, b]).operands == (a, b)
    assert In(X, [1, 2]).values == (1, 2)
    assert Func("f", [a, 1]).args == (a, 1)
    with pytest.raises(TypeError):
        And((a, "b"))
    with pytest.raises(TypeError):
        Not("a")
    with pytest.raises(TypeError):
        In(X, "abc")
    with pytest.raises(TypeError):
        Func("f", (True,))
    with pytest.raises(TypeError):
        Func("f", (1.5,))
    with pytest.raises(TypeError):
        Related(object(), "x")
    with pytest.raises(QueryError, match="at least one bound"):
        Range(X, None, None)
    with pytest.raises(QueryError, match="empty"):
        Like(X, "")
    with pytest.raises(QueryError, match="unpaired backslash"):
        Like(X, "abc\\")


def test_constants():
    assert EVERYTHING == Constant(True)
    assert NOTHING == Constant(False)


# --- literals ---------------------------------------------------------------


def test_solr_literals():
    assert now() == SolrLiteral("NOW")
    assert days_ago(7) == SolrLiteral("NOW-7DAYS")
    assert days_ago(0) == now()
    with pytest.raises(ValueError):
        days_ago(-1)
    with pytest.raises(TypeError):
        days_ago(1.5)
    with pytest.raises(TypeError):
        days_ago(True)


def test_order_defaults():
    assert Order(X) == Order(X, False)
    assert Order(X, True) != Order(X)


# --- patterns ---------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern, tokens",
    [
        ("abc", ("abc",)),
        ("a*b?c", ("a", Wildcard.ANY, "b", Wildcard.ONE, "c")),
        ("**", (Wildcard.ANY, Wildcard.ANY)),
        (r"a\*b", ("a*b",)),
        (r"a\?\\", ("a?\\",)),
        (r"\-x", ("-x",)),
        ("50%", ("50%",)),
        ("", ()),
    ],
)
def test_parse_pattern(pattern, tokens):
    assert parse_pattern(pattern) == tokens


def test_parse_pattern_percent_alias():
    assert parse_pattern("Casey%", percent=True) == ("Casey", Wildcard.ANY)
    assert parse_pattern(r"50\%", percent=True) == ("50%",)


def test_format_and_escape_pattern_round_trip():
    text = r"what? a*b\c"
    assert escape_pattern(text) == r"what\? a\*b\\c"
    assert parse_pattern(escape_pattern(text)) == (text,)
    tokens = ("a*", Wildcard.ANY, "\\", Wildcard.ONE)
    assert parse_pattern(format_pattern(tokens)) == tokens
    assert format_pattern(parse_pattern("Casey%", percent=True)) == "Casey*"


# --- walk / transform -------------------------------------------------------


REL = object()


def test_walk_is_pre_order_and_reaches_func_and_related():
    inner = Related(REL, And((c, Not(d))))
    expr = Or((And((a, b)), Func("f", (1, inner, "s", Order(X)))))
    assert list(walk(expr)) == [
        expr,
        And((a, b)),
        a,
        b,
        expr.operands[1],
        inner,
        And((c, Not(d))),
        c,
        Not(d),
        d,
    ]


def test_walk_leaf():
    assert list(walk(InLibrary("abc"))) == [InLibrary("abc")]


def test_transform_is_bottom_up_and_rebuilds_containers():
    seen = []

    def record(node: Expr) -> Expr:
        seen.append(node)
        return node

    expr = Not(And((a, Func("f", (b, 2)))))
    assert transform(expr, record) == expr
    assert seen == [a, b, Func("f", (b, 2)), And((a, Func("f", (b, 2)))), expr]


def test_transform_replaces_nodes_everywhere():
    def swap(node: Expr) -> Expr:
        return Match(Y, node.value) if isinstance(node, Match) else node

    expr = Or((a, Not(b), Func("f", (c, 3)), Related(REL, d), Raw("q")))
    assert transform(expr, swap) == Or(
        (Match(Y, "a"), Not(Match(Y, "b")), Func("f", (Match(Y, "c"), 3)), Related(REL, Match(Y, "d")), Raw("q"))
    )


# --- simplify ---------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, expected",
    [
        (And((a, And((b, And((c,)))))), And((a, b, c))),
        (Or((Or((a, b)), c)), Or((a, b, c))),
        (And((a, b, a, c, b)), And((a, b, c))),
        (Or((b, a, b)), Or((b, a))),
        (And((a, NOTHING, b)), NOTHING),
        (And((a, EVERYTHING, b)), And((a, b))),
        (And((EVERYTHING, a)), a),
        (And((EVERYTHING, EVERYTHING)), EVERYTHING),
        (Or((a, EVERYTHING)), EVERYTHING),
        (Or((a, NOTHING, b)), Or((a, b))),
        (Or((NOTHING,)), NOTHING),
        (And(()), EVERYTHING),
        (Or(()), NOTHING),
        (Not(EVERYTHING), NOTHING),
        (Not(NOTHING), EVERYTHING),
        (Not(Not(a)), a),
        (Not(Not(Not(a))), Not(a)),
        (In(X, ()), NOTHING),
        (In(X, ("a",)), a),
        (In(X, ("a", "b")), In(X, ("a", "b"))),
        (And((a,)), a),
        (Or((a,)), a),
        (Raw("q"), Raw("q")),
    ],
)
def test_simplify(expr, expected):
    assert simplify(expr) == expected


def test_simplify_folds_constants_through_nesting():
    expr = And((a, Or((Not(Not(b)), And((c, Not(EVERYTHING)))))))
    assert simplify(expr) == And((a, b))
    expr = Or((Not(And((a, NOTHING))), b))
    assert simplify(expr) == EVERYTHING
    expr = Not(Or((And((EVERYTHING, In(X, ()))), Not(EVERYTHING))))
    assert simplify(expr) == EVERYTHING


def test_simplify_dedupes_after_inner_simplification():
    assert simplify(And((a, And((a, EVERYTHING)), In(X, ("a",))))) == a


def test_simplify_inside_func_and_related():
    expr = Func("citations", (And((a, EVERYTHING)), 3))
    assert simplify(expr) == Func("citations", (a, 3))
    expr = Related(REL, Or((In(X, ("b",)), NOTHING, Not(Not(c)))))
    assert simplify(expr) == Related(REL, Or((b, c)))
    expr = And((Func("pos", (In(X, ()), 1, 1)), a))
    assert simplify(expr) == And((Func("pos", (NOTHING, 1, 1)), a))
