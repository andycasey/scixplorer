"""Documents in ADS / SciX, searched with the ADS search API.

Field definitions follow the search index's Solr schema (``schema.xml`` in the
``adsabs/montysolr`` repository; ``scripts/schema_report.py`` compares the two)
and the query parser's virtual fields (``abs``, ``full``, ``inst``, ...).
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Iterable, Sequence
from typing import Any, ClassVar, TypeAlias

from ..exceptions import LazyLoadWarning, QueryError
from ..expr import Expr, Func, In, InLibrary, Order, Raw
from ..fields import (
    DateField,
    DateTimeField,
    Field,
    FieldExpr,
    FloatField,
    IntegerField,
    NameField,
    StringField,
    TextField,
    VirtualField,
)
from ..model import Model, Relation
from ..query import Query
from ..search import ResultPage, SearchBackend
from ..utils import parse_bibcode, to_bibcodes
from .affiliation import Affiliation
from .journal import Journal
from .library import Library

__all__ = ["Document", "SearchQuery"]

#: One entry of ``Document.affiliation``: per author, an affiliation, several, or none.
AuthorAffiliation: TypeAlias = Affiliation | tuple[Affiliation, ...] | None


def _journal_of(document: Document) -> Journal | None:
    """The journal named by the bibcode's journal part, else by the first bibstem."""
    bibcode = document.bibcode
    if bibcode is not None:
        try:
            abbreviation = parse_bibcode(bibcode)["journal_abbreviation"]
        except ValueError:
            pass
        else:
            return Journal.lookup(abbreviation)
    bibstems = document.bibstem
    return Journal.lookup(bibstems[0]) if bibstems else None


def _affiliations_of(document: Document) -> list[AuthorAffiliation] | None:
    """One entry per author, parsed from ``aff_id``.

    ``"-"`` (no affiliation) gives ``None``; ``"A1; A2"`` gives a tuple of the
    affiliations; ids missing from the packaged data are skipped.
    """
    entries = document.aff_id
    if entries is None:
        return None
    return [_author_affiliation(entry) for entry in entries]


def _author_affiliation(entry: str) -> AuthorAffiliation:
    ids = (part.strip() for part in entry.split(";"))
    found = [Affiliation.lookup(i) for i in ids if i and i != "-"]
    known = [affiliation for affiliation in found if affiliation is not None]
    if not known:
        return None
    return known[0] if len(known) == 1 else tuple(known)


def _operand(name: str, expr: Expr | str) -> Expr:
    """The expression argument of an operator: an ``Expr``, or a ``str`` of raw Solr syntax."""
    if isinstance(expr, Expr):
        return expr
    if isinstance(expr, str):
        if not expr.strip():
            raise QueryError(f"Document.{name}() needs a non-empty query")
        return Raw(expr)
    raise TypeError(f"Document.{name}() needs an expression or a Solr query string, not {expr!r}")


def _operator(name: str, solr_name: str, doc: str) -> classmethod[Any, [Expr | str], Expr]:
    """A classmethod building the one-argument Solr operator ``solr_name(expr)``."""

    def operator(cls: Any, expr: Expr | str) -> Expr:
        return Func(solr_name, (_operand(name, expr),))

    operator.__name__ = name
    operator.__qualname__ = f"Document.{name}"
    operator.__doc__ = doc
    return classmethod(operator)


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, not {value!r}")
    if value < 1:
        raise QueryError(f"{name} must be at least 1, not {value}")
    return value


def _outside_package_stacklevel() -> int:
    """The ``stacklevel`` for a warning raised by the caller, pointing at the first frame outside ``ads``."""
    level = 1
    frame = sys._getframe(1)
    while frame.f_back is not None and frame.f_globals.get("__name__", "").split(".")[0] == "ads":
        frame = frame.f_back
        level += 1
    return level


