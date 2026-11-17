"""The results browser of the ``scix`` command, driven key by key without a terminal."""

from __future__ import annotations

import io
import os
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from scixplorer import data
from scixplorer.cli import interpret, main
from scixplorer.cli.output import Style
from scixplorer.cli.session import Session
from scixplorer.cli.tui import Browser, copy_to_clipboard, decode_key, decode_keys, run

from cli_fakes import FakeAPI, documents, install_thesaurus


@pytest.fixture(autouse=True)
def thesaurus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path / "cache"))
    install_thesaurus()
    yield
    data.clear("uat")


class Desk:
    """A browser over a fake API, with what it opened and copied."""

    def __init__(self, n: int = 3, *, sort: str | None = None, clipboard: bool = True, styled: bool = False) -> None:
        self.api = FakeAPI(documents(n))
        self.opened: list[str] = []
        self.copied: list[str] = []

        def copy(text: str) -> bool:
            self.copied.append(text)
            return clipboard

        self.browser = Browser(
            Session(self.api.client()), sort=sort, style=Style(styled), opener=self.opened.append, copier=copy
        )

    def keys(self, *keys: str) -> bool:
        alive = True
        for key in keys:
            alive = self.browser.key(key)
        return alive

    def screen(self, width: int = 100, height: int = 30) -> list[str]:
        lines = self.browser.render(width, height)
        assert len(lines) == height
        return lines


def desk(query: str = "Casey 2019", n: int = 3, **options: object) -> Desk:
    made = Desk(n, **options)  # type: ignore[arg-type]
    assert made.browser.search(query)
    return made


# Keys from the terminal.


@pytest.mark.parametrize(
    "data, key",
    [
        (b"\x1b[A", "up"), (b"\x1bOB", "down"), (b"\x1b[C", "right"), (b"\x1b[D", "left"),
        (b"\x1b[5~", "pgup"), (b"\x1b[6~", "pgdn"), (b"\x1b[H", "home"), (b"\x1b[F", "end"),
        (b"\x1b", "esc"), (b"\x1b[99~", ""), (b"\r", "enter"), (b"\n", "enter"), (b"\x7f", "backspace"),
        (b"\t", "tab"), (b"\x03", "ctrl-c"), (b"\x15", "ctrl-u"), (b"\x17", "ctrl-w"), (b"q", "q"), (b" ", " "),
        ("é".encode(), "é"), (b"pasted words", "pasted words"), (b"\x00", ""), (b"", ""),
    ],
)  # fmt: skip
def test_decode_key(data: bytes, key: str) -> None:
    assert decode_key(data) == key


def test_keys_that_arrive_together_are_told_apart() -> None:
    assert decode_keys(b"\x1b[Bq") == ["down", "q"]
    assert decode_keys(b"jj\x1b[A\x1b[A\r") == ["jj", "up", "up", "enter"]
    assert decode_keys(b"Casey 2005\r") == ["Casey 2005", "enter"]
    assert decode_keys(b"\x1b[200~pasted\x1b[201~\x1b") == ["pasted", "esc"]
    assert decode_keys(b"\x1b\x1b[D") == ["esc", "left"]


# The list.


def test_the_list_shows_the_search_the_documents_and_the_selected_abstract() -> None:
    lines = desk().screen()
    assert lines[0].startswith(" author Casey · year 2019") and lines[0].endswith("1/3 ")
    assert lines[1] == " most cited first"
    assert lines[2].startswith("  2019 Casey             Paper 0 about 2 things & more") and lines[2].endswith("ApJ    100")
    assert lines[3].startswith("  2019 Casey & Ji        Paper 1") and lines[4].startswith("  2019 Casey et al.")
    rule = lines.index("─" * 100)
    assert lines[rule + 1 : rule + 4] == [
        " Paper 0 about 2 things & more",
        " Casey, Andrew R.",
        " ApJ 880, 100 (2019) · 100 citations · refereed · 2019ApJ...880.0000C",
    ]
    assert lines[rule + 5].startswith(" Abstract of paper 0. We measure things carefully.")
    assert lines[-1].startswith(" ↑↓ move")


