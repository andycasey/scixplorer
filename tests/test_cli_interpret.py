"""How the ``scix`` command reads a search typed in plain words."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

import scixplorer.client
from scixplorer import data, search
from scixplorer.cli import Interpretation, Reading, interpret
from scixplorer.cli.interpret import _singular

from cli_fakes import install_thesaurus

TODAY = date(2026, 11, 18)


@pytest.fixture(autouse=True)
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An empty cache of its own, and no API client: reading a search never sends a request."""

    def fail() -> None:
        raise AssertionError("reading a search must not need a client")

    monkeypatch.setattr(scixplorer.client, "get_default_client", fail)
    monkeypatch.setenv("SCIX_CACHE_DIR", str(tmp_path / "cache"))
    data.clear("uat")
    yield
    data.clear("uat")


@pytest.fixture
def thesaurus(offline: None) -> None:
    install_thesaurus()


def read(query: str | list[str]) -> tuple[str, str]:
    """``(how it was read, the Solr query)`` of a search."""
    interpretation = interpret(query, today=TODAY)
    assert interpretation.expr is not None
    return str(interpretation), search.to_solr(interpretation.expr)


def solr(query: str | list[str]) -> str:
    return read(query)[1]


# The three searches the command was designed around.


def test_a_capitalised_word_is_an_author_and_a_four_digit_number_a_year(thesaurus: None) -> None:
    assert read(["Casey", "2005"]) == ("author Casey · year 2005", 'author:"Casey" AND year:2005')


def test_a_word_naming_a_uat_concept_is_that_concept(thesaurus: None) -> None:
    assert read(["exoplanets", "2026"]) == ("concept Exoplanets (UAT 498) · year 2026", "uat_id:498 AND year:2026")


def test_a_journal_is_followed_by_its_volume_and_page(thesaurus: None) -> None:
    assert read(["ApJ", "608", "119"]) == (
        "journal The Astrophysical Journal (ApJ) · volume 608 · page 119",
        'bibstem:"ApJ" AND volume:"608" AND page:"119"',
    )


# Authors.


@pytest.mark.parametrize(
    "query, expected",
    [
        ("Casey", 'author:"Casey"'),
        ("Casey, A.", 'author:"Casey, A."'),
        ("Casey, A. R. 2019", 'author:"Casey, A. R." AND year:2019'),
        ("Casey, Andrew 2019", 'author:"Casey, Andrew" AND year:2019'),
        ("Casey, Andrew R. 2019", 'author:"Casey, Andrew R." AND year:2019'),
        ("A. R. Casey 2019", 'author:"Casey, A. R." AND year:2019'),
        ("A Casey", 'author:"Casey, A"'),
        ("Casey Schlaufman 2015", 'author:"Casey" AND author:"Schlaufman" AND year:2015'),
        ("Casey & Schlaufman 2015", 'author:"Casey" AND author:"Schlaufman" AND year:2015'),
        ("Casey and Schlaufman 2015", 'author:"Casey" AND author:"Schlaufman" AND year:2015'),
        ("Casey, Schlaufman, K. C.", 'author:"Casey" AND author:"Schlaufman, K. C."'),
        ("van der Marel 2002", 'author:"van der Marel" AND year:2002'),
        ("O'Brien", '''author:"O'Brien"'''),
        ("Staveley-Smith", 'author:"Staveley-Smith"'),
        ("Müller 2019", 'author:"Müller" AND year:2019'),
        ("Casey et al. 2019", 'first_author:"Casey" AND year:2019'),
        ("Casey, A. R. et al 2019", 'first_author:"Casey, A. R." AND year:2019'),
        ("Casey Ji et al 2019", 'first_author:"Casey" AND author:"Ji" AND year:2019'),
        ("^Casey 2019", 'first_author:"Casey" AND year:2019'),
        ("^Casey, A. 2019", 'first_author:"Casey, A." AND year:2019'),
        ('"Casey, A" 2019', 'author:"Casey, A" AND year:2019'),
        ('"^Casey, A" 2019', 'first_author:"Casey, A" AND year:2019'),
        ('"^Casey" 2019', 'first_author:"Casey" AND year:2019'),
        ('"^" 2019', "year:2019"),
    ],
)
def test_authors(thesaurus: None, query: str, expected: str) -> None:
    assert solr(query) == expected


