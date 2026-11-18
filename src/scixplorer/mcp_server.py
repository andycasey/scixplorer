"""A Model Context Protocol (MCP) server for SciX, run by ``scix --mcp``.

It gives an assistant such as Claude the things scixplorer does that a plain
search API cannot: a search that reads plain words and names (journals,
institutions and Unified Astronomy Thesaurus concepts are resolved locally,
without the assistant having to know their identifiers), and says how it read
them; plus documents, exports, citation metrics and SciX libraries.

The tools are the methods of :class:`Tools`, which return plain dicts and can
be called without a server; :func:`build` registers them on an ``MCPServer``,
and :func:`serve` runs it on standard input and output.

This module needs the ``mcp`` package, version 2 or later, which is not
installed with scixplorer: install ``scixplorer[mcp]``.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .cli import default_sort
from .cli.interpret import Interpretation, Reading, _identifier, _journals, _thesaurus, interpret
from .cli.output import FIELDS, SORTS, abstract_of, authors_line, page_url, pdf_url, reference, title_of
from .cli.session import Session
from .client import Client, get_default_client
from .exceptions import APIError, SciXError
from .models.affiliation import Affiliation
from .models.document import Document
from .models.journal import Journal
from .models.library import Library
from .models.planetary import PlanetaryFeature
from .models.uat import UATConcept
from .search import to_solr
from .services import export as export_service
from .services.metrics import metrics as metrics_service

__all__ = ["INSTRUCTIONS", "Tools", "build", "serve"]

#: The most documents one call hands to the export, metrics and library services.
MAX_DOCUMENTS = 2000

SortName = Literal["date", "citations", "reads", "relevance", "oldest", "author"]

ExportFormat = Literal[
    "bibtex", "bibtexabs", "aastex", "ads", "endnote", "medlars", "procite", "refworks", "ris",
    "icarus", "mnras", "soph", "ieee", "dcxml", "refxml", "refabsxml", "votable",
]  # fmt: skip

#: What the server tells an assistant when it connects.
INSTRUCTIONS = """\
Tools for SciX (Science Explorer, the successor to NASA/ADS): the literature of astronomy, physics, \
planetary and earth science.

Start with `search`. Its query may be plain words: it reads capitalised words as author surnames, \
four-digit numbers as years, journal names and Unified Astronomy Thesaurus concepts as what they are, \
and returns `read_as`, which says how each word was understood. If that is not what was meant, name \
the fields (author:"Kewley, L" year:2015-2020 journal:ApJ title:"..."). `explain_search` shows the \
reading without searching. `resolve` finds the identifier of a journal, institution, concept or \
planetary feature by name.

