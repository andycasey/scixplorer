"""The results browser of the ``scix`` command.

:class:`Browser` holds what is on the screen and changes it one key at a time;
it draws itself as lines of text and never touches the terminal, so it can be
driven and read without one. :func:`run` connects it to a terminal.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from ..exceptions import SciXError
from ..models.document import Document
from .interpret import Interpretation, interpret
from .output import (
    SORTS,
    Style,
    abstract_of,
    authors_line,
    facts,
    fit,
    page_url,
    pdf_url,
    reference,
    short_authors,
    title_of,
    wrap,
)
from .session import Session

__all__ = ["Browser", "decode_key", "decode_keys", "run"]

#: How many documents are fetched at a time.
PAGE = 50

_KEYS = "↑↓ move   ⏎ read   o open   f full text   b BibTeX   c cited by   r references   s sort   / search   ? help   q quit"

_HELP = """\
↑ ↓  j k        move                 PgUp PgDn  g G    a page, the first, the last
⏎  →            read the abstract    ←  Esc            back
o               open the SciX page   f                 open the full text
c               papers that cite it  r                 papers it cites
space           mark                 b                 copy BibTeX (of the marked)
y               copy the bibcode     s                 change the order
/               search again         q                 quit"""

_ESCAPES = {
    "[A": "up", "OA": "up", "[B": "down", "OB": "down", "[C": "right", "OC": "right", "[D": "left", "OD": "left",
    "[5~": "pgup", "[6~": "pgdn", "[H": "home", "OH": "home", "[1~": "home", "[F": "end", "OF": "end", "[4~": "end",
}  # fmt: skip

_CONTROLS = {
    "\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\t": "tab",
    "\x03": "ctrl-c", "\x04": "ctrl-d", "\x15": "ctrl-u", "\x17": "ctrl-w",
}  # fmt: skip


_ESCAPE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|O[@-~])?")


def decode_keys(data: bytes) -> list[str]:
    """The keys a terminal sent as ``data``, in order.

    Each is the name of a key (``"up"``, ``"enter"``, ``"esc"``, ``"ctrl-u"``)
    or the text typed; text that arrives together (a paste) stays together.
    Escape sequences that name no key here are left out.
    """
    text = data.decode("utf-8", "replace")
    keys: list[str] = []
    typed = ""
    position = 0
    while position < len(text):
        character = text[position]
        if character.isprintable():
            typed += character
            position += 1
            continue
        if typed:
            keys.append(typed)
            typed = ""
        if character == "\x1b":
            sequence = _ESCAPE.match(text, position)
            assert sequence is not None
            keys.append("esc" if len(sequence[0]) == 1 else _ESCAPES.get(sequence[0][1:], ""))
            position = sequence.end()
        else:
            keys.append(_CONTROLS.get(character, ""))
            position += 1
    if typed:
        keys.append(typed)
    return [key for key in keys if key]


def decode_key(data: bytes) -> str:
    """The first key of :func:`decode_keys`, or ``""`` if there is none."""
    keys = decode_keys(data)
    return keys[0] if keys else ""


def copy_to_clipboard(text: str) -> bool:
    """Put ``text`` on the system clipboard; ``False`` if no clipboard program is installed."""
    commands = (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"], ["clip.exe"])
    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(command, input=text.encode("utf-8"), check=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        return True
    return False


def _bibtex(documents: list[Document], client: Any) -> str:
    from ..services import export

    return export.bibtex(documents, client=client)


@dataclass
class _View:
    """One search and where the reader is in its results."""

    interpretation: Interpretation
    documents: list[Document]
    total: int
    sort: str
    selected: int = 0
    top: int = 0
    marked: set[str] = field(default_factory=set)
    widened_from: Interpretation | None = None


class Browser:
    """A list of results with the selected document's abstract below it.

    :param session: runs the searches.
    :param sort: the order of the results (a key of ``SORTS``); ``None`` lets each search
        choose: the most cited first if it names a year, else the newest.
    :param style: how text is emphasised.
    :param opener: opens a URL (default: the web browser).
    :param copier: copies text to the clipboard, returning whether it could.
    """

    def __init__(
        self,
        session: Session,
        *,
        sort: str | None = None,
        style: Style | None = None,
        opener: Callable[[str], Any] = webbrowser.open,
        copier: Callable[[str], bool] = copy_to_clipboard,
    ) -> None:
        self.session = session
        self.sort = sort or "date"
        self._fixed_sort = sort is not None
        self.style = style or Style()
        self.opener = opener
        self.copier = copier
        self.views: list[_View] = []
        self.mode = "prompt"
        self.buffer = ""
        self.message = ""
        self.scroll = 0
        self.rows = 10
        #: Called just before a request is sent, so that the screen can say so.
        self.on_busy: Callable[[], None] | None = None

    # State.

    @property
    def view(self) -> _View | None:
        return self.views[-1] if self.views else None

    @property
    def document(self) -> Document | None:
        view = self.view
        if view is None or not view.documents:
            return None
        return view.documents[view.selected]

    def _busy(self, message: str) -> None:
        self.message = message
        if self.on_busy is not None:
            self.on_busy()

    def search(self, query: str | Interpretation) -> bool:
        """Run a search and show its results; on an error, say so and keep what is shown."""
        try:
            interpretation = interpret(query) if isinstance(query, str) else query
            if interpretation.expr is None:
                self.message = "Type what to search for."
                return False
            if not self._fixed_sort:
                self.sort = "citations" if any(r.kind == "year" for r in interpretation.readings) else "date"
            self._busy(f"Searching: {interpretation} …")
            found = self.session.find(interpretation, self.sort, limit=PAGE)
        except (SciXError, ValueError) as error:
            self.message = str(error)
            return False
        view = _View(found.interpretation, found.documents, found.total, self.sort)
        if found.widened:
            view.widened_from = interpretation
        self.views.append(view)
        self.mode, self.scroll = "list", 0
        self.message = "" if found.total else "No documents."
        return True

    def _reload(self) -> None:
        """Fetch the current search again in the current order."""
        view = self.view
        if view is None:
            return
        try:
            self._busy(f"Sorting, {SORTS[self.sort][2]} …")
            documents, total = self.session.fetch(view.interpretation, self.sort, limit=PAGE)
        except SciXError as error:
            self.message = str(error)
            return
        self.views[-1] = _View(view.interpretation, documents, total, self.sort, widened_from=view.widened_from)
        self.message = ""

    def _more(self) -> None:
        """Fetch the next documents when the selection nears the end of those loaded."""
        view = self.view
        if view is None or len(view.documents) >= view.total or view.selected < len(view.documents) - 5:
            return
        try:
            self._busy("Loading more …")
            documents, total = self.session.fetch(
                view.interpretation, view.sort, limit=PAGE, offset=len(view.documents)
            )
        except SciXError as error:
            self.message = str(error)
            view.total = len(view.documents)
            return
        view.documents.extend(documents)
        view.total = total if documents else len(view.documents)
        self.message = ""

    def _move(self, step: int) -> None:
        view = self.view
        if view is None or not view.documents:
            return
        view.selected = max(0, min(len(view.documents) - 1, view.selected + step))
        self.scroll = 0
        self._more()

    # Keys.

    def key(self, key: str) -> bool:
        """Act on one key; ``False`` means the reader has quit."""
        if key == "ctrl-c":
            return False
        if self.mode == "prompt":
            return self._prompt_key(key)
        self.message = ""
        if self.mode == "help":
            self.mode = "list"
            return key != "q"
        if key in ("q", "ctrl-d"):
            return False
        if self.mode == "read":
            return self._read_key(key)
        return self._list_key(key)

    def _prompt_key(self, key: str) -> bool:
        if key == "esc" or (key == "ctrl-d" and not self.buffer):
            if not self.views:
                return False
            self.mode, self.message = "list", ""
        elif key == "enter":
            if self.buffer.strip():
                self.search(self.buffer)
        elif key == "backspace":
            self.buffer = self.buffer[:-1]
        elif key == "ctrl-u":
            self.buffer = ""
        elif key == "ctrl-w":
            self.buffer = self.buffer.rstrip().rpartition(" ")[0]
            self.buffer += " " if self.buffer else ""
        elif key and key not in _ESCAPES.values() and key not in _CONTROLS.values():
            self.buffer += key
        return True

    def _read_key(self, key: str) -> bool:
        if key in ("esc", "left", "h", "backspace", "enter"):
            self.mode, self.scroll = "list", 0
        elif key in ("down", "j"):
            self.scroll += 1
        elif key in ("up", "k"):
            self.scroll = max(0, self.scroll - 1)
        elif key in ("pgdn", " "):
            self.scroll += max(1, self.rows - 2)
        elif key == "pgup":
            self.scroll = max(0, self.scroll - max(1, self.rows - 2))
        elif key == "n":
            self._move(1)
        elif key == "p":
            self._move(-1)
        else:
            self._act(key)
        return True

    def _list_key(self, key: str) -> bool:
        view = self.view
        if key in ("down", "j"):
            self._move(1)
        elif key in ("up", "k"):
            self._move(-1)
        elif key == "pgdn":
            self._move(self.rows)
        elif key == "pgup":
            self._move(-self.rows)
        elif key in ("home", "g"):
            self._move(-(10**9))
        elif key in ("end", "G"):
            self._move(10**9)
        elif key in ("enter", "right", "l") and self.document is not None:
            self.mode, self.scroll = "read", 0
        elif key in ("esc", "left", "h", "backspace"):
            if len(self.views) > 1:
                self.views.pop()
                self.sort = self.views[-1].sort
        elif key == " " and view is not None and self.document is not None:
            bibcode = self.document.bibcode
            if bibcode:
                view.marked.symmetric_difference_update({bibcode})
            self._move(1)
        else:
            self._act(key)
        return True

    def _act(self, key: str) -> None:
        """The keys that mean the same in the list and while reading."""
        document, view = self.document, self.view
        if key == "/":
            self.mode = "prompt"
            self.buffer = view.interpretation.text if view is not None else ""
        elif key == "?":
            self.mode = "help"
        elif key == "s":
            names = list(SORTS)
            self.sort = names[(names.index(self.sort) + 1) % len(names)]
            self._fixed_sort = True
            self._reload()
        elif document is None or not document.bibcode:
            return
        elif key == "o":
            url = page_url(document)
            if url is not None:
                self.opener(url)
                self.message = f"Opened {url}"
        elif key == "f":
            url = pdf_url(document)
            if url is None:
                self.message = "SciX lists no full text for this document."
            else:
                self.opener(url)
                self.message = f"Opened {url}"
        elif key == "c":
            self.search(f"citations:{document.bibcode}")
        elif key == "r":
            self.search(f"references:{document.bibcode}")
        elif key == "y":
            self._copy(document.bibcode, f"Copied {document.bibcode}")
        elif key == "b" and view is not None:
            chosen = [each for each in view.documents if each.bibcode in view.marked] or [document]
            try:
                self._busy("Exporting BibTeX …")
                text = _bibtex(chosen, self.session.client)
            except SciXError as error:
                self.message = str(error)
                return
            noun = "document" if len(chosen) == 1 else "documents"
            self._copy(text, f"Copied BibTeX for {len(chosen)} {noun}")

    def _copy(self, text: str, done: str) -> None:
        self.message = done if self.copier(text) else "No clipboard program found (pbcopy, wl-copy, xclip or xsel)."

    # Drawing.

    def render(self, width: int, height: int) -> list[str]:
        """The screen as exactly ``height`` lines of at most ``width`` columns."""
        width, height = max(width, 20), max(height, 6)
        if self.mode == "help":
            body = self._help(width)
        elif self.mode == "read" and self.document is not None:
            body = self._reading(width, height - 3)
        else:
            body = self._list(width, height - 3)
        lines = [*self._header(width), *body][: height - 1]
        lines += [""] * (height - 1 - len(lines))
        if self.mode == "prompt" and self.message:
            lines[-1] = self.style.warn(fit(" " + self.message, width))
        lines.append(self._footer(width))
        return lines

    def _header(self, width: int) -> list[str]:
        style, view = self.style, self.view
        if view is None:
            return [style.bold(" scix"), style.dim(fit(" Search SciX in plain words: Casey 2005 · exoplanets since 2020 · ApJ 608 119", width))]
        position = f"{view.selected + 1 if view.documents else 0}/{view.total} "
        read = fit(f" {view.interpretation}", width - len(position) - 1, pad=True)
        order = f" {SORTS[view.sort][2]}" if view.total > 1 else ""
        if view.marked:
            order += f" · {len(view.marked)} marked"
        if view.widened_from is not None:
            order += f" · nothing for: {view.widened_from}"
        return [style.bold(style.accent(read)) + " " + style.dim(position), style.dim(fit(order, width))]

    def _footer(self, width: int) -> str:
        style = self.style
        if self.mode == "prompt":
            return fit(" search: " + self.buffer, width - 1) + style.selected(" ")
        if self.message:
            return style.warn(fit(" " + self.message, width))
        if self.mode == "read":
            return style.dim(fit(" ↑↓ scroll   n p next, previous   ← back   o open   f full text   b BibTeX   c cited by   r references   q quit", width))
        return style.dim(fit(" " + _KEYS, width))

    def _row(self, document: Document, width: int, marked: bool) -> str:
        data = document._data
        year = str(data.get("year") or "")
        cited = str(data.get("citation_count") or 0)
        who = 16 if width >= 70 else 10
        where = fit(reference(document).split(" ")[0], 6, pad=True, right=True) if width >= 86 else ""
        title = width - 2 - 5 - who - 2 - (len(where) + 2 if where else 0) - 7
        row = (
            ("● " if marked else "  ")
            + fit(year, 4, pad=True)
            + " "
            + fit(short_authors(document), who, pad=True)
            + "  "
            + fit(title_of(document), title, pad=True)
        )
        if where:
            row += "  " + where
        return fit(row + fit(cited, 7, pad=True, right=True), width)

    def _list(self, width: int, height: int) -> list[str]:
        view, style = self.view, self.style
        if view is None or not view.documents:
            return []
        preview = (height - 1) // 2 if height >= 13 else 0
        rows = max(1, height - (preview + 1 if preview else 0))
        self.rows = rows
        view.top = max(min(view.top, view.selected), view.selected - rows + 1, 0)
        lines: list[str] = []
        for index in range(view.top, min(view.top + rows, len(view.documents))):
            document = view.documents[index]
            row = self._row(document, width, document.bibcode in view.marked)
            lines.append(style.selected(row) if index == view.selected else row)
        if preview:
            lines += [""] * (rows - len(lines))
            lines.append(style.dim("─" * width))
            lines.extend(self._details(view.documents[view.selected], width, limit=preview))
        return lines

    def _details(self, document: Document, width: int, *, limit: int | None = None, authors: int = 8) -> list[str]:
        """A document in full: title, authors, where it appeared, abstract; cut to ``limit`` lines."""
        style = self.style
        inner = width - 2
        title = wrap(title_of(document), inner)
        if limit is not None:
            title = title[:2]
        lines = [" " + style.bold(line) for line in title]
        who = authors_line(document, authors)
        lines.extend(" " + line for line in ([fit(who, inner)] if limit is not None else wrap(who, inner)))
        lines.append(" " + style.accent(fit(facts(document), inner)))
        abstract = wrap(abstract_of(document) or "(no abstract)", inner)
        lines.append("")
        if limit is not None and len(lines) + len(abstract) > limit:
            abstract = abstract[: max(0, limit - len(lines))]
            if abstract:
                abstract[-1] = fit(abstract[-1] + " …", inner)
        lines.extend(" " + line for line in abstract)
        return lines if limit is None else lines[:limit]

    def _reading(self, width: int, height: int) -> list[str]:
        document = self.document
        if document is None:
            return []
        self.rows = height
        lines = self._details(document, min(width, 100), authors=40)
        data = document._data
        extra = [f"doi:{doi}" for doi in (data.get("doi") or [])[:1]]
        extra += [each for each in (data.get("identifier") or []) if each.lower().startswith("arxiv:")][:1]
        url = page_url(document)
        lines.append("")
        lines.extend(" " + self.style.dim(fit(each, width - 2)) for each in (*extra, url) if each)
        self.scroll = max(0, min(self.scroll, len(lines) - height))
        return lines[self.scroll : self.scroll + height]

    def _help(self, width: int) -> list[str]:
        style, view = self.style, self.view
        lines: list[str] = []
        if view is not None:
            lines.append(style.bold(fit(" How the search was read", width)))
            typed = max(len(reading.source) for reading in view.interpretation.readings)
            for reading in view.interpretation.readings:
                lines.append(fit(f"   {reading.source:<{typed}}   {reading.kind}: {reading.value}", width))
            try:
                from ..search import to_solr

                lines.append(style.dim(fit("   sent as: " + to_solr(self.session.query(view.interpretation, view.sort)), width)))
            except SciXError:
                pass
            lines.extend(style.warn(line) for note in view.interpretation.notes for line in wrap(" " + note, width))
            lines.append("")
        lines.append(style.bold(fit(" Keys", width)))
        lines.extend(fit("   " + line, width) for line in _HELP.splitlines())
        lines.append("")
        lines.append(style.dim(fit(" The number at the end of each row is how often the document is cited.", width)))
        return lines


# The terminal.


def _read_keys(descriptor: int, timeout: float) -> list[str] | None:
    """The keys typed since the last call, or ``None`` if none arrives within ``timeout`` seconds."""
    import select

    ready, _, _ = select.select([descriptor], [], [], timeout)
    if not ready:
        return None
    try:
        data = os.read(descriptor, 1024)
    except OSError:
        data = b""
    if not data:
        return ["ctrl-c"]  # the terminal went away
    if data.endswith(b"\x1b"):
        # An escape sequence can arrive in pieces; a lone Esc is followed by nothing.
        more, _, _ = select.select([descriptor], [], [], 0.03)
        if more:
            data += os.read(descriptor, 1024)
    return decode_keys(data)


def run(
    browser: Browser,
    interpretation: Interpretation | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Show ``browser`` on the terminal until the reader quits; returns the exit status.

    Starts with the results of ``interpretation``, or with the search prompt
    if there is none. The terminal is put back as it was, whatever happens.
    """
    import termios
    import tty

    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    descriptor = stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    size = (0, 0)

    def draw() -> None:
        nonlocal size
        size = tuple(shutil.get_terminal_size())  # type: ignore[assignment]
        lines = browser.render(*size)
        stdout.write("\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K\x1b[J")
        stdout.flush()

    try:
        tty.setcbreak(descriptor, termios.TCSANOW)  # not TCSAFLUSH: keep what was typed ahead
        stdout.write("\x1b[?1049h\x1b[?25l")
        browser.on_busy = draw
        if interpretation is not None:
            browser.mode = "list"
            if not browser.search(interpretation):
                browser.mode = "prompt"
                browser.buffer = interpretation.text
        draw()
        alive = True
        while alive:
            keys = _read_keys(descriptor, 0.25)
            if keys is None:
                if tuple(shutil.get_terminal_size()) != size:
                    draw()
                continue
            for key in keys:
                alive = alive and browser.key(key)
            draw()
    except KeyboardInterrupt:
        pass
    finally:
        browser.on_busy = None
        stdout.write("\x1b[?25h\x1b[?1049l")
        stdout.flush()
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
    return 0