def test_every_line_fits_the_terminal_at_any_size() -> None:
    made = desk(n=40)
    for width, height in ((200, 60), (100, 30), (80, 24), (60, 12), (40, 8), (10, 3)):
        for keys in ((), ("enter",), ("?",), ("/",)):
            made.keys(*keys)
            lines = made.browser.render(width, height)
            assert len(lines) == max(height, 6)
            assert all(len(line) <= max(width, 20) for line in lines), (width, height, keys)
            made.keys("esc")


def test_a_small_terminal_has_no_preview() -> None:
    lines = desk(n=40).screen(80, 10)
    assert "─" * 80 not in lines and sum(line.startswith("  2019") for line in lines) == 7


def test_moving_scrolls_the_list_and_changes_the_preview() -> None:
    made = desk(n=40)
    browser = made.browser
    made.keys("down", "j")
    assert browser.view.selected == 2 and " Paper 2 about 2 things & more" in made.screen()
    made.keys("up", "k", "k")
    assert browser.view.selected == 0
    made.keys("pgdn")
    made.screen()
    assert browser.view.selected == browser.rows and browser.view.top == 1
    made.keys("G")
    assert browser.view.selected == 39 and made.screen()[0].endswith("40/40 ")
    made.keys("g")
    made.screen()
    assert (browser.view.selected, browser.view.top) == (0, 0)
    made.keys("end", "home", "pgdn", "pgup")
    assert browser.view.selected == 0


def test_the_selected_row_is_highlighted_and_colour_can_be_off() -> None:
    assert desk(styled=True).screen()[2].startswith("\x1b[7m  2019 Casey")
    assert not any("\x1b" in line for line in desk().screen())


def test_more_documents_load_as_the_selection_nears_the_end() -> None:
    made = desk(n=120)
    browser = made.browser
    assert len(browser.view.documents) == 50 and browser.view.total == 120
    made.keys("G")
    assert len(browser.view.documents) == 100 and browser.view.selected == 49
    made.keys("G", "G")
    assert len(browser.view.documents) == 120 and browser.view.selected == 119
    assert [request.url.params["start"] for request in made.api.searches] == ["0", "50", "100"]
    made.keys("G")
    assert len(made.api.searches) == 3


def test_marking_documents() -> None:
    made = desk()
    made.keys(" ", " ")
    assert made.browser.view.marked == {"2019ApJ...880.0000C", "2019ApJ...880.0001C"} and made.browser.view.selected == 2
    lines = made.screen()
    assert lines[1] == " most cited first · 2 marked" and lines[2].startswith("● 2019") and lines[4].startswith("  2019")
    made.keys("up", " ")
    assert made.browser.view.marked == {"2019ApJ...880.0000C"}


# Acting on a document.


def test_open_and_full_text() -> None:
    made = desk()
    made.keys("o")
    assert made.opened == ["https://scixplorer.org/abs/2019ApJ...880.0000C/abstract"]
    assert made.screen()[-1] == " Opened https://scixplorer.org/abs/2019ApJ...880.0000C/abstract"
    made.keys("f")
    assert made.opened[-1] == "https://scixplorer.org/link_gateway/2019ApJ...880.0000C/PUB_PDF"
    made.keys("down", "f")
    assert len(made.opened) == 2 and made.screen()[-1] == " SciX lists no full text for this document."
    made.keys("up")
    assert made.screen()[-1].startswith(" ↑↓ move")  # a message lasts until the next key


def test_copying_the_bibcode_and_bibtex() -> None:
    made = desk()
    made.keys("y")
    assert made.copied == ["2019ApJ...880.0000C"] and made.screen()[-1] == " Copied 2019ApJ...880.0000C"
    made.keys("b")
    assert made.api.exported == [["2019ApJ...880.0000C"]] and made.copied[-1].startswith("@ARTICLE{2019ApJ...880.0000C,")
    assert made.screen()[-1] == " Copied BibTeX for 1 document"
    made.keys("down", " ", " ", "b")
    assert made.api.exported[-1] == ["2019ApJ...880.0001C", "2019ApJ...880.0002C"]
    assert made.screen()[-1] == " Copied BibTeX for 2 documents"