def test_a_pasted_reference_is_read_piece_by_piece(thesaurus: None) -> None:
    how, q = read("Casey, A. R., Hawkins, K., Hogg, D. W. 2017, ApJ, 840, 59")
    assert how == (
        "author Casey, A. R. · author Hawkins, K. · author Hogg, D. W. · year 2017 · "
        "journal The Astrophysical Journal (ApJ) · volume 840 · page 59"
    )
    assert q == (
        'author:"Casey, A. R." AND author:"Hawkins, K." AND author:"Hogg, D. W." AND year:2017 '
        'AND bibstem:"ApJ" AND volume:"840" AND page:"59"'
    )
    assert solr("Casey ApJ 880 125 (2019)") == 'author:"Casey" AND bibstem:"ApJ" AND volume:"880" AND page:"125" AND year:2019'


def test_initials_after_a_surname_stop_at_a_journal_reference(thesaurus: None) -> None:
    assert solr("Jarrett, AJ 125 525") == 'author:"Jarrett" AND bibstem:"AJ" AND volume:"125" AND page:"525"'
    assert solr("Jarrett, AJ") == 'author:"Jarrett, AJ"'


# Years.


@pytest.mark.parametrize(
    "query, expected",
    [
        ("2005", "year:2005"),
        ("2005-2010", "year:[2005 TO 2010]"),
        ("2005..2010", "year:[2005 TO 2010]"),
        ("2005–2010", "year:[2005 TO 2010]"),
        ("2015-", "year:[2015 TO *]"),
        ("2015+", "year:[2015 TO *]"),
        ("..2015", "year:[* TO 2015]"),
        ("since 2020", "year:[2020 TO *]"),
        ("from 2020", "year:[2020 TO *]"),
        ("after 2020", "year:[2021 TO *]"),
        ("before 2010", "year:[* TO 2009]"),
        ("until 2010", "year:[* TO 2010]"),
        ("(2019)", "year:2019"),
        ("year:2019", "year:2019"),
        ("y:2019-2021", "year:[2019 TO 2021]"),
        ("year:[2019 TO *]", "year:[2019 TO *]"),
        ("year:2019-", "year:[2019 TO *]"),
        ("year:recent", "year:recent"),
    ],
)
def test_years(thesaurus: None, query: str, expected: str) -> None:
    assert solr(query) == expected


def test_a_number_is_a_year_only_if_it_could_be_one(thesaurus: None) -> None:
    assert solr("1499") == 'abs:"1499"'
    assert solr("2028") == "year:2028"  # in press, two years ahead
    assert solr("2029") == 'abs:"2029"'
    assert solr("2010-2005") == 'abs:"2010-2005"'
    assert solr("12345") == 'abs:"12345"'


# Journals.


@pytest.mark.parametrize(
    "query, expected",
    [
        ("MNRAS 2020", 'bibstem:"MNRAS" AND year:2020'),
        ("mnras 495 1", 'bibstem:"MNRAS" AND volume:"495" AND page:"1"'),
        ("ApJ 900", 'bibstem:"ApJ" AND volume:"900"'),
        ("ApJ 880 L12", 'bibstem:"ApJ" AND volume:"880" AND page:"L12"'),
        ("ApJ 2004 608 119", 'bibstem:"ApJ" AND year:2004 AND volume:"608" AND page:"119"'),
        ("ApJ 608 119 2004", 'bibstem:"ApJ" AND volume:"608" AND page:"119" AND year:2004'),
        ("ApJ, 880, 125", 'bibstem:"ApJ" AND volume:"880" AND page:"125"'),
        ("araa 2020", 'bibstem:"ARA&A" AND year:2020'),
        ("A+A 2020", 'bibstem:"A&A" AND year:2020'),
        ("AA 2020", 'bibstem:"A&A" AND year:2020'),
        ("ApJL 2020", 'bibstem:"ApJL" AND year:2020'),
        ("Nature 2020", 'bibstem:"Natur" AND year:2020'),
        ("Science", 'bibstem:"Sci"'),
        ("AJ 2020", 'bibstem:"AJ" AND year:2020'),
        ("AJ 125 525", 'bibstem:"AJ" AND volume:"125" AND page:"525"'),
        ("Icar 2020", 'bibstem:"Icar" AND year:2020'),
        ("Astrophysical Journal 900 12", 'bibstem:"ApJ" AND volume:"900" AND page:"12"'),
        ("The Astronomical Journal 2020", 'bibstem:"AJ" AND year:2020'),
        ("Monthly Notices of the Royal Astronomical Society", 'bibstem:"MNRAS"'),
        ("j:ApJ", 'bibstem:"ApJ"'),
        ("j:mnras", 'bibstem:"MNRAS"'),
        ('j:"Astronomy and Astrophysics"', 'bibstem:"A&A"'),
        ("journal:NotInTheList", 'bibstem:"NotInTheList"'),
        ('"Annual Review of Astronomy and Astrophysics" 2020', 'bibstem:"ARA&A" AND year:2020'),
    ],
)
def test_journals(thesaurus: None, query: str, expected: str) -> None:
    assert solr(query) == expected