class Document(Model):
    """A document (paper, book, dataset, software, ...) in ADS / SciX.

    Search with :meth:`select` and ``where``::

        Document.select(Document.title).where(
            (Document.author[0] == "Casey, A") & Document.year.between(2015, 2020)
        ).order_by(Document.citation_count.desc()).limit(10)

    Fields hold the values ADS returned; ``None`` means ADS had no value.
    Search-only fields (such as :attr:`full`) can be queried but not read.

    Deferred fields (``_deferred``): ``ack``, ``body``, ``caption``,
    ``citation``, ``credit``, ``mention`` and ``reference`` are not fetched by
    a plain ``Document.select()``, because they are large (full text, or lists
    of bibcodes that can run to tens of thousands) and rarely needed. They are
    fetched when selected explicitly, or loaded on first access.

    Identity: a document is identified by the first of its identifiers
    (``_identifiers``: ``bibcode``, ``scix_id``, ``id``) that has a value. Two
    documents are equal when that is the same field with the same value; a
    document with no identifier is equal only to itself.

    Lazy loading: reading a stored field that was not fetched sends one search
    request that loads it for every document of the same result page that
    lacks it, with a :class:`~ads.exceptions.LazyLoadWarning`. Documents are
    found by their identifier (in practice the bibcode; documents of the page
    identified by another field are loaded with one more request per field).
    The request is synchronous, even inside async code, so there select the
    fields you need. The identifiers (always fetched) and ``score`` (only
    meaningful for the query that produced the document) are never loaded
    this way, and a document with no identifier loads nothing.

    Relations: :attr:`journal` (from the bibcode) and :attr:`affiliation`
    (from ``aff_id``) give :class:`~ads.models.journal.Journal` and
    :class:`~ads.models.affiliation.Affiliation` objects, and can be searched
    by their fields (``Document.journal.title.contains("astro")``), as can those
    models' fields directly (``Journal.title.contains("astro")``).
    """

    #: The fields that can identify a document, in order of preference. Every
    #: search fetches them all, and identity, lazy loading and ``repr`` use the
    #: first one that has a value.
    _identifiers: ClassVar[tuple[str, ...]] = ("bibcode", "scix_id", "id")
    _backend = SearchBackend()
    _deferred: ClassVar[frozenset[str]] = frozenset(
        {"ack", "body", "caption", "citation", "credit", "mention", "reference"}
    )
    #: Fields never loaded on access: identifiers are always fetched, and a
    #: score belongs to the query that produced the document.
    _not_lazy: ClassVar[frozenset[str]] = frozenset({*_identifiers, "score"})

    abs = VirtualField("Search for a word or phrase in abstract, title, and keywords.")
    abstract = TextField("The document abstract.")
    ack = TextField("The acknowledgements extracted from the full text.")
    aff = TextField("The raw, provided affiliation field: one string per author.", multivalued=True)
    aff_abbrev = TextField("Abbreviations of the curated affiliations, one entry per author.", multivalued=True)
    aff_canonical = TextField("Canonical names of the curated affiliations, one entry per author.", multivalued=True)
    aff_id = StringField(
        "Curated affiliation identifiers, parsed from the given affiliation string, one entry per "
        "author ('-' for none; 'A1; A2' for several). See https://ui.adsabs.harvard.edu/blog/affils-update.",
        multivalued=True,
    )
    affil = VirtualField(
        "Search all affiliation fields at once: raw strings, canonical names, abbreviations and identifiers."
    )
    all = VirtualField("Search by author_norm, alternate_title, bibcode, doi, and identifier.")
    alternate_bibcode = StringField("Alternate bibcodes of the record.", multivalued=True)
    alternate_title = TextField(
        "Alternate title, usually present when the original title is not in English.", multivalued=True
    )
    arxiv = VirtualField("Search by arXiv identifier (an alias of identifier).")
    arxiv_class = TextField("The arXiv classes a document was submitted to.", multivalued=True)
    author = NameField("Author names, 'Lastname, First M', in order.", multivalued=True)
    author_count = IntegerField("The number of authors.")
    author_norm = StringField("Author names in the form 'Lastname, F'.", multivalued=True)
    bibcode = StringField(
        "Document bibliographic code. See https://ui.adsabs.harvard.edu/help/actions/bibcode for more information."
    )
    bibgroup = StringField("Records by bibliographic groups, curated by staff outside of ADS.", multivalued=True)
    bibstem = StringField("Abbreviated name(s) of the journal or publication (e.g., ApJ).", multivalued=True)
    body = TextField("The full text, where ADS has it. Search it with body == '...'.")
    book_author = NameField("Authors of the book containing the record (e.g. for a chapter).", multivalued=True)
    caption = TextField("Figure and table captions extracted from the full text.", multivalued=True)
    citation = StringField("Bibcodes of the documents citing this one.", multivalued=True)
    citation_count = IntegerField("The number of citations to this document.")
    citation_count_norm = FloatField("Normalised citation count: citations divided by the number of authors.")
    cite_read_boost = FloatField("The normalized boost factor.")
    classic_factor = IntegerField(
        "A classic prestige score, designed to more highly rank papers that are relevant and popular now."
    )
    collection = VirtualField("Search by collection (an alias of database), e.g. 'astronomy'.")
    comment = TextField("Comments on the record, e.g. from the publisher.", multivalued=True)
    copyright = TextField("The copyright statement.")
    credit = StringField("Bibcodes of the documents that mention (credit) this record.", multivalued=True)
    credit_count = IntegerField("The number of documents that mention (credit) this record.")
    data = StringField(
        "Related data sources. For example: data == 'CDS' will return records that have CDS data.",
        multivalued=True,
    )
    data_count = IntegerField("The number of related data sources.")
    database = StringField(
        "The database(s) the document resides in (e.g., astronomy or physics).", multivalued=True
    )
    date = DateTimeField(
        "Publication date, represented by a time format and used for indexing. "
        "For example: date == datetime(2015, 7, 1)."
    )
    doctype = StringField("Search documents by their type: article, thesis, et cetera.")
    doi = StringField("Digital object identifier(s).", multivalued=True)
    editor = NameField("Editors of the publication.", multivalued=True)
    eid = StringField("Electronic identifier of the paper (equivalent of page number).")
    email = TextField(
        "Search by email addresses for the authors that included them in the article ('-' for none).",
        multivalued=True,
    )
    entry_date = DateTimeField(
        "Creation date of the ADS record. Note that this can be used like Document.entry_date >= days_ago(7)."
    )
    esources = StringField(
        "Types of electronic sources available for a record (e.g., pub_html, eprint_pdf).", multivalued=True
    )
    facility = TextField(
        "Facilities declared in a record, based on a controlled list by AAS journals.", multivalued=True
    )
    first_author = NameField("The first author's name, 'Lastname, First M'.")
    first_author_norm = StringField("The first author's name in the form 'Lastname, F'.")
    full = VirtualField("Search by title, abstract, body, keyword, and ack.")
    gpn = TextField(
        "Planetary features named in the record, from the Gazetteer of Planetary Nomenclature.",
        multivalued=True,
    )
    gpn_id = IntegerField("Gazetteer of Planetary Nomenclature identifiers of those features.", multivalued=True)
    grant = TextField("Search by grant identifiers and grant agencies.", multivalued=True)
    grant_agencies = VirtualField(
        "A field with just the grant agencies name (e.g., NASA). Not in the published index schema."
    )
    grant_id = VirtualField("Search by grant identifier. Not in the published index schema.")
    has = StringField(
        "The names of the fields this record has values for; e.g. has == 'body' finds records with full text.",
        multivalued=True,
    )
    id = StringField("A unique identifier for the document, curated by ADS.")
    identifier = StringField(
        "Search by an array of alternative identifiers for a record. May contain alternative bibcodes, "
        "DOIs, and/or arXiv identifiers.",
        multivalued=True,
    )
    indexstamp = DateTimeField("Datetime when the document was last indexed by the ADS Solr service.")
    inst = VirtualField(
        "Find records that contain a curated (ADS-identified) affiliation or institution, including its "
        "child institutions. See also: Document.affiliation."
    )
    institution = StringField(
        "Curated institution abbreviations, one entry per author (searched by inst).", multivalued=True
    )
    isbn = StringField("International Standard Book Number(s).", multivalued=True)
    issn = StringField("International Standard Serial Number(s).", multivalued=True)
    issue = StringField("Issue number of the journal that includes the article.")
    keyword = TextField(
        "An array of normalized and non-normalized keyword values associated with the record.", multivalued=True
    )
    keyword_norm = TextField("Keywords normalised to a controlled vocabulary.", multivalued=True)
    keyword_schema = TextField("The vocabulary each keyword in keyword comes from.", multivalued=True)
    lang = StringField("The language of the main title. Search-only.", stored=False)
    links_data = StringField(
        "Information on what linked documents are available (JSON strings). Returned, but not searchable.",
        multivalued=True,
        indexed=False,
    )
    mention = StringField("Bibcodes of the records (e.g. software) this document mentions.", multivalued=True)
    mention_count = IntegerField("The number of records (e.g. software) this document mentions.")
    nedid = StringField("List of NED IDs for a record.", multivalued=True)
    nedtype = StringField("Keywords used to describe the NED type (e.g., galaxy, star).", multivalued=True)
    orcid = VirtualField("ORCID identifier, from all possible sources.")
    orcid_other = TextField("ORCID claims from users who used the ADS claiming interface.", multivalued=True)
    orcid_pub = TextField("ORCIDs supplied by publishers.", multivalued=True)
    orcid_user = TextField(
        "ORCID claims from users who gave ADS consent to expose their public profile.", multivalued=True
    )
    page = StringField("Page number of a record.", multivalued=True)
    page_count = IntegerField(
        "If page_range is present, it gives the difference between the first and last page numbers in the range."
    )
    page_range = StringField("The page range of the record. Returned, but not searchable.", indexed=False)
    planetary_feature = TextField("Planetary features mentioned in the record.", multivalued=True)
    planetary_feature_id = IntegerField("Identifiers of those planetary features.", multivalued=True)
    property = StringField(
        "An array of miscellaneous flags associated with a record. Examples include: refereed, "
        "notrefereed, article, nonarticle, ads_openaccess, eprint_openaccess, pub_openaccess, "
        "openaccess, ocrabstract.",
        multivalued=True,
    )
    pub = StringField("The canonical name of the publication that the record appeared in.")
    pub_abbrev = StringField("The abbreviated name of the publication.")
    pub_raw = TextField("Name of the publisher, but also includes the volume, page, and issue if exists.")
    pubdate = DateField("Publication date (to month-level granularity only).")
    publisher = TextField("Name of the publisher.")
    pubnote = TextField("Notes about the publication, e.g. 'Accepted for publication in ApJ'.", multivalued=True)
    read_count = IntegerField("The number of times the record has been viewed within a 90 day window.")
    reference = StringField("Bibcodes of the documents this one cites.", multivalued=True)
    reference_count = IntegerField("The number of documents this one cites.")
    score = FloatField(
        "The closeness of the document to the query match (Solr's relevance score for the query "
        "that returned it; not an index field)."
    )
    scix_id = StringField("The SciX identifier of the record.")
    series = TextField("Name of the series, e.g. of a book or of conference proceedings.")
    simbid = IntegerField(
        "List of SIMBAD IDs within a document. This field has privacy restrictions.", multivalued=True
    )
    simbtype = TextField("Keywords used to describe the SIMBAD type.", multivalued=True)
    thesis = TextField("Thesis information, such as the degree-granting institution.")
    title = TextField("The title of the record (and any other titles it has).", multivalued=True)
    uat = StringField(
        "Unified Astronomy Thesaurus (UAT) concepts assigned to the record.", multivalued=True
    )
    uat_id = IntegerField("Unified Astronomy Thesaurus concept identifiers.", multivalued=True)
    vizier = StringField("Keywords, 'subject' tags from Vizier.", multivalued=True)
    volume = StringField("The journal volume.")
    year = IntegerField("Year of publication.")

    journal: Relation[Journal | None] = Relation(
        Journal,
        key="bibstem",
        related_key="abbreviation",
        resolve=_journal_of,
        doc="The journal the document appeared in, from the journal part of its bibcode (None if unknown).",
    )
    affiliation: Relation[list[AuthorAffiliation] | None] = Relation(
        Affiliation,
        key="aff_id",
        related_key="id",
        resolve=_affiliations_of,
        doc=(
            "The curated affiliations, one entry per author: None, an Affiliation, or a tuple of them "
            "(from aff_id; ids missing from the packaged data are skipped). Use ads.utils.flatten() "
            "to scan them."
        ),
    )

    def __init__(self, **values: Any) -> None:
        self._page: ResultPage | None = None
        super().__init__(**values)

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state["_page"] = None
        return state

    def _key_value(self) -> tuple[str, Any] | None:
        """The first identifier with a value, as ``(field name, value)``; ``None`` if there is none.

        ``Model`` compares and hashes instances by this.
        """
        for name in self._identifiers:
            value = self._data.get(name)
            if value is not None:
                return name, value
        return None

    def __repr__(self) -> str:
        identity = self._key_value()
        if identity is None:
            return "<Document>"
        name, value = identity
        return f"<Document: {name}={value!r}>"

    def __str__(self) -> str:
        identity = self._key_value()
        return repr(self) if identity is None else str(identity[1])

    # Second-order and other operators.

    citations = _operator(
        "citations",
        "citations",
        """Documents that cite the documents matching ``expr``.

        ``Document.citations(Document.bibcode == "2003AJ....125..525J")`` finds the
        documents citing that one; :meth:`references` finds those it cites. See
        https://ui.adsabs.harvard.edu/help/search/citations-and-references.

        :param expr: an expression, or a Solr query string.
        """,
    )
    references = _operator(
        "references",
        "references",
        """Documents cited by the documents matching ``expr``.

        ``Document.references(Document.bibcode == "2003AJ....125..525J")`` finds the
        documents that one cites; :meth:`citations` finds those citing it. See
        https://ui.adsabs.harvard.edu/help/search/citations-and-references.

        :param expr: an expression, or a Solr query string.
        """,
    )
    trending = _operator(
        "trending",
        "trending",
        """Documents most read by users who read recent papers matching ``expr``.

        These are papers currently being read by people interested in the field. See
        https://ui.adsabs.harvard.edu/help/search/second-order.

        :param expr: an expression, or a Solr query string.
        """,
    )
    useful = _operator(
        "useful",
        "useful",
        """Documents frequently cited by the most relevant papers matching ``expr``.

        These are usually studies that discuss methods and techniques useful to
        conduct research in the field. See https://ui.adsabs.harvard.edu/help/search/second-order.

        :param expr: an expression, or a Solr query string.
        """,
    )
    reviews = _operator(
        "reviews",
        "reviews",
        """Documents citing the most relevant papers matching ``expr``: the reviews of the field.

        Takes the papers citing those matching ``expr``, and ranks them by how many
        of them each cites. See https://ui.adsabs.harvard.edu/help/search/second-order.

        :param expr: an expression, or a Solr query string.
        """,
    )
    similar = _operator(
        "similar",
        "similar",
        """Documents whose abstracts are similar in wording to those of the documents matching ``expr``.

        See https://ui.adsabs.harvard.edu/help/search/second-order.

        :param expr: an expression, or a Solr query string.
        """,
    )
    citis = _operator(
        "citis",
        "citis",
        """Like :meth:`citations`, using less memory but more slowly.

        :param expr: an expression, or a Solr query string.
        """,
    )
    classic_relevance = _operator(
        "classic_relevance",
        "classic_relevance",
        """The documents matching ``expr``, ranked (almost) as ADS Classic's relevance score did.

        :param expr: an expression, or a Solr query string.
        """,
    )
    instructive = _operator(
        "instructive",
        "instructive",
        """A synonym of :meth:`reviews`.

        :param expr: an expression, or a Solr query string.
        """,
    )
    join_citations = _operator(
        "join_citations",
        "joincitations",
        """Like :meth:`citations`, using a Lucene block join (Solr ``joincitations()``).

        :param expr: an expression, or a Solr query string.
        """,
    )
    join_references = _operator(
        "join_references",
        "joinreferences",
        """Like :meth:`references`, using a Lucene block join (Solr ``joinreferences()``).

        :param expr: an expression, or a Solr query string.
        """,
    )
    reviews2 = _operator(
        "reviews2",
        "reviews2",
        """The original implementation of :meth:`reviews`.

        :param expr: an expression, or a Solr query string.
        """,
    )
    useful2 = _operator(
        "useful2",
        "useful2",
        """The original implementation of :meth:`useful`.

        :param expr: an expression, or a Solr query string.
        """,
    )

    @classmethod
    def top_n(cls, n: int, expr: Expr | str, order_by: Order | FieldExpr | None = None) -> Expr:
        """The first ``n`` documents matching ``expr`` (Solr ``topn()``).

        :param n: how many documents.
        :param expr: an expression, or a Solr query string.
        :param order_by: how to rank them before taking the first ``n``: an
            :class:`~ads.expr.Order` such as ``Document.citation_count.desc()``, or
            a field handle (ascending). By default, by relevance.
        """
        args: list[Any] = [_positive_int("top_n() n", n), _operand("top_n", expr)]
        if isinstance(order_by, Order):
            args.append(Order(cls._field(order_by.field), order_by.descending))
        elif isinstance(order_by, FieldExpr):
            cls._field(order_by)
            args.append(order_by.asc())
        elif order_by is not None:
            raise TypeError(f"top_n() order_by must be an Order or a field, not {order_by!r}")
        return Func("topn", tuple(args))

    @classmethod
    def pos(cls, expr: Expr | str, start: int, end: int | None = None) -> Expr:
        """Documents where ``expr`` matches at a position (1-indexed, inclusive) of a multivalued field.

        For authors prefer indexing: ``Document.author[0] == "Casey, A"`` (first
        author) is ``pos(author == ..., 1)``, and ``Document.author[0:3]`` the first
        three. ``pos`` is for expressions written as Solr strings.

        :param expr: an expression, or a Solr query string.
        :param start: the first position, counting from 1.
        :param end: the last position (inclusive); ``None`` means ``start``.
        """
        first = _positive_int("pos() start", start)
        last = first if end is None else _positive_int("pos() end", end)
        if last < first:
            raise QueryError(f"pos() end ({last}) is before start ({first})")
        return Func("pos", (_operand("pos", expr), first, last))

    @classmethod
    def raw(cls, query: str) -> Expr:
        """A condition written in Solr syntax, sent as is (combine it with ``&``, ``|``, ``~``)."""
        if not isinstance(query, str):
            raise TypeError(f"Document.raw() needs a str, not {query!r}")
        if not query.strip():
            raise QueryError("Document.raw() needs a non-empty query")
        return Raw(query)

    @classmethod
    def in_(cls, source: Any) -> Expr:
        """Documents in ``source``: a :class:`~ads.models.library.Library`, or bibcodes.

        A library is tested by ADS itself (``docs(library/<id>)``), without
        fetching its bibcodes. Anything else may be bibcode strings, documents,
        or iterables of them, and becomes ``Document.bibcode.in_(...)``.
        """
        if isinstance(source, Library):
            library_id = source._data.get("id")
            if not isinstance(library_id, str) or not library_id.strip():
                raise QueryError(f"{source!r} has no id, so it is not on ADS; use Library.create()")
            return InLibrary(library_id)
        return In(cls._fields["bibcode"], tuple(to_bibcodes(source)))

    @classmethod
    def not_in(cls, source: Any) -> Expr:
        """Documents not in ``source`` (see :meth:`in_`)."""
        return ~cls.in_(source)

    # Lazy loading.

    def _load_missing(self, field: Field[Any, Any]) -> Any:
        """Load ``field`` for this document and the documents of its page that lack it.

        Documents are found by their identifier, with one request per
        identifier field in use (in practice only ``bibcode``).
        """
        if not field.stored:
            raise AttributeError(f"Document.{field.name} is search-only and has no value on an instance")
        if field.name in self._not_lazy or self._key_value() is None:
            return None
        page = self._page
        candidates: list[Document] = [self]
        if page is not None:
            candidates += [
                member
                for member in page.members()
                if isinstance(member, Document) and member is not self and field.name not in member._data
            ]
        groups: dict[str, list[Document]] = {}
        for document in candidates:
            identity = document._key_value()
            if identity is not None:
                groups.setdefault(identity[0], []).append(document)
        count = sum(len(group) for group in groups.values())
        requests = "another search request" if len(groups) == 1 else f"{len(groups)} more search requests"
        warnings.warn(
            f"Document.{field.name} was not fetched; loading it for {count} document(s) with "
            f"{requests}. Select the fields you need, e.g. "
            f"Document.select(Document.{field.name}, ...), to avoid this.",
            LazyLoadWarning,
            stacklevel=_outside_package_stacklevel(),
        )
        client = None if page is None else page.client
        for name, group in groups.items():
            identifier: FieldExpr = getattr(Document, name)
            query = Document.select(field).where(identifier.in_([document._data[name] for document in group]))
            loaded = {row._data.get(name): row for row in query.using(client)}
            for document in group:
                row = loaded.get(document._data[name])
                document._data[field.name] = None if row is None else row._data.get(field.name)
                document._related.clear()
        return self._data[field.name]


