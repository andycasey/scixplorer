"""The ``scix`` command: search SciX from the terminal in plain words.

::

    scix Kewley 2008           # an author and a year
    scix exoplanets 2026       # a UAT concept and a year
    scix ApJ 608 119           # a journal, a volume and a page

:func:`~scixplorer.cli.interpret.interpret` reads the words; in a terminal the
results open in a browser you move through with the keyboard, and anywhere else
(a pipe, or with ``--plain``) they are printed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from .. import __version__
from .._config import config
from ..exceptions import SciXError
from ..models.document import Document
from ..query import Sender
from .interpret import Interpretation, Reading, interpret
from .output import SORTS, Style, explanation, found_line, listing, page_url
from .session import Found, Session

__all__ = ["Interpretation", "Reading", "interpret", "main"]

#: The ``--format`` values sent to the export service.
EXPORT_FORMATS = (
    "bibtex", "bibtexabs", "aastex", "ads", "endnote", "medlars", "procite", "refworks", "ris",
    "icarus", "mnras", "soph", "ieee", "dcxml", "refxml", "refabsxml", "votable",
)  # fmt: skip

FORMATS = ("list", "bibcodes", "json", *EXPORT_FORMATS)

_EPILOG = """\
how words are read:
  Kewley 2008             an author and a year (capitalised words are surnames)
  Kewley et al. 2019      the first author; so is ^Kewley
  "Kewley, L." 2015-2020  an author with initials, and a range of years
  exoplanets since 2020   a Unified Astronomy Thesaurus concept
  ApJ 608 119             a journal, a volume and a page
  2019ApJ...880...16K     a bibcode; a DOI, arXiv or ORCID identifier works too
  "dark matter halo"      a phrase in the title, abstract or keywords
  title:gaia a:Kewley     a named field (any document field, or a: t: j: y:)
  citations:<bibcode>     what cites it; also references: trending: reviews:

Run `scix --explain <words>` to see how a search is read without sending it.
"""


def _limit(value: str) -> int | None:
    if value.lower() == "all":
        return None
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number or 'all', not {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scix",
        description="Search SciX (the successor to NASA/ADS) in plain words.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("words", nargs="*", metavar="WORD", help="what to search for")
    parser.add_argument("-n", "--limit", type=_limit, default=10, metavar="N", help="print N documents, or 'all' (default: 10)")
    parser.add_argument("-s", "--sort", choices=tuple(SORTS), default=None, help="the order of the results (default: citations if the search names a year, else date)")
    parser.add_argument("-r", "--refereed", action="store_true", help="only refereed documents")
    parser.add_argument("-c", "--collection", metavar="NAME", help="only one collection, such as astronomy")
    parser.add_argument("-q", "--query", action="store_true", help="send the words as a Solr query, without reading them")
    parser.add_argument("-x", "--explain", action="store_true", help="show how the words are read, and send nothing")
    parser.add_argument("-p", "--plain", action="store_true", help="print the results instead of browsing them")
    parser.add_argument("-o", "--open", action="store_true", help="open the first result in your web browser")
    parser.add_argument("-f", "--format", choices=FORMATS, default=None, metavar="FORMAT", help="print the results as: " + ", ".join(FORMATS))
    parser.add_argument("--bibtex", dest="format", action="store_const", const="bibtex", help="the same as --format bibtex")
    parser.add_argument("--json", dest="format", action="store_const", const="json", help="the same as --format json")
    parser.add_argument("--bibcodes", dest="format", action="store_const", const="bibcodes", help="the same as --format bibcodes")
    parser.add_argument("--refresh-data", action="store_true", help="download the journal list, affiliations, thesaurus and gazetteer, then stop")
    parser.add_argument("--version", action="version", version=f"scix {__version__}")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client: Sender | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the ``scix`` command and return its exit status.

    0 means documents were found (or nothing had to be searched), 1 that none
    matched, and 2 that the search could not be run.

    :param argv: the arguments (default: those of the process).
    :param client: the client to search with (default: the default client).
    """
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    parser = _parser()
    args = parser.parse_intermixed_args(argv)
    try:
        return _run(args, parser, client, stdin, stdout, stderr)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except SciXError as error:
        print(f"scix: {error}", file=stderr)
        return 2