Documents are identified by bibcode (such as 2019ApJ...880...16K). `get_document` gives one in full; \
`export` gives BibTeX and other formats; `metrics` gives citation statistics. Libraries are the user's \
saved collections on SciX: tools that create, change or delete them act on the user's account, so do \
so only when asked.
"""

_QUERY = """\
What to search for, in plain words or with named fields; every part must match. \
Capitalised words are author surnames (Kewley; "Kewley, L. J." with initials; ^Kewley or \
"Kewley et al." for the first author). Four-digit numbers are years (2019, 2015-2020, "since 2020"). \
A journal abbreviation or title is that journal, and numbers after it are volume and page \
(ApJ 880 16). The name of a Unified Astronomy Thesaurus concept is that topic (exoplanets, \
"hot jupiters"). A bibcode, DOI, arXiv id or ORCID iD is that identifier. Quoted phrases and other \
words are searched in title, abstract and keywords. \
To be exact, name the field: author:"Kewley, L" first_author: title: abs: year:2015-2020 \
journal:ApJ uat:<name or number> inst:"Harvard U" aff_id: orcid: doi: property:refereed \
doctype:article database:astronomy, or any other SciX field. \
Operators take a bibcode or a query: citations:2019ApJ...880...16K (documents citing it), \
references: (documents it cites), similar:, trending:exoplanets, reviews:, useful:.\
"""

_LIBRARY = "The library: its id, or its exact name."

_BIBCODES = f"Bibcodes of the documents (at most {MAX_DOCUMENTS})."

_FROM_QUERY = "Instead of bibcodes: a search, read as by `search`, whose first `limit` results are used."

_Method = TypeVar("_Method", bound=Callable[..., Any])


def _tool(method: _Method) -> _Method:
    """Report the errors a caller can act on as tool errors, with their message.

    Anything else is a fault in the server, and the MCP server hides its details.
    """

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except (SciXError, ValueError) as error:
            raise ToolError(str(error)) from error

    return wrapper  # type: ignore[return-value]


def _brief(document: Document, *, abstract: bool = False) -> dict[str, Any]:
    """A document in a few lines: enough to choose among results."""
    data = document._data
    brief: dict[str, Any] = {
        "bibcode": data.get("bibcode"),
        "title": title_of(document),
        "authors": authors_line(document, 3),
        "year": data.get("year"),
        "publication": reference(document),
        "citations": data.get("citation_count") or 0,
    }
    if abstract:
        brief["abstract"] = abstract_of(document)
    return brief


def _library(library: Library) -> dict[str, Any]:
    modified = library.date_last_modified
    return {
        "id": library.id,
        "name": library.name,
        "description": library.description,
        "documents": library.num_documents,
        "public": library.public,
        "permission": library.permission,
        "owner": library.owner,
        "last_modified": modified.isoformat() if modified is not None else None,
    }


def _ranked(text: str, rows: list[Any], names: Callable[[Any], list[str]]) -> list[Any]:
    """``rows`` with those named exactly ``text`` first, then those whose name starts with it."""
    wanted = text.strip().lower()

    def rank(row: Any) -> int:
        lowered = [name.lower() for name in names(row) if name]
        if wanted in lowered:
            return 0
        return 1 if any(name.startswith(wanted) for name in lowered) else 2

    return sorted(rows, key=rank)


class Tools:
    """What the server's tools do. Every method returns something JSON can hold.

    :param client: the client to send requests with (default: the default client).
    """

    def __init__(self, client: Client | None = None) -> None:
        self._client = client
        self.session = Session(client)

    @property
    def client(self) -> Client:
        return self._client if self._client is not None else get_default_client()

    # Searching.

    def _read(self, query: str, solr: bool = False) -> Interpretation:
        text = query.strip()
        if not text:
            raise ValueError("The query is empty: say what to search for.")
        if solr:
            return Interpretation(text, (Reading("query", text, text, Document.raw(text)),))
        return interpret(text)

    def _bibcodes(self, bibcodes: list[str] | None, query: str | None, limit: int) -> list[str]:
        """The documents a tool was given: bibcodes, or the first ``limit`` results of a search."""
        if (bibcodes is None) == (query is None):
            raise ValueError("Give either bibcodes or query, not both and not neither.")
        if bibcodes is not None:
            found = list(dict.fromkeys(bibcode.strip() for bibcode in bibcodes if bibcode.strip()))
            if not found:
                raise ValueError("No bibcodes were given.")
            return found
        interpretation = self._read(query or "")
        found, _ = self.session.bibcodes(interpretation, default_sort(interpretation), limit=limit)
        if not found:
            raise ValueError(f"The search matched no documents (read as: {interpretation}).")
        return found

    @_tool
    def search(
        self,
        query: Annotated[str, Field(description=_QUERY)],
        limit: Annotated[int, Field(ge=1, le=100, description="How many documents to return.")] = 10,
        offset: Annotated[int, Field(ge=0, description="How many documents to skip, to see the next page.")] = 0,
        sort: Annotated[
            SortName | None,
            Field(description="The order. By default: citations if the query names a year, else date (newest first)."),
        ] = None,
        abstracts: Annotated[bool, Field(description="Include each document's abstract.")] = False,
        solr: Annotated[
            bool, Field(description="Send the query to SciX as Solr syntax, exactly as written, without reading it.")
        ] = False,
    ) -> dict[str, Any]:
        """Search SciX (the successor to NASA/ADS) for papers and other documents.

        Returns how the query was read (`read_as`), the Solr query sent, the
        number of documents that match (`total`) and the documents, each with
        its bibcode, title, first authors, year, publication and citation count.
        If nothing matches, the search is widened once (a surname is also
        searched as a word, a concept also as its words) and `note` says so.
        """
        interpretation = self._read(query, solr)
        order = sort or default_sort(interpretation)
        note = None
        if offset:
            documents, total = self.session.fetch(interpretation, order, limit=limit, offset=offset)
        else:
            found = self.session.find(interpretation, order, limit=limit)
            documents, total = found.documents, found.total
            if found.widened:
                note = f"Nothing matched {interpretation}, so the search was widened."
                interpretation = found.interpretation
        result: dict[str, Any] = {
            "read_as": str(interpretation),
            "solr_query": to_solr(self.session.query(interpretation, order)),
            "sort": SORTS[order][2],
            "total": total,
            "offset": offset,
            "documents": [_brief(document, abstract=abstracts) for document in documents],
        }
        notes = [note, *interpretation.notes] if note else list(interpretation.notes)
        if notes:
            result["note"] = " ".join(notes)
        return result

    @_tool
    def explain_search(
        self,
        query: Annotated[str, Field(description=_QUERY)],
    ) -> dict[str, Any]:
        """Show how `search` would read a query, piece by piece, and the Solr query it would send.

        Nothing is sent to SciX. Use it to check a query before running it, or
        to learn the Solr syntax for something.
        """
        interpretation = self._read(query)
        order = default_sort(interpretation)
        result: dict[str, Any] = {
            "read_as": [
                {"typed": reading.source, "as": reading.kind, "meaning": reading.value}
                for reading in interpretation.readings
            ],
            "solr_query": to_solr(self.session.query(interpretation, order)),
            "sort": SORTS[order][2],
        }
        if interpretation.notes:
            result["note"] = " ".join(interpretation.notes)
        return result

    @_tool
    def get_document(
        self,
        identifier: Annotated[
            str, Field(description="A bibcode, DOI or arXiv identifier, or a link to the document on SciX, ADS or arXiv.")
        ],
    ) -> dict[str, Any]:
        """Get one document in full: all its authors, its abstract, keywords, identifiers and links."""
        text = identifier.strip()
        if not text:
            raise ValueError("The identifier is empty.")
        reading = _identifier(text)
        expr = reading.expr if reading is not None and reading.kind != "orcid" else Document.identifier == text
        fields = (*FIELDS, "pubdate", "keyword", "uat", "reference_count", "read_count")
        document = Document.select(*fields).where(expr).limit(1).using(self.client).first()
        if document is None:
            raise ToolError(f"SciX has no document with the identifier {text!r}.")
        data = document._data
        authors = data.get("author") or []
        date = data.get("pubdate")
        arxiv = [each for each in data.get("identifier") or [] if each.lower().startswith("arxiv:")]
        return {
            "bibcode": data.get("bibcode"),
            "title": title_of(document),
            "authors": authors[:50],
            "author_count": len(authors),
            "year": data.get("year"),
            "date": date.isoformat() if date is not None else None,
            "publication": data.get("pub"),
            "reference": reference(document),
            "type": data.get("doctype"),
            "refereed": "refereed" in (each.lower() for each in data.get("property") or []),
            "doi": (data.get("doi") or [None])[0],
            "arxiv": arxiv[0] if arxiv else None,
            "keywords": (data.get("keyword") or [])[:25],
            "uat_concepts": data.get("uat") or [],
            "abstract": abstract_of(document),
            "citations": data.get("citation_count") or 0,
            "references": data.get("reference_count") or 0,
            "reads": data.get("read_count") or 0,
            "url": page_url(document),
            "full_text_url": pdf_url(document),
        }

    @_tool
    def resolve(
        self,
        kind: Annotated[
            Literal["journal", "institution", "concept", "planetary_feature"],
            Field(description="What the name is of. `concept` is a Unified Astronomy Thesaurus concept."),
        ],
        name: Annotated[str, Field(description="The name, or part of it; for a journal, its abbreviation works too.")],
        limit: Annotated[int, Field(ge=1, le=50, description="How many matches to return.")] = 10,
    ) -> dict[str, Any]:
        """Find the identifier SciX uses for a journal, an institution, a UAT concept or a planetary feature.

        Nothing is sent to SciX: the names are looked up in reference data held
        locally. Each match says how to search for it (`search_with`), which
        can be put in a `search` query as it is.
        """
        text = name.strip()
        if not text:
            raise ValueError("The name is empty.")
        matches: list[dict[str, Any]]
        if kind == "journal":
            matches = self._journals(text, limit)
        elif kind == "institution":
            matches = self._institutions(text, limit)
        elif kind == "concept":
            matches = self._concepts(text, limit)
        else:
            matches = self._features(text, limit)
        return {"kind": kind, "name": text, "matches": matches}

    def _journals(self, text: str, limit: int) -> list[dict[str, Any]]:
        found: dict[str, str] = {}
        named = _journals().named(text)
        if named is not None:
            found[named[0]] = named[1]
        titled = Journal.select().where(Journal.title.contains(text) | Journal.abbreviation.startswith(text)).all()
        for journal in _ranked(text, titled, lambda journal: [journal.title or "", journal.abbreviation or ""]):
            if journal.abbreviation:
                found.setdefault(journal.abbreviation, journal.title or "")
        return [
            {"abbreviation": abbreviation, "title": title, "search_with": f'journal:"{abbreviation}"'}
            for abbreviation, title in list(found.items())[:limit]
        ]

    def _institutions(self, text: str, limit: int) -> list[dict[str, Any]]:
        condition = Affiliation.canonical_name.contains(text) | Affiliation.abbreviation.contains(text)
        rows = Affiliation.select().where(condition | (Affiliation.id == text)).all()
        ranked = _ranked(text, rows, lambda row: [row.abbreviation or "", row.canonical_name or "", row.id or ""])
        matches = []
        for row in ranked[:limit]:
            parent = row.parent
            match = {
                "id": row.id,
                "abbreviation": row.abbreviation,
                "name": row.canonical_name,
                "country": row.country,
                "part_of": parent.abbreviation if parent is not None else None,
                "search_with": f"aff_id:{row.id}",
            }
            if parent is None:
                # inst: also finds the institution's departments, which aff_id: does not. A department's
                # abbreviation ("Sch Phy") is shared with other institutions', so only its id is given.
                match["search_with"] = f'inst:"{row.abbreviation}"'
                match["search_without_its_parts_with"] = f"aff_id:{row.id}"
            matches.append(match)
        return matches

    def _concepts(self, text: str, limit: int) -> list[dict[str, Any]]:
        thesaurus = _thesaurus()
        if thesaurus is None:
            UATConcept.select().first()  # raises DataNotAvailable, which says what to run
        rows = UATConcept.select().where((UATConcept.name == text) | (UATConcept.alt_names == text)).all()
        exact = thesaurus.named(text) if thesaurus is not None else None
        if exact is not None and exact not in rows:
            rows.insert(0, exact)
        ranked = _ranked(text, rows, lambda row: [row.name or "", *(row.alt_names or ())])
        if exact is not None:
            ranked = [exact, *(row for row in ranked if row != exact)]
        return [
            {
                "id": row.id,
                "name": row.name,
                "other_names": list(row.alt_names or ()),
                "broader": [parent.name for parent in row.parents],
                "narrower": [child.name for child in row.children.limit(15)],
                "search_with": f"uat:{row.id}",
            }
            for row in ranked[:limit]
            if not row.deprecated
        ]

    def _features(self, text: str, limit: int) -> list[dict[str, Any]]:
        rows = PlanetaryFeature.select().where(PlanetaryFeature.name == text).all()
        ranked = _ranked(text, rows, lambda row: [row.name or ""])
        return [
            {
                "id": row.id,
                "name": row.name,
                "on": row.target,
                "type": row.feature_type,
                "search_with": f"planetary_feature_id:{row.id}",
            }
            for row in ranked[:limit]
        ]

    # Services.

    @_tool
    def export(
        self,
        bibcodes: Annotated[list[str], Field(min_length=1, max_length=MAX_DOCUMENTS, description=_BIBCODES)],
        format: Annotated[ExportFormat, Field(description="The format; bibtexabs is BibTeX with abstracts.")] = "bibtex",
    ) -> str:
        """Export documents as BibTeX, AASTeX, RIS, EndNote or another reference format."""
        text: str = getattr(export_service, format)(self._bibcodes(bibcodes, None, 0), client=self.client)
        return text

    @_tool
    def metrics(
        self,
        bibcodes: Annotated[
            list[str] | None, Field(min_length=1, max_length=MAX_DOCUMENTS, description=_BIBCODES)
        ] = None,
        query: Annotated[str | None, Field(description=_FROM_QUERY)] = None,
        limit: Annotated[
            int, Field(ge=1, le=MAX_DOCUMENTS, description="With `query`: how many of its results to use.")
        ] = MAX_DOCUMENTS,
        per_year: Annotated[
            bool, Field(description="Also give the counts and indicators for each year (a long answer).")
        ] = False,
    ) -> dict[str, Any]:
        """Citation and usage statistics for a set of documents taken together.

        Gives the number of papers, reads and downloads (`basic`), citations
        (`citations`), and indicators such as the h-index (`indicators`: h, g,
        i10, i100, m, tori, riq, read10); `refereed` holds the same for the
        refereed documents alone. Give bibcodes, or a query such as
        orcid:0000-0002-1825-0097 for everything by one author.
        """
        chosen = self._bibcodes(bibcodes, query, limit)
        types = ["basic", "citations", "indicators"] + (["histograms", "timeseries"] if per_year else [])
        data = metrics_service(chosen, types=types, client=self.client)
        sections = {"basic": "basic stats", "citations": "citation stats", "indicators": "indicators"}

        def section(key: str) -> dict[str, Any]:
            value = data.get(key)
            # The list of self-citations can run to thousands of bibcodes; its count is kept.
            return {name: each for name, each in value.items() if name != "self-citations"} if isinstance(value, dict) else {}

        result: dict[str, Any] = {"documents": len(chosen), "skipped": data.get("skipped bibcodes") or []}
        result.update({name: section(key) for name, key in sections.items()})
        result["refereed"] = {name: section(f"{key} refereed") for name, key in sections.items()}
        if per_year:
            result["per_year"] = {"histograms": data.get("histograms"), "time_series": data.get("time series")}
        return result

    # Libraries.

    def _find_library(self, library: str) -> Library:
        """The library with this id or name: one of the user's, or a public one by id."""
        wanted = library.strip()
        if not wanted:
            raise ValueError("The library is empty: give its id or its name.")
        mine = Library.select().using(self.client).all()
        tests: tuple[Callable[[Library], bool], ...] = (
            lambda each: each.id == wanted,
            lambda each: each.name == wanted,
            lambda each: (each.name or "").lower() == wanted.lower(),
        )
        for test in tests:
            found = [each for each in mine if test(each)]
            if len(found) > 1:
                ids = ", ".join(str(each.id) for each in found)
                raise ToolError(f"{len(found)} libraries are named {wanted!r}; give the id of one of them: {ids}.")
            if found:
                found[0].client = self.client
                return found[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{16,}", wanted):
            # Not one of the user's: it may be someone else's public library.
            try:
                other = Library.select().where(Library.id == wanted).using(self.client).first()
            except APIError:
                other = None
            if other is not None:
                other.client = self.client
                return other
        names = ", ".join(repr(each.name) for each in mine) or "none"
        raise ToolError(f"No library has the id or name {wanted!r}. The user's libraries: {names}.")

    @_tool
    def list_libraries(self) -> dict[str, Any]:
        """List the user's SciX libraries: their ids, names, descriptions and sizes."""
        libraries = Library.select().using(self.client).all()
        return {"libraries": [_library(library) for library in libraries]}

    @_tool
    def get_library(
        self,
        library: Annotated[str, Field(description=_LIBRARY)],
        limit: Annotated[int, Field(ge=1, le=100, description="How many of its documents to return.")] = 25,
        offset: Annotated[int, Field(ge=0, description="How many documents to skip, to see the next page.")] = 0,
        sort: Annotated[SortName | None, Field(description="The order of the documents (default: date, newest first).")] = None,
    ) -> dict[str, Any]:
        """Get a SciX library: what it is, and the documents in it."""
        found = self._find_library(library)
        name = found.name or str(found.id)
        contents = Interpretation(name, (Reading("library", name, name, Document.in_(found)),))
        documents, total = self.session.fetch(contents, sort or "date", limit=limit, offset=offset)
        return {
            "library": _library(found),
            "total": total,
            "offset": offset,
            "documents": [_brief(document) for document in documents],
        }

    @_tool
    def create_library(
        self,
        name: Annotated[str, Field(min_length=1, description="The name, which no other library of the user may have.")],
        description: Annotated[str | None, Field(description="What the library is for.")] = None,
        public: Annotated[bool, Field(description="Whether anyone may read it.")] = False,
        bibcodes: Annotated[list[str] | None, Field(max_length=MAX_DOCUMENTS, description=_BIBCODES)] = None,
        query: Annotated[str | None, Field(description=_FROM_QUERY)] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_DOCUMENTS, description="With `query`: how many of its results to add.")] = 25,
    ) -> dict[str, Any]:
        """Create a library on the user's SciX account, empty or holding the documents given."""
        documents = [] if bibcodes is None and query is None else self._bibcodes(bibcodes, query, limit)
        created = Library.create(name, description, public, documents, client=self.client)
        return {"created": _library(created)}

    @_tool
    def edit_library(
        self,
        library: Annotated[str, Field(description=_LIBRARY)],
        name: Annotated[str | None, Field(description="A new name.")] = None,
        description: Annotated[str | None, Field(description="A new description.")] = None,
        public: Annotated[bool | None, Field(description="Whether anyone may read it.")] = None,
    ) -> dict[str, Any]:
        """Change a library's name, description or whether it is public. Only what is given changes."""
        found = self._find_library(library)
        found.update(name=name, description=description, public=public)
        return {"library": _library(found)}

    @_tool
    def add_to_library(
        self,
        library: Annotated[str, Field(description=_LIBRARY)],
        bibcodes: Annotated[list[str] | None, Field(max_length=MAX_DOCUMENTS, description=_BIBCODES)] = None,
        query: Annotated[str | None, Field(description=_FROM_QUERY)] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_DOCUMENTS, description="With `query`: how many of its results to add.")] = 25,
    ) -> dict[str, Any]:
        """Add documents to a library: bibcodes, or the results of a search."""
        found = self._find_library(library)
        chosen = self._bibcodes(bibcodes, query, limit)
        added = found.add(chosen)
        return {"added": added, "already_there": len(chosen) - added, "library": _library(found)}

    @_tool
    def remove_from_library(
        self,
        library: Annotated[str, Field(description=_LIBRARY)],
        bibcodes: Annotated[list[str], Field(min_length=1, max_length=MAX_DOCUMENTS, description=_BIBCODES)],
    ) -> dict[str, Any]:
        """Remove documents from a library. The documents themselves are not affected."""
        found = self._find_library(library)
        removed = found.remove(self._bibcodes(bibcodes, None, 0))
        return {"removed": removed, "library": _library(found)}

    @_tool
    def combine_libraries(
        self,
        operation: Annotated[
            Literal["union", "intersection", "difference"],
            Field(
                description="union: documents in any of them; intersection: documents in all of them; "
                "difference: documents in `library` but in none of `others`."
            ),
        ],
        library: Annotated[str, Field(description=_LIBRARY)],
        others: Annotated[list[str], Field(min_length=1, description="The other libraries: ids or exact names.")],
        name: Annotated[str | None, Field(description="The name of the new library (SciX chooses one if not given).")] = None,
        description: Annotated[str | None, Field(description="What the new library is for.")] = None,
        public: Annotated[bool, Field(description="Whether anyone may read the new library.")] = False,
    ) -> dict[str, Any]:
        """Make a new library from two or more: their union, intersection or difference. The originals are kept."""
        first = self._find_library(library)
        rest = [self._find_library(other) for other in others]
        combine = getattr(first, operation)
        created: Library = combine(*rest, name=name, description=description, public=public, client=self.client)
        return {"created": _library(created)}

    @_tool
    def delete_library(
        self,
        library: Annotated[str, Field(description=_LIBRARY)],
    ) -> dict[str, Any]:
        """Delete a library from the user's SciX account, for good. This cannot be undone."""
        found = self._find_library(library)
        deleted = _library(found)
        found.delete()
        return {"deleted": deleted}