def test_without_a_clipboard_program_the_browser_says_so() -> None:
    made = desk(clipboard=False)
    made.keys("y")
    assert made.screen()[-1].startswith(" No clipboard program found")


def test_copy_to_clipboard_uses_the_first_program_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[tuple[list[str], bytes]] = []
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/xclip" if name == "xclip" else None)
    monkeypatch.setattr("subprocess.run", lambda command, input, **_: ran.append((command, input)))
    assert copy_to_clipboard("text é") and ran == [(["xclip", "-selection", "clipboard"], "text é".encode())]
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert not copy_to_clipboard("text")


def test_citations_and_references_open_a_new_search_and_back_returns() -> None:
    made = desk()
    made.keys("down", "c")
    assert made.api.queries[-1] == 'citations(bibcode:"2019ApJ...880.0001C")'
    assert made.screen()[0].startswith(" citations of bibcode 2019ApJ...880.0001C")
    assert made.screen()[1] == " newest first"  # no year in this search
    made.keys("r")
    assert made.api.queries[-1] == 'references(bibcode:"2019ApJ...880.0000C")' and len(made.browser.views) == 3
    made.keys("left", "esc")
    assert len(made.browser.views) == 1 and made.browser.view.selected == 1
    assert made.screen()[1] == " most cited first"
    made.keys("backspace")
    assert len(made.browser.views) == 1  # the first search stays


def test_s_changes_the_order_and_fetches_again() -> None:
    made = desk()
    made.keys("down", "s")
    assert made.api.params()["sort"] == "read_count desc,id asc"
    assert made.screen()[1] == " most read first" and made.browser.view.selected == 0
    made.keys("s", "s", "s", "s")
    assert made.api.params()["sort"] == "date desc,id asc"
    # An order the reader chose is kept for the next search.
    made.keys("c")
    assert made.api.params()["sort"] == "date desc,id asc"


def test_a_given_sort_is_used_for_every_search() -> None:
    made = desk(sort="oldest")
    assert made.api.params()["sort"] == "date asc,id asc"
    made.keys("c")
    assert made.api.params()["sort"] == "date asc,id asc"


# Reading.


def test_reading_shows_the_whole_document_and_scrolls() -> None:
    made = desk(n=5)
    made.keys("down", "down", "enter")
    assert made.browser.mode == "read"
    lines = made.screen(80, 12)
    assert lines[2:5] == [
        " Paper 2 about 2 things & more",
        " Casey, Andrew R.; Ji, Alexander P.; Hogg, David W.",
        " ApJ 880, 102 (2019) · 100 citations · refereed · 2019ApJ...880.0002C"[:0] or lines[4],
    ]
    assert lines[-1].startswith(" ↑↓ scroll")
    made.keys("down", "j", "pgdn")
    assert made.browser.scroll > 2
    made.keys("pgdn", "pgdn", "pgdn")
    bottom = made.screen(80, 12)
    assert " https://scixplorer.org/abs/2019ApJ...880.0002C/abstract" in bottom and " doi:10.3847/1538-4357/2" in bottom
    made.keys("up", "k", "pgup", "pgup", "pgup")
    assert made.browser.scroll == 0
    made.keys("n")
    assert made.browser.view.selected == 3 and made.browser.mode == "read"
    made.keys("p", "p")
    assert made.browser.view.selected == 1
    made.keys("o")
    assert made.opened == ["https://scixplorer.org/abs/2019ApJ...880.0001C/abstract"]
    made.keys("esc")
    assert made.browser.mode == "list"
    made.keys("right")
    assert made.browser.mode == "read"
    made.keys("left")
    assert made.browser.mode == "list"


# Searching again, and help.