def _is_terminal(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _style(stream: TextIO) -> Style:
    plain = "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"
    return Style(_is_terminal(stream) and not plain)


def _refresh(stdout: TextIO) -> int:
    from .. import data

    results = data.refresh()
    for result in results:
        print(f"{result.name}: {'updated' if result.ok else 'failed'}{' - ' + result.message if result.message else ''}", file=stdout)
    return 0 if all(result.ok for result in results) else 2


def _read(args: argparse.Namespace) -> Interpretation:
    words: list[str] = args.words
    if args.query:
        text = " ".join(words)
        interpretation = Interpretation(text, (Reading("query", text, text, Document.raw(text)),))
    else:
        interpretation = interpret(words)
    if args.refereed:
        refereed = Reading("property", "--refereed", "refereed", Document.property == "refereed")
        interpretation = Interpretation(interpretation.text, (*interpretation.readings, refereed), interpretation.notes)
    return interpretation


def default_sort(interpretation: Interpretation) -> str:
    """The order to show a search in when none is asked for.

    Newest first, unless the search names a year: then the dates say little,
    and the most cited documents of those years come first.
    """
    return "citations" if any(reading.kind == "year" for reading in interpretation.readings) else "date"


def _run(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    client: Sender | None,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.refresh_data:
        return _refresh(stdout)
    if args.collection:
        config.collection = args.collection
    session = Session(client)
    browse = (
        not (args.plain or args.open or args.explain or args.format)
        and _is_terminal(stdin)
        and _is_terminal(stdout)
        and _can_browse()
    )
    if not any(word.strip() for word in args.words):
        if browse:
            return _browse(session, None, args.sort, stdin, stdout)
        parser.print_usage(stderr)
        print("scix: say what to search for, such as: scix Kewley 2008", file=stderr)
        return 2
    interpretation = _read(args)
    sort: str = args.sort or default_sort(interpretation)
    if args.explain:
        from ..search import to_solr

        q = to_solr(session.query(interpretation, sort))
        print("\n".join(explanation(interpretation, q, sort, _style(stdout))), file=stdout)
        return 0
    if browse:
        return _browse(session, interpretation, args.sort, stdin, stdout)
    return _print(session, interpretation, sort, args, stdout, stderr)


def _can_browse() -> bool:
    try:
        import termios  # noqa: F401
    except ImportError:  # Windows
        return False
    return os.environ.get("TERM") != "dumb"


def _browse(
    session: Session, interpretation: Interpretation | None, sort: str | None, stdin: TextIO, stdout: TextIO
) -> int:
    from . import tui

    browser = tui.Browser(session, sort=sort, style=Style("NO_COLOR" not in os.environ))
    return tui.run(browser, interpretation, stdin=stdin, stdout=stdout)


def _print(
    session: Session, interpretation: Interpretation, sort: str, args: argparse.Namespace, stdout: TextIO, stderr: TextIO
) -> int:
    output = args.format or "list"
    # The list carries its own heading; any other format keeps standard output for the data alone.
    heading = stdout if output == "list" else stderr
    style = _style(heading)
    # Say how the search was read before sending it: the answer takes a moment to arrive.
    print(style.accent(str(interpretation)), file=heading, flush=True)
    for note in interpretation.notes:
        print(style.warn(note), file=stderr)
    found = session.find(interpretation, sort, limit=1 if args.open and not args.format else args.limit)
    if found.widened:
        print(style.warn(f"Nothing matched, so searched for: {found.interpretation}"), file=heading)
    if not found.total:
        print("No documents.", file=heading)
        return 1
    print(style.dim(found_line(found.total, len(found.documents), sort)), file=heading)
    if args.open:
        _open(found, heading)
        if not args.format:
            return 0
    if output == "list":
        width = min(shutil.get_terminal_size((88, 24)).columns, 100)
        print("\n" + "\n".join(listing(found.documents, width=width, style=_style(stdout))), file=stdout)
    elif output == "bibcodes":
        print("\n".join(document.bibcode for document in found.documents if document.bibcode), file=stdout)
    elif output == "json":
        rows: list[dict[str, Any]] = [document.to_dict() for document in found.documents]
        print(json.dumps(rows, indent=2, ensure_ascii=False), file=stdout)
    else:
        from ..services import export

        text: str = getattr(export, output)(found.documents, client=session.client)
        print(text.rstrip("\n"), file=stdout)
    return 0


def _open(found: Found, heading: TextIO) -> None:
    import webbrowser

    url = page_url(found.documents[0])
    if url is not None:
        print(url, file=heading)
        webbrowser.open(url)