def build(client: Client | None = None, *, read_only: bool = False) -> MCPServer:
    """The MCP server with its tools registered.

    :param client: the client to send requests with (default: the default client).
    :param read_only: leave out the tools that create, change or delete libraries.
    """
    tools = Tools(client)
    server = MCPServer(
        "scixplorer",
        title="SciX",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://andycasey.github.io/scixplorer/",
        log_level="WARNING",
    )
    local = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    reads = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    adds = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
    changes = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
    server.add_tool(tools.search, title="Search SciX", annotations=reads)
    server.add_tool(tools.explain_search, title="Explain a search", annotations=local)
    server.add_tool(tools.get_document, title="Get a document", annotations=reads)
    server.add_tool(tools.resolve, title="Resolve a name", annotations=local)
    server.add_tool(tools.export, title="Export references", annotations=reads, structured_output=False)
    server.add_tool(tools.metrics, title="Citation metrics", annotations=reads)
    server.add_tool(tools.list_libraries, title="List libraries", annotations=reads)
    server.add_tool(tools.get_library, title="Get a library", annotations=reads)
    if not read_only:
        server.add_tool(tools.create_library, title="Create a library", annotations=adds)
        server.add_tool(tools.add_to_library, title="Add to a library", annotations=adds)
        server.add_tool(tools.combine_libraries, title="Combine libraries", annotations=adds)
        server.add_tool(tools.edit_library, title="Edit a library", annotations=changes)
        server.add_tool(tools.remove_from_library, title="Remove from a library", annotations=changes)
        server.add_tool(tools.delete_library, title="Delete a library", annotations=changes)
    return server


def serve(client: Client | None = None, *, read_only: bool = False) -> None:
    """Run the server on standard input and output until the client disconnects."""
    build(client, read_only=read_only).run("stdio")
