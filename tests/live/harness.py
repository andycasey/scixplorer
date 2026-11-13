"""Searching from the live tests: requests built by the client's own planner, and readable outcomes.

Every search goes through :func:`ads.search.plan` and :meth:`SearchPlan.request`,
so the ``q``, ``fl``, ``sort`` and endpoint are exactly what the client sends.
A rejected request does not raise: the :class:`Outcome` holds the server's
error text, so a test can show the ``q`` it sent and what the server said.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypeVar

from ads import search
from ads.client import Client, Request, Response
from ads.exceptions import APIError, DataWarning
from ads.expr import Expr, Order
from ads.models.document import Document
from ads.query import Query

from .support import LiveTransport, Verdict, redact

T = TypeVar("T")

#: Long-published, heavily cited records. Nothing about them is assumed
#: except that these bibcodes exist; expectations are derived from what the
#: first request returns.
PINNED = (
    "1998AJ....116.1009R",  # supernova evidence for an accelerating universe
    "1999ApJ...517..565P",  # Omega and Lambda from 42 high-redshift supernovae
    "2013PASP..125..306F",  # emcee
    "2016PhRvL.116f1102A",  # GW150914
    "2020A&A...641A...6P",  # Planck 2018 cosmological parameters (has '&')
)

#: Fields fetched for the pinned records; the probes are derived from them.
PINNED_FIELDS = (
    "bibcode", "id", "title", "author", "first_author", "author_count", "year", "pubdate",
    "citation_count", "reference", "identifier", "database", "aff", "institution",
    "orcid_pub", "orcid_user", "orcid_other", "bibstem",
)


def closed_set() -> Expr:
    """A fixed, finite, long-published set of records that range partitions are counted within."""
    return (Document.bibstem == "AJ") & Document.year.between(2014, 2017)


def compiled(expr: Expr, expected: str) -> Expr:
    """``expr``, after checking that the client still compiles it to ``expected`` (the form under test)."""
    actual = search.to_solr(expr)
    if actual != expected:
        raise AssertionError(
            f"the compiler no longer emits the form this live test checks; update the test.\n"
            f"  expected: {expected}\n  actual:   {actual}"
        )
    return expr


@dataclass
class Outcome:
    """One search: what was sent, and the server's answer or error."""

    q: str
    request: Request | None
    num_found: int | None = None
    docs: list[dict[str, Any]] = field(default_factory=list)
    status: int | None = None
    error: str | None = None
    data: Any = None

    @property
    def ok(self) -> bool:
        """Whether the server answered with a search result."""
        return self.error is None and self.num_found is not None

    @property
    def bibcodes(self) -> list[str]:
        return [str(doc.get("bibcode")) for doc in self.docs]

    def describe(self) -> str:
        """The request and the answer, for failure messages."""
        lines = [f"  q:        {self.q}"]
        if self.request is not None:
            params = {k: v for k, v in (self.request.params or {}).items() if k != "q"}
            fl = str(params.get("fl", ""))
            if len(fl) > 120:
                params["fl"] = fl[:117] + "..."
            lines.append(f"  request:  {self.request.method} {self.request.path} {params}")
            if self.request.content is not None:
                lines.append(f"  body:     {len(str(self.request.content).splitlines()) - 1} uploaded bibcode(s)")
        if self.error is not None:
            lines.append(f"  error:    {self.error}")
        else:
            lines.append(f"  numFound: {self.num_found}; rows returned: {len(self.docs)}")
        return "\n".join(lines)

    def explain(self, expectation: str) -> str:
        """``expectation`` followed by :meth:`describe`."""
        return f"{expectation}\n{self.describe()}"

    def require(self, expectation: str) -> Outcome:
        """This outcome, or ``AssertionError`` (with the q and the server's error) if it failed."""
        if not self.ok:
            raise AssertionError(self.explain(expectation))
        return self