def SearchQuery(
    q: str,
    fl: Sequence[str] | str | None = None,
    sort: str | None = None,
    rows: int | None = None,
    start: int = 0,
    fq: str | Iterable[str] | None = None,
) -> Query[Document]:
    """A document query from Solr parameters, for those who already have a query string.

    :param q: the Solr query.
    :param fl: field names to fetch (a list, or a comma-separated string); all
        non-deferred fields if ``None``.
    :param sort: e.g. ``"citation_count desc, bibcode asc"``.
    :param rows: the most documents to return (``None`` for all).
    :param start: how many leading documents to skip.
    :param fq: filter queries, AND-ed with ``q``.

    Returns ``Document.select(*fl).where(Document.raw(q), ...)`` with the order,
    limit and offset applied: a query like any other.
    """
    names = [name.strip() for name in fl.split(",")] if isinstance(fl, str) else list(fl or ())
    try:
        query = Document.select(*(name for name in names if name))
    except AttributeError as exc:
        raise QueryError(str(exc)) from None
    filters = [fq] if isinstance(fq, str) else list(fq or ())
    query = query.where(Document.raw(q), *(Document.raw(f) for f in filters))
    if sort is not None:
        query = query.order_by(*_parse_sort(sort))
    return query.limit(rows).offset(start)


def _parse_sort(sort: str) -> list[Order]:
    """Parse ``"field dir[, field dir]"`` (Solr field names) into orders."""
    by_solr_name = {field.solr_name: field for field in Document._fields.values()}
    orders = []
    for clause in sort.split(","):
        parts = clause.split()
        if len(parts) != 2 or parts[1].lower() not in ("asc", "desc"):
            raise QueryError(f"Cannot parse sort clause {clause.strip()!r}: expected 'field asc' or 'field desc'")
        field = by_solr_name.get(parts[0])
        if field is None:
            raise QueryError(f"Cannot sort by unknown field {parts[0]!r}")
        orders.append(Order(field, parts[1].lower() == "desc"))
    return orders
