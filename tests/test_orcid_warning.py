"""An ORCID field compared with a value that is not a valid ORCID iD warns, and the condition is still built."""

from __future__ import annotations

import warnings
from collections.abc import Callable

import pytest

from scixplorer import Author, Document, search
from scixplorer.exceptions import QueryWarning
from scixplorer.expr import Expr

VALID = "0000-0002-1825-0097"
MISTYPED = "0000-0002-1825-0098"


@pytest.mark.parametrize(
    ("build", "solr"),
    [
        (lambda: Document.orcid == MISTYPED, f'orcid:"{MISTYPED}"'),
        (lambda: Document.orcid != MISTYPED, f'*:* AND -orcid:"{MISTYPED}"'),
        (lambda: Document.orcid.exact(MISTYPED), f'=orcid:"{MISTYPED}"'),
        (lambda: Document.orcid.in_([VALID, MISTYPED]), f'orcid:("{VALID}" OR "{MISTYPED}")'),
        (lambda: Document.orcid_pub == MISTYPED, f'orcid_pub:"{MISTYPED}"'),
        (lambda: Document.orcid_user == MISTYPED, f'orcid_user:"{MISTYPED}"'),
        (lambda: Document.orcid_other == MISTYPED, f'orcid_other:"{MISTYPED}"'),
        (lambda: Document.authors.any(Author.orcid == MISTYPED), f'orcid:"{MISTYPED}"'),
        (lambda: Document.authors.any(Author.orcid_pub == MISTYPED), f'orcid_pub:"{MISTYPED}"'),
    ],
)
def test_invalid_orcid_warns_and_the_condition_is_still_built(build: Callable[[], Expr], solr: str) -> None:
    with pytest.warns(QueryWarning, match="wrong check digit") as caught:
        condition = build()
    assert len(caught) == 1
    assert search.to_solr(condition) == solr


def test_the_warning_names_the_field_and_the_value_and_points_at_the_caller() -> None:
    with pytest.warns(QueryWarning) as caught:
        Document.orcid == "1825-0097"  # noqa: B015
    message = str(caught[0].message)
    assert message.startswith("Document.orcid: '1825-0097' is not of the form 0000-0002-1825-0097")
    assert message.endswith("The search is sent as written.")
    assert caught[0].filename == __file__


@pytest.mark.parametrize(
    "build",
    [
        lambda: Document.orcid == VALID,
        lambda: Document.orcid.in_([VALID, "0000-0002-1694-233X"]),
        lambda: Document.authors.any(Author.orcid == VALID),
        # A pattern is not an iD, and is not checked.
        lambda: Document.orcid.like("0000-0002-1825-*"),
        lambda: Document.orcid.startswith("0000-0002"),
        # Only the ORCID fields are checked.
        lambda: Document.title == MISTYPED,
    ],
)
def test_no_warning(build: Callable[[], Expr]) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        build()