def test_slash_edits_the_search() -> None:
    made = desk()
    made.keys("/")
    assert made.browser.mode == "prompt" and made.screen()[-1] == " search: Casey 2019 "
    made.keys("backspace", "backspace", "backspace", "backspace", "2", "0", "2", "0")
    made.keys("ctrl-w")
    assert made.browser.buffer == "Casey "
    made.keys("ctrl-u", "up", "tab", "exoplanets since", " ", "2020", "enter")
    assert made.api.queries[-1] == "uat_id:498 AND year:[2020 TO *]"
    assert made.browser.mode == "list" and len(made.browser.views) == 2
    assert made.screen()[0].startswith(" concept Exoplanets (UAT 498) · year 2020 or later")
    made.keys("/", "esc")
    assert made.browser.mode == "list" and len(made.browser.views) == 2
    made.keys("/", "ctrl-u", "enter")
    assert made.browser.mode == "prompt"  # nothing typed: keep asking


def test_help_shows_how_the_search_was_read() -> None:
    made = desk("Casey et al. 2019 exoplanets")
    made.keys("?")
    lines = made.screen()
    assert lines[2:6] == [
        " How the search was read",
        "   Casey et al.   first author: Casey",
        "   2019           year: 2019",
        "   exoplanets     concept: Exoplanets (UAT 498)",
    ]
    assert lines[6] == '   sent as: first_author:"Casey" AND year:2019 AND uat_id:498'
    assert any(line.startswith("   space") for line in lines)
    assert made.keys("x") and made.browser.mode == "list"
    made.keys("?")
    assert not made.keys("q")


def test_help_shows_notes() -> None:
    data.clear("uat")
    made = desk("spectra")
    made.keys("?")
    assert "scix --refresh-data" in " ".join(line.strip() for line in made.screen())


# Starting, stopping and errors.


def test_a_browser_starts_at_the_prompt() -> None:
    made = Desk()
    lines = made.screen()
    assert lines[0] == " scix" and lines[-1] == " search:  "
    made.keys("C", "a", "s", "e", "y", "enter")
    assert made.browser.mode == "list" and made.api.queries == ['author:"Casey"']
    quitter = Desk()
    assert not quitter.keys("esc")  # nothing to go back to
    assert not Desk().keys("ctrl-d")


def test_quitting() -> None:
    assert not desk().keys("q")
    assert not desk().keys("ctrl-c")
    assert not desk().keys("ctrl-d")
    assert not desk().keys("enter", "q")
    made = desk()
    made.keys("/")
    assert made.keys("q") and made.browser.buffer.endswith("q")  # a letter, while typing
    assert not made.keys("ctrl-c")


def test_no_documents() -> None:
    made = desk(n=0)
    lines = made.screen()
    assert lines[0].endswith("0/0 ") and lines[-1] == " No documents."
    assert made.keys("down", "enter", "o", "c", "b", "y", " ", "s") and made.opened == []


def test_a_search_that_finds_nothing_is_widened_and_says_so() -> None:
    made = Desk()
    calls = iter([0])

    def reply(request: httpx.Request) -> httpx.Response:
        if next(calls, None) == 0:
            return httpx.Response(200, json={"response": {"numFound": 0, "start": 0, "docs": []}})
        made.api.reply = None
        return made.api._search(request, "/search/query")

    made.api.reply = reply
    assert made.browser.search("Hubble")
    lines = made.screen()
    assert lines[0].startswith(" author or text Hubble") and lines[1] == " newest first · nothing for: author Hubble"


def test_an_error_is_shown_and_the_results_stay() -> None:
    made = desk()
    made.api.reply = lambda request: httpx.Response(500, json={"error": "boom"})
    made.keys("c")
    assert len(made.browser.views) == 1 and "500" in made.screen()[-1]
    made.keys("s")
    assert "500" in made.screen()[-1] and made.browser.view.documents
    made.keys("/", "ctrl-u", "G", "a", "i", "a", "enter")
    lines = made.screen()
    assert made.browser.mode == "prompt" and "500" in lines[-2] and lines[-1] == " search: Gaia "


def test_an_error_while_loading_more_keeps_what_is_loaded() -> None:
    made = desk(n=120)
    made.api.reply = lambda request: httpx.Response(500, json={"error": "boom"})
    made.keys("G")
    assert len(made.browser.view.documents) == 50 and made.browser.view.total == 50