def test_an_ordinary_word_is_not_a_journal(thesaurus: None) -> None:
    # "Icar" and "AJ" are abbreviations, but alone they are too like a name to be read as journals.
    assert solr("Icar") == 'author:"Icar"'
    assert solr("nature of dark matter") == '(author:"nature" OR abs:"nature") AND uat_id:353'


def test_a_word_that_is_a_journal_and_a_concept_is_the_journal_only_before_a_volume(thesaurus: None) -> None:
    assert solr("MHD") == "uat_id:1964"
    assert solr("MHD 2020") == "uat_id:1964 AND year:2020"
    assert solr("MHD 12 34") == 'bibstem:"MHD" AND volume:"12" AND page:"34"'
    assert solr("Solar Physics 2020") == "uat_id:1476 AND year:2020"
    assert solr("Solar Physics 295 12") == 'bibstem:"SoPh" AND volume:"295" AND page:"12"'
    assert solr("Galaxies 2021") == "uat_id:573 AND year:2021"


# Concepts.


@pytest.mark.parametrize(
    "query, expected",
    [
        ("exoplanet", "uat_id:498"),
        ("Exoplanets", "uat_id:498"),
        ("extrasolar planets 2020", "uat_id:498 AND year:2020"),
        ("hot jupiters since 2020", "uat_id:753 AND year:[2020 TO *]"),
        ("black hole 2015-2020", "uat_id:162 AND year:[2015 TO 2020]"),
        ("galaxy", "uat_id:573"),
        ("the sun", "uat_id:1693"),
        ("dark matter halos", "uat_id:1880"),
        ("dark matter halo stellar", 'uat_id:1880 AND abs:"stellar"'),
        ("Gaia exoplanets", "uat_id:2360 AND uat_id:498"),
        ("YSOs", "uat_id:1834"),
        ("uat:498", "uat_id:498"),
        ("uat:exoplanets", "uat_id:498"),
        ('concept:"hot jupiters"', "uat_id:753"),
        ("uat:9999", "uat_id:9999"),
        ('uat:"not a concept"', 'uat:"not a concept"'),
    ],
)
def test_concepts(thesaurus: None, query: str, expected: str) -> None:
    assert solr(query) == expected


def test_a_two_letter_synonym_is_not_read_as_a_concept(thesaurus: None) -> None:
    # "DE" is a synonym of Dark energy, and "de" begins many surnames.
    assert solr("de") == 'abs:"de"'
    assert solr("de Silva 2015") == 'author:"de Silva" AND year:2015'


def test_a_quoted_phrase_is_searched_as_words_even_if_it_names_a_concept(thesaurus: None) -> None:
    assert solr('"dark matter" 2020') == 'abs:"dark matter" AND year:2020'
    # Of several command-line words, one holding spaces was quoted in the shell.
    assert solr(["dark matter", "2020"]) == 'abs:"dark matter" AND year:2020'
    # A single command-line word is the whole search.
    assert solr(["dark matter 2020"]) == "uat_id:353 AND year:2020"