class Live:
    """Sends searches for the live tests through one client and its counting transport."""

    def __init__(self, client: Client, transport: LiveTransport) -> None:
        self.client = client
        self.transport = transport

    def search(
        self,
        expr: Expr | None = None,
        *,
        fields: Sequence[str] = (),
        order: Sequence[Order] = (),
        start: int = 0,
        rows: int = 0,
    ) -> Outcome:
        """Search as the client would for ``Document.select(*fields).where(expr).order_by(*order)``.

        With ``rows=0`` and no ``fields`` the request is the one ``Query.count()``
        sends (``fl=bibcode``), so counts are shared through the session cache.
        """
        query = Document.select(*fields)
        if expr is not None:
            query = query.where(expr)
        if order:
            query = query.order_by(*order)
        plan = search.plan(query)
        if plan.empty:
            return Outcome(plan.q, None, num_found=0)
        fl = ("bibcode",) if rows == 0 and not fields else None
        return self.send(plan.request(start, rows, fl=fl), plan.q)

    def count(self, expr: Expr | None) -> Outcome:
        """The number of documents matching ``expr`` (``rows=0``), as ``Query.count()`` asks for it."""
        return self.search(expr)

    def total(self, expr: Expr | None, expectation: str) -> int:
        """``numFound`` for ``expr``, or ``AssertionError`` with the q and the server's error."""
        found = self.count(expr).require(expectation).num_found
        assert found is not None
        return found

    def send(self, request: Request, q: str) -> Outcome:
        """Send a search request; never raises for an API error (the outcome holds it)."""
        try:
            response = self.client.send(request)
        except APIError as exc:
            return Outcome(q, request, status=exc.status_code, error=redact(str(exc), self.transport.hidden))
        return _outcome(q, request, response)

    def fetch(self, query: Query[Any]) -> list[Any]:
        """Run ``query`` end to end (``Query.all()``); ``AssertionError`` with the q and the server's error if rejected."""
        try:
            return query.using(self.client).all()
        except APIError as exc:
            raise AssertionError(
                f"the query was rejected\n  q:        {search.plan(query).q}\n"
                f"  error:    {redact(str(exc), self.transport.hidden)}"
            ) from None

    def tally(self, query: Query[Any]) -> int:
        """``Query.count()`` end to end; ``AssertionError`` with the q and the server's error if rejected."""
        try:
            return query.using(self.client).count()
        except APIError as exc:
            raise AssertionError(
                f"the count was rejected\n  q:        {search.plan(query).q}\n"
                f"  error:    {redact(str(exc), self.transport.hidden)}"
            ) from None

    def documents(self, outcome: Outcome) -> tuple[list[Document], list[str]]:
        """Load the outcome's rows as the client does, returning the documents and any DataWarning texts."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DataWarning)
            documents = [Document._from_row(row) for row in outcome.docs]
        return documents, [str(w.message) for w in caught if issubclass(w.category, DataWarning)]


def _outcome(q: str, request: Request, response: Response) -> Outcome:
    data = response.data
    body = data.get("response") if isinstance(data, dict) else None
    docs = body.get("docs") if isinstance(body, dict) else None
    found = body.get("numFound") if isinstance(body, dict) else None
    if not isinstance(docs, list) or isinstance(found, bool) or not isinstance(found, int):
        text = repr(data)
        return Outcome(
            q, request, status=response.status_code, data=data,
            error=f"unexpected response shape (no response.docs/numFound): {text[:300]}",
        )
    return Outcome(q, request, num_found=found, docs=docs, status=response.status_code, data=data)


def summarise(verdict: Verdict[Outcome]) -> str:
    """A verdict on one line: its status and the server's error or numFound."""
    evidence = verdict.evidence
    if evidence is None:
        return verdict.status
    return f"{verdict.status}: {evidence.error or f'numFound {evidence.num_found}'}"


def complete(gen: Generator[Request, Response, T], client: Client) -> T:
    """Run a sans-IO generator that only yields requests (such as ``fetch_bibcodes``) and return its result."""
    try:
        request = next(gen)
        while True:
            request = gen.send(client.send(request))
    except StopIteration as stop:
        result: T = stop.value
        return result


# Values derived from the pinned records.


def name_parts(name: str) -> tuple[str, str]:
    """``("Riess", "Adam G.")`` from ``"Riess, Adam G."``."""
    last, _, given = name.partition(",")
    return last.strip(), given.strip()


def has_full_given_name(name: str) -> bool:
    """Whether the first given name is spelled out (``"Adam G."``, not ``"A. G."``)."""
    given = name_parts(name)[1]
    first = given.split()[0] if given.split() else ""
    return len(first) >= 2 and first.isalpha()


def leading_words(text: str, most: int = 4) -> str | None:
    """The first words of ``text`` (up to ``most``) while they are plain alphabetic words; ``None`` if under two."""
    words = []
    for word in text.split():
        if not re.fullmatch(r"[A-Za-z]+", word):
            break
        words.append(word)
        if len(words) == most:
            break
    return " ".join(words) if len(words) >= 2 else None


def choose_lead(records: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The first pinned record suited to the probes, or ``None``.

    It needs at least three authors, a first author with a spelled-out given
    name, first and second authors with different last names, and a title
    starting with at least two plain words.
    """
    for bibcode in PINNED:
        record = records.get(bibcode)
        if record is None:
            continue
        authors = record.get("author") or []
        titles = record.get("title") or []
        if len(authors) < 3 or not titles or leading_words(titles[0]) is None:
            continue
        if not has_full_given_name(authors[0]):
            continue
        if name_parts(authors[0])[0].lower() == name_parts(authors[1])[0].lower():
            continue
        return record
    return None


def first_value(values: Any) -> str | None:
    """The first entry of a multivalued field that is a real value (not ``"-"`` or blank)."""
    for value in values or []:
        if isinstance(value, str) and value.strip() and value.strip() != "-":
            return value.strip()
    return None


def arxiv_id(identifiers: Any) -> str | None:
    """An arXiv identifier of the record, as listed in ``identifier`` without the ``arXiv:`` prefix."""
    for value in identifiers or []:
        if not isinstance(value, str):
            continue
        bare = value[len("arXiv:"):] if value.lower().startswith("arxiv:") else value
        if re.fullmatch(r"\d{4}\.\d{4,5}|[a-z\-]+(\.[A-Z]{2})?/\d{7}", bare):
            return bare
    return None


def alias_value(alias: str, record: dict[str, Any]) -> str | None:
    """A value that ``alias:value`` should match on ``record`` (derived from its own fields)."""
    title = (record.get("title") or [""])[0]
    if alias == "arxiv":
        return arxiv_id(record.get("identifier"))
    if alias == "collection":
        return first_value(record.get("database"))
    if alias == "affil":
        aff = first_value(record.get("aff"))
        return None if aff is None else leading_words(re.sub(r"[^A-Za-z ]", " ", aff), 2)
    if alias == "inst":
        inst = first_value(record.get("institution"))
        return None if inst is None else inst.split(";")[0].strip() or None
    if alias == "orcid":
        for name in ("orcid_pub", "orcid_user", "orcid_other"):
            orcid = first_value(record.get(name))
            if orcid is not None:
                return orcid
        return None
    if alias in ("full", "abs"):
        return leading_words(title)
    raise ValueError(alias)


def month_of(pubdate: str) -> date:
    """The first day of the month of an ADS ``pubdate`` such as ``2020-06-00``."""
    year, month = pubdate.split("-")[:2]
    return date(int(year), int(month), 1)