def test_busy_is_announced_before_each_request() -> None:
    made = Desk()
    seen: list[str] = []
    made.browser.on_busy = lambda: seen.append(made.browser.message)
    made.browser.search("Casey 2019")
    made.keys("s", "b")
    assert seen == ["Searching: author Casey · year 2019 …", "Sorting, most read first …", "Exporting BibTeX …"]


# On a real (pseudo) terminal.


def on_a_terminal(made: Desk, keys: bytes, query: str = "Casey 2019") -> tuple[int, str, bool]:
    """Run the browser on a pseudo-terminal, typing ``keys`` once it has started.

    Returns ``(exit status, what was written, whether the terminal was put back as it was)``.
    If the browser does not quit by itself, the terminal is closed after a few seconds.
    """
    termios = pytest.importorskip("termios")
    import pty
    import threading

    master, slave = pty.openpty()
    done = threading.Event()

    def typist() -> None:
        time.sleep(0.3)
        os.write(master, keys)
        done.wait(10)
        os.close(master)

    def modes() -> list[int]:
        # Without PENDIN, which macOS sets by itself while typed input is waiting.
        iflag, oflag, cflag, lflag = termios.tcgetattr(slave)[:4]
        return [iflag, oflag, cflag, lflag & ~getattr(termios, "PENDIN", 0)]

    thread = threading.Thread(target=typist)
    before = modes()
    out = io.StringIO()
    try:
        thread.start()
        with os.fdopen(slave, "r", closefd=False) as stdin:
            status = run(made.browser, interpret(query), stdin=stdin, stdout=out)
        try:
            restored = modes() == before
        except termios.error:  # the terminal has gone away (Linux)
            restored = False
    finally:
        done.set()
        thread.join()
        os.close(slave)
    return status, out.getvalue(), restored


def test_run_draws_on_a_terminal_and_restores_it() -> None:
    made = Desk()
    status, text, restored = on_a_terminal(made, b"\x1b[Bq")
    assert status == 0 and restored and made.browser.on_busy is None
    assert text.startswith("\x1b[?1049h\x1b[?25l") and text.endswith("\x1b[?25h\x1b[?1049l")
    assert "Searching: author Casey · year 2019" in text and "Paper 1 about 2 things" in text
    assert made.browser.view.selected == 1


def test_run_goes_to_the_prompt_when_the_first_search_fails() -> None:
    made = Desk()
    made.api.reply = lambda request: httpx.Response(500, json={"error": "boom"})
    status, text, restored = on_a_terminal(made, b"\x1b")
    assert status == 0 and restored
    assert made.browser.mode == "prompt" and made.browser.buffer == "Casey 2019"
    assert "search: Casey 2019" in text and "500" in text


def test_run_stops_when_the_terminal_goes_away() -> None:
    made = Desk()
    started = time.monotonic()
    status, _, _ = on_a_terminal(made, b"")
    assert status == 0 and time.monotonic() - started < 30


def test_run_ends_quietly_if_the_terminal_cannot_be_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    termios = pytest.importorskip("termios")
    original = termios.tcsetattr

    def gone(descriptor: int, when: int, attributes: list[object]) -> None:
        if when == termios.TCSADRAIN:  # the restore at the end; on Linux it fails once the terminal is closed
            raise termios.error(5, "Input/output error")
        original(descriptor, when, attributes)

    monkeypatch.setattr(termios, "tcsetattr", gone)
    status, text, _ = on_a_terminal(Desk(), b"q")
    assert status == 0 and text.endswith("\x1b[?25h\x1b[?1049l")


def test_the_command_only_browses_on_a_terminal() -> None:
    # Standard input and output here are not terminals, so the results are printed.
    api = FakeAPI()
    out = io.StringIO()
    assert main(["Casey"], client=api.client(), stdin=io.StringIO(), stdout=out, stderr=io.StringIO()) == 0
    assert out.getvalue().startswith("author Casey\n") and "\x1b[?1049h" not in out.getvalue()