def test_without_the_thesaurus_topics_are_words_and_a_note_says_so() -> None:
    interpretation = interpret("exoplanets 2026", today=TODAY)
    assert search.to_solr(interpretation.expr) == '(author:"exoplanets" OR abs:"exoplanets") AND year:2026'
    assert "scix --refresh-data" in interpretation.notes[0]
    assert interpret("Casey 2005", today=TODAY).notes == ()
    assert search.to_solr(interpret("uat:498").expr) == "uat_id:498"


# Words.


def test_a_lower_case_word_outside_the_thesaurus_is_an_author_or_a_word(thesaurus: None) -> None:
    assert read("casey 2005") == ("author or text casey · year 2005", '(author:"casey" OR abs:"casey") AND year:2005')
    # A word the thesaurus uses is a topic, not a name.
    assert read("young halos") == ("text young · text halos", 'abs:"young" AND abs:"halos"')
    assert solr("DR3 SDSS H2O") == 'abs:"DR3" AND abs:"SDSS" AND abs:"H2O"'


def test_stopwords_are_left_out_unless_they_are_all_there_is(thesaurus: None) -> None:
    assert solr("objects of the planets") == 'abs:"objects" AND abs:"planets"'
    assert solr("of the") == 'abs:"of the"'


def test_refereed_is_a_property(thesaurus: None) -> None:
    assert solr("Casey refereed") == 'author:"Casey" AND property:"refereed"'


# Identifiers.


@pytest.mark.parametrize(
    "query, expected",
    [
        ("2005ApJ...608..119C", 'bibcode:"2005ApJ...608..119C"'),
        ("2016PhRvL.116f1102A", 'bibcode:"2016PhRvL.116f1102A"'),
        ("10.3847/1538-4357/ab1b3a", 'doi:"10.3847/1538-4357/ab1b3a"'),
        ("doi:10.3847/1538-4357/ab1b3a", 'doi:"10.3847/1538-4357/ab1b3a"'),
        ("https://doi.org/10.3847/1538-4357/ab1b3a", 'doi:"10.3847/1538-4357/ab1b3a"'),
        ("arXiv:2301.12345", 'arxiv:"2301.12345"'),
        ("2301.12345v2", 'arxiv:"2301.12345"'),
        ("astro-ph/0601001", 'arxiv:"astro-ph/0601001"'),
        ("https://arxiv.org/abs/2301.12345", 'arxiv:"2301.12345"'),
        ("https://arxiv.org/pdf/2301.12345.pdf", 'arxiv:"2301.12345"'),
        ("0000-0002-1825-0097", 'orcid:"0000-0002-1825-0097"'),
        ("https://orcid.org/0000-0002-1825-0097", 'orcid:"0000-0002-1825-0097"'),
        ("orcid:0000-0002-1825-0097", 'orcid:"0000-0002-1825-0097"'),
        ("https://ui.adsabs.harvard.edu/abs/2019ApJ...880..125C/abstract", 'bibcode:"2019ApJ...880..125C"'),
        ("https://scixplorer.org/abs/2019ApJ...880..125C", 'bibcode:"2019ApJ...880..125C"'),
        ("https://scixplorer.org/abs/arXiv:2301.12345/abstract", 'identifier:"arXiv:2301.12345"'),
        ('"2005ApJ...608..119C"', 'bibcode:"2005ApJ...608..119C"'),
    ],
)
def test_identifiers(thesaurus: None, query: str, expected: str) -> None:
    assert solr(query) == expected


def test_a_mistyped_orcid_is_searched_without_a_warning(thesaurus: None, recwarn: pytest.WarningsRecorder) -> None:
    assert solr("0000-0002-1825-0098") == 'orcid:"0000-0002-1825-0098"'
    assert not recwarn.list


# Named fields and operators.


@pytest.mark.parametrize(
    "query, expected",
    [
        ("a:Casey", 'author:"Casey"'),
        ('author:"Casey, A" 2005', 'author:"Casey, A" AND year:2005'),
        ("fa:Casey", 'first_author:"Casey"'),
        ("t:gaia", 'title:"gaia"'),
        ('title:"dark matter"', 'title:"dark matter"'),
        ("abs:spectroscopy", 'abs:"spectroscopy"'),
        ("kw:metallicity", 'keyword:"metallicity"'),
        ("vol:608 p:119", 'volume:"608" AND page:"119"'),
        ("property:refereed Casey", 'property:"refereed" AND author:"Casey"'),
        ("citation_count:100", "citation_count:100"),
        ("citation_count:many", "citation_count:many"),
        ("citations:2019ApJ...880..125C", 'citations(bibcode:"2019ApJ...880..125C")'),
        ("refs:2019ApJ...880..125C", 'references(bibcode:"2019ApJ...880..125C")'),
        ("trending:exoplanets", "trending(uat_id:498)"),
        ('reviews:"hot jupiters"', "reviews(uat_id:753)"),
        ("useful:", 'abs:"useful:"'),
        ("similar:2019ApJ...880..125C", 'similar(bibcode:"2019ApJ...880..125C")'),
        ("unknown:thing", 'abs:"unknown:thing"'),
    ],
)
def test_named_fields_and_operators(thesaurus: None, query: str, expected: str) -> None:
    assert solr(query) == expected


def test_a_command_line_word_with_spaces_keeps_its_field(thesaurus: None) -> None:
    assert solr(["author:Casey, A", "2005"]) == 'author:"Casey, A" AND year:2005'
    assert solr(["trending:hot jupiters", "2020"]) == "trending(uat_id:753) AND year:2020"


def test_solr_syntax_is_sent_as_it_is(thesaurus: None) -> None:
    for query in (
        'author:"Casey, A" AND year:[2000 TO 2010]',
        "exoplanets OR planets",
        "citations(bibcode:2019ApJ...880..125C)",
        "title:exoplan*",
    ):
        how, q = read(query)
        assert (how, q) == (f"query {query}", query)
    # Upper-case words inside quotes are a phrase, not operators.
    assert solr('"AND OR NOT"') == 'abs:"AND OR NOT"'


# The interpretation itself.


def test_each_reading_says_what_was_typed_and_what_it_means(thesaurus: None) -> None:
    interpretation = interpret("Casey et al. 2019 ApJ 880 125 exoplanets", today=TODAY)
    assert [(reading.kind, reading.source, reading.value) for reading in interpretation.readings] == [
        ("first author", "Casey et al.", "Casey"),
        ("year", "2019", "2019"),
        ("journal", "ApJ", "The Astrophysical Journal (ApJ)"),
        ("volume", "880", "880"),
        ("page", "125", "125"),
        ("concept", "exoplanets", "Exoplanets (UAT 498)"),
    ]
    assert interpretation.text == "Casey et al. 2019 ApJ 880 125 exoplanets"
    assert all(isinstance(reading, Reading) for reading in interpretation.readings)


def test_an_empty_search_has_no_readings(thesaurus: None) -> None:
    for query in ("", "   ", [], [" "]):
        interpretation = interpret(query)
        assert isinstance(interpretation, Interpretation)
        assert interpretation.readings == () and interpretation.expr is None and interpretation.relaxed() is None


def test_relaxed_widens_a_surname_and_a_concept_to_words(thesaurus: None) -> None:
    interpretation = interpret("Hubble exoplanets 2020", today=TODAY)
    wider = interpretation.relaxed()
    assert wider is not None
    assert str(wider) == "author or text Hubble · concept or text Exoplanets (UAT 498) · year 2020"
    assert search.to_solr(wider.expr) == (
        '(author:"Hubble" OR abs:"Hubble") AND (uat_id:498 OR abs:"exoplanets") AND year:2020'
    )
    # Nothing to widen: an author with initials, a year, a journal.
    assert interpret("Casey, A. 2019 ApJ", today=TODAY).relaxed() is None


def test_the_indexes_follow_the_data(thesaurus: None) -> None:
    assert solr("exoplanets") == "uat_id:498"
    data.clear("uat")
    assert solr("exoplanets") == 'author:"exoplanets" OR abs:"exoplanets"'
    install_thesaurus()
    assert solr("exoplanets") == "uat_id:498"


def test_singular() -> None:
    assert [_singular(word) for word in ("galaxies", "black holes", "stars", "mass", "gas", "nucleus", "axis")] == [
        "galaxy", "black hole", "star", "mass", "gas", "nucleus", "axis",
    ]  # fmt: skip
