"""An offline stand-in for the SciX API, used to run the examples in the documentation.

``DocsAPI`` answers the requests the client makes (search, libraries, export,
resolver, visualisation networks, citation helper) from a small set of sample
records, over ``httpx.MockTransport``. Unlike ``search_fakes.FakeSearch`` it
evaluates the Solr query string, so that an example searching for a first
author or a year range returns the records a reader would expect. The query
is parsed back into the client's own expression nodes and evaluated with
:mod:`scixplorer.evaluate`, which follows ADS's documented matching rules
(curated synonyms known only to the server are not applied).

The sample records are real papers, but their counts, affiliations, keywords,
vocabulary assignments and the papers on which an ORCID iD appears are
illustrative, not current SciX data.

``UAT_JSON`` and ``PLANETARY_FEATURES_CSV`` are small extracts of the real
vocabularies, so that the identifiers shown in the documentation are true:
the Unified Astronomy Thesaurus (American Astronomical Society, CC BY-SA 3.0,
https://github.com/astrothesaurus/UAT) and the Gazetteer of Planetary
Nomenclature (IAU WGPSN / USGS, public domain).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from search_fakes import PREFIX

from scixplorer import evaluate
from scixplorer.client import Client, service_for
from scixplorer.data import DATASETS
from scixplorer.expr import And, Expr, In, Like, Match, Not, Or, Range
from scixplorer.fields import DateTimeField, Field, FloatField, IntegerField
from scixplorer.models.document import Document

__all__ = [
    "DOCS_TOKEN",
    "PLANETARY_FEATURES_CSV",
    "SAMPLE_DOCUMENTS",
    "SAMPLE_LIBRARIES",
    "UAT_JSON",
    "DocsAPI",
    "UnsupportedByFake",
    "refresh_transport",
]

#: The token the example client sends (never a real one).
DOCS_TOKEN = "docs-example-token"


# Sample records, keyed by Solr field name as the search API returns them.


def _normalised(name: str) -> str:
    """A name as SciX's ``author_norm`` writes it: ``"Lastname, F"``."""
    last, _, given = name.partition(",")
    return f"{last}, {given.strip()[:1]}" if given.strip() else last


def _record(**fields: Any) -> dict[str, Any]:
    fields.setdefault("database", ["astronomy"])
    fields.setdefault("doctype", "article")
    fields["first_author"] = fields["author"][0]
    fields["author_norm"] = [_normalised(name) for name in fields["author"]]
    fields["author_count"] = len(fields["author"])
    fields["bibstem"] = [fields["bibcode"][4:9].strip(".")]
    count = len(fields["author"])
    for name in ("email", "orcid_pub", "orcid_user", "orcid_other"):
        fields.setdefault(name, ["-"] * count)
    return fields


SAMPLE_DOCUMENTS: list[dict[str, Any]] = [
    _record(
        bibcode="2015ApJ...808...16N",
        id="10978346",
        title=["The Cannon: A data-driven approach to Stellar Label Determination from Spectra"],
        author=["Ness, M.", "Hogg, David W.", "Rix, H. -W.", "Ho, Anna. Y. Q.", "Zasowski, G."],
        aff=[
            "Max-Planck-Institut für Astronomie, Königstuhl 17, D-69117 Heidelberg, Germany",
            "Center for Cosmology and Particle Physics, New York University, New York, NY 10003, USA; "
            "Max-Planck-Institut für Astronomie, Königstuhl 17, D-69117 Heidelberg, Germany",
            "Max-Planck-Institut für Astronomie, Königstuhl 17, D-69117 Heidelberg, Germany",
            "California Institute of Technology, Pasadena, CA 91125, USA",
            "Department of Physics and Astronomy, Johns Hopkins University, Baltimore, MD 21218, USA",
        ],
        aff_id=["A01570", "A00951; A01570", "A01570", "A01093", "A00142"],
        orcid_pub=["0000-0001-5082-6693", "0000-0003-2866-9403", "-", "-", "-"],
        year=2015,
        pubdate="2015-08-00",
        pub="The Astrophysical Journal",
        volume="808",
        page=["16"],
        doi=["10.1088/0004-637X/808/1/16"],
        keyword=["methods: data analysis", "methods: statistical", "stars: fundamental parameters", "surveys"],
        property=["ARTICLE", "REFEREED", "EPRINT_OPENACCESS"],
        citation_count=480,
        read_count=95,
        reference=["2013PASP..125..306F"],
        citation=["2016arXiv160303040C"],
        entry_date="2015-07-29T00:00:00Z",
    ),
    _record(
        bibcode="2016arXiv160303040C",
        id="11416591",
        title=["The Cannon 2: A data-driven model of stellar spectra for detailed chemical abundance analyses"],
        author=["Casey, Andrew R.", "Hogg, David W.", "Ness, Melissa", "Rix, Hans-Walter", "Ho, Anna Y. Q.", "Gilmore, Gerry"],
        aff=[
            "Institute of Astronomy, University of Cambridge, Madingley Road, Cambridge CB3 0HA, UK",
            "Center for Cosmology and Particle Physics, New York University, New York, NY 10003, USA",
            "Max-Planck-Institut für Astronomie, Königstuhl 17, D-69117 Heidelberg, Germany",
            "Max-Planck-Institut für Astronomie, Königstuhl 17, D-69117 Heidelberg, Germany",
            "California Institute of Technology, Pasadena, CA 91125, USA",
            "Institute of Astronomy, University of Cambridge, Madingley Road, Cambridge CB3 0HA, UK",
        ],
        aff_id=["A00319", "A00951", "A01570", "A01570", "A01093", "A00319"],
        orcid_user=["-", "0000-0003-2866-9403", "-", "-", "-", "-"],
        year=2016,
        pubdate="2016-03-00",
        pub="arXiv e-prints",
        doctype="eprint",
        identifier=["arXiv:1603.03040", "2016arXiv160303040C"],
        keyword=["Astrophysics - Solar and Stellar Astrophysics", "Astrophysics - Astrophysics of Galaxies"],
        property=["EPRINT_OPENACCESS", "NOT REFEREED", "OPENACCESS"],
        citation_count=120,
        read_count=40,
        reference=["2015ApJ...808...16N", "2013PASP..125..306F"],
        entry_date="2016-03-11T00:00:00Z",
    ),
    _record(
        bibcode="1998AJ....116.1009R",
        id="1640582",
        title=["Observational Evidence from Supernovae for an Accelerating Universe and a Cosmological Constant"],
        author=[
            "Riess, Adam G.", "Filippenko, Alexei V.", "Challis, Peter", "Clocchiatti, Alejandro",
            "Diercks, Alan", "Garnavich, Peter M.", "Gilliland, Ron L.", "Hogan, Craig J.", "Jha, Saurabh",
            "Kirshner, Robert P.", "Leibundgut, B.", "Phillips, M. M.", "Reiss, David", "Schmidt, Brian P.",
            "Schommer, Robert A.", "Smith, R. Chris", "Spyromilio, J.", "Stubbs, Christopher",
            "Suntzeff, Nicholas B.", "Tonry, John",
        ],
        aff=[
            "Department of Astronomy, University of California, Berkeley, CA 94720-3411",
            "Department of Astronomy, University of California, Berkeley, CA 94720-3411",
            "Harvard-Smithsonian Center for Astrophysics, 60 Garden Street, Cambridge, MA 02138",
            "-", "-",
            "Harvard-Smithsonian Center for Astrophysics, 60 Garden Street, Cambridge, MA 02138",
            "Space Telescope Science Institute, 3700 San Martin Drive, Baltimore, MD 21218",
            "-",
            "Harvard-Smithsonian Center for Astrophysics, 60 Garden Street, Cambridge, MA 02138",
            "Harvard-Smithsonian Center for Astrophysics, 60 Garden Street, Cambridge, MA 02138",
            "-", "-", "-",
            "Mount Stromlo and Siding Spring Observatories, Private Bag, Weston Creek, ACT 2611, Australia",
            "-", "-", "-", "-", "-", "-",
        ],
        aff_id=[
            "A11188", "A11188", "A01400", "-", "-", "A01400", "A03671", "-", "A01400", "A01400",
            "-", "-", "-", "A00343", "-", "-", "-", "-", "-", "-",
        ],
        year=1998,
        pubdate="1998-09-00",
        pub="The Astronomical Journal",
        volume="116",
        page=["1009"],
        doi=["10.1086/300499"],
        keyword=["COSMOLOGY: OBSERVATIONS", "COSMOLOGY: DISTANCE SCALE", "GALAXIES: DISTANCES AND REDSHIFTS", "STARS: SUPERNOVAE: GENERAL"],
        property=["ARTICLE", "REFEREED"],
        citation_count=15200,
        read_count=310,
        entry_date="1998-09-01T00:00:00Z",
    ),
    _record(
        bibcode="1995Natur.378..355M",
        id="1406395",
        title=["A Jupiter-mass companion to a solar-type star"],
        author=["Mayor, Michel", "Queloz, Didier"],
        aff=["Geneva Observatory, 51 Chemin des Maillettes, CH-1290 Sauverny, Switzerland"] * 2,
        aff_id=["A03006", "A03006"],
        year=1995,
        pubdate="1995-11-00",
        pub="Nature",
        volume="378",
        page=["355"],
        doi=["10.1038/378355a0"],
        keyword=["Planets", "Stars"],
        property=["ARTICLE", "REFEREED"],
        uat_id=[498, 753, 1332],
        uat=["exoplanets", "hot jupiters", "radial velocity"],
        citation_count=4300,
        read_count=150,
        entry_date="1995-11-01T00:00:00Z",
    ),
    _record(
        bibcode="2000ApJ...529L..45C",
        id="1450122",
        title=["Detection of Planetary Transits Across a Sun-like Star"],
        author=["Charbonneau, David", "Brown, Timothy M.", "Latham, David W.", "Mayor, Michel"],
        aff=[
            "Harvard-Smithsonian Center for Astrophysics, 60 Garden Street, Cambridge, MA 02138",
            "High Altitude Observatory, National Center for Atmospheric Research, Boulder, CO 80307",
            "Harvard-Smithsonian Center for Astrophysics, 60 Garden Street, Cambridge, MA 02138",
            "Observatoire de Genève, 51 Chemin des Maillettes, 1290 Sauverny, Switzerland",
        ],
        aff_id=["A01400", "-", "A01400", "A03006"],
        year=2000,
        pubdate="2000-01-00",
        pub="The Astrophysical Journal",
        volume="529",
        page=["L45"],
        doi=["10.1086/312457"],
        keyword=["STARS: PLANETARY SYSTEMS", "TECHNIQUES: PHOTOMETRIC"],
        property=["ARTICLE", "REFEREED"],
        uat_id=[498, 753, 1711],
        uat=["exoplanets", "hot jupiters", "transits"],
        citation_count=1500,
        read_count=60,
        entry_date="2000-01-20T00:00:00Z",
    ),
    _record(
        bibcode="2013PASP..125..306F",
        id="9767213",
        title=["emcee: The MCMC Hammer"],
        author=["Foreman-Mackey, Daniel", "Hogg, David W.", "Lang, Dustin", "Goodman, Jonathan"],
        aff=[
            "Center for Cosmology and Particle Physics, New York University, New York, NY 10003, USA",
            "Center for Cosmology and Particle Physics, New York University, New York, NY 10003, USA",
            "-",
            "Courant Institute of Mathematical Sciences, New York University, New York, NY 10012, USA",
        ],
        aff_id=["A00951", "A00951", "-", "A00954"],
        orcid_pub=["-", "0000-0003-2866-9403", "-", "-"],
        year=2013,
        pubdate="2013-03-00",
        pub="Publications of the Astronomical Society of the Pacific",
        volume="125",
        page=["306"],
        doi=["10.1086/670067"],
        keyword=["Physics - Computational Physics", "Astrophysics - Instrumentation and Methods for Astrophysics"],
        property=["ARTICLE", "REFEREED", "EPRINT_OPENACCESS"],
        citation_count=9800,
        read_count=420,
        citation=["2015ApJ...808...16N", "2016arXiv160303040C"],
        entry_date="2013-03-01T00:00:00Z",
    ),
    _record(
        bibcode="2010GeoRL..3704201M",
        id="8813411",
        title=["Paleoclimate of Mars as captured by the stratigraphic record in Gale Crater"],
        author=["Milliken, R. E.", "Grotzinger, J. P.", "Thomson, B. J."],
        aff=[
            "Jet Propulsion Laboratory, California Institute of Technology, Pasadena, California, USA",
            "Division of Geological and Planetary Sciences, California Institute of Technology, Pasadena, California, USA",
            "Applied Physics Laboratory, Johns Hopkins University, Laurel, Maryland, USA",
        ],
        aff_id=["A01099", "A01095", "A00143"],
        year=2010,
        pubdate="2010-02-00",
        pub="Geophysical Research Letters",
        volume="37",
        page=["L04201"],
        doi=["10.1029/2009GL041870"],
        keyword=["Planetary Sciences: Solid Surface Planets: Erosion and weathering", "Mars"],
        property=["ARTICLE", "REFEREED"],
        database=["astronomy", "earthscience"],
        planetary_feature_id=[2071],
        planetary_feature=["Gale"],
        citation_count=310,
        read_count=25,
        entry_date="2010-02-25T00:00:00Z",
    ),
]

#: Libraries known to the fake: id -> metadata and bibcodes. The first is the caller's own.
SAMPLE_LIBRARIES: dict[str, dict[str, Any]] = {
    "Rk3bq7xQTgS0vVZ2mN8cWw": {
        "name": "Data-driven spectroscopy",
        "description": "Papers on The Cannon",
        "public": False,
        "owner": "example_user",
        "permission": "owner",
        "num_users": 1,
        "date_created": "2023-05-02T09:14:00.000000",
        "date_last_modified": "2024-01-17T16:40:00.000000",
        "documents": ["2015ApJ...808...16N", "2016arXiv160303040C"],
    },
    "Z3x9QhVnS2uYtLm4KpWb7g": {
        "name": "Exoplanet discoveries",
        "description": "The first detections",
        "public": True,
        "owner": "example_user",
        "permission": "owner",
        "num_users": 1,
        "date_created": "2022-11-08T12:00:00.000000",
        "date_last_modified": "2022-11-08T12:00:00.000000",
        "documents": ["1995Natur.378..355M", "2000ApJ...529L..45C"],
    },
}


# Vocabulary extracts (see the module docstring for their sources and licences).


def _concept(number: int, name: str, broader: Iterable[int] = (), alt: Iterable[str] = (), deprecated: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "uri": f"http://astrothesaurus.org/uat/{number}",
        "name": name,
        "broader": [{"uri": f"http://astrothesaurus.org/uat/{n}"} for n in broader] or None,
        "altNames": list(alt) or None,
    }
    if deprecated:
        item["status"] = "deprecated"
    return item


UAT_JSON = json.dumps(
    [
        _concept(486, "Exoplanet astronomy"),
        _concept(489, "Exoplanet detection methods", [486], ["Exoplanet detection", "Extrasolar planet detection", "Extrasolar planetary system detection"]),
        _concept(495, "Exoplanet structure", [486]),
        _concept(487, "Exoplanet atmospheres", [495]),
        _concept(498, "Exoplanets", [486], ["Extrasolar planets", "Planets", "Planets (Extrasolar)"]),
        _concept(695, "Habitable planets", [498]),
        _concept(753, "Hot Jupiters", [498], ["Epistellar jovians", "Pegasean planets", "Pegasids", "Roaster planets"]),
        _concept(1063, "Mini Neptunes", [498]),
        _concept(1655, "Super Earths", [498], ["Super-Earths"]),
        _concept(1332, "Radial velocity", [80, 489], ["Astrometric radial velocity", "Radial-velocity method", "Spectroscopic radial velocity", "Radial velocity methods"]),
        _concept(1711, "Transits", [1677], ["Astronomical transits"]),
        _concept(1529, "Solar system astronomy", alt=["Planetary astronomy"]),
        _concept(1255, "Planetary science", [1529], ["Planetology"]),
        _concept(1575, "Stephan's quintet", deprecated=True),
    ]
)

PLANETARY_FEATURES_CSV = """\
Feature ID,Clean          Feature Name,Target,Feature Type,Approval          Status,Approval Date
16115,Aarna,Mars,"Crater, craters",Approved,"Dec 16, 2022"
1297,Copernicus,Mars,"Crater, craters",Approved,1973
1296,Copernicus,Moon,"Crater, craters",Approved,1935
8421,Copernicus A,Moon,Satellite Feature,Approved,2006
2071,Gale,Mars,"Crater, craters",Approved,1991
2072,Galen,Moon,"Crater, craters",Approved,1973
14300,Jezero,Mars,"Crater, craters",Approved,"Jul 25, 2007"
4453,Olympus Mons,Mars,"Mons, montes",Approved,1973
6163,Tycho,Moon,"Crater, craters",Approved,1935
6288,Valles Marineris,Mars,"Vallis, valles",Approved,1973
"""


# A parser for the Solr queries the client sends (and simple raw ones), into expression nodes.


class UnsupportedByFake(Exception):
    """The query uses something this offline stand-in does not evaluate."""


#: Search-only fields, as the stored fields whose words they search.
_VIRTUAL = {
    "abs": ("abstract", "title", "keyword"),
    "full": ("abstract", "title", "keyword", "body", "ack"),
    "affil": ("aff", "aff_canonical", "aff_abbrev", "aff_id"),
    "orcid": ("orcid_pub", "orcid_user", "orcid_other"),
    "collection": ("database",),
    "arxiv": ("identifier",),
    "all": ("author_norm", "alternate_title", "bibcode", "doi", "identifier"),
    "inst": ("institution",),
}

_BY_SOLR_NAME: dict[str, Field[Any, Any]] = {field.solr_name: field for field in Document._fields.values()}

_NOW = re.compile(r"NOW(?:-(\d+)DAYS)?$")


class _Pos(Expr):
    """``pos(inner, start, end)``: ``inner`` holds within those (1-indexed) values of its field."""

    __slots__ = ("inner", "start", "end")

    def __init__(self, inner: Expr, start: int, end: int) -> None:
        self.inner, self.start, self.end = inner, start, end


class _Operator(Expr):
    """A second-order operator or ``docs(library/<id>)``, evaluated over the whole sample."""

    __slots__ = ("name", "args")

    def __init__(self, name: str, args: tuple[Any, ...]) -> None:
        self.name, self.args = name, args


class _Parser:
    """Recursive descent over the query: OR of ANDs of optionally negated clauses."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def parse(self) -> Expr:
        expr = self.disjunction()
        self.space()
        if self.i != len(self.text):
            raise UnsupportedByFake(f"cannot parse {self.text[self.i:]!r} in {self.text!r}")
        return expr

    def space(self) -> None:
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def keyword(self, word: str) -> bool:
        self.space()
        end = self.i + len(word)
        if self.text[self.i : end] == word and (end == len(self.text) or self.text[end] in " ("):
            self.i = end
            return True
        return False

    def at_end_of_group(self) -> bool:
        self.space()
        return self.i >= len(self.text) or self.text[self.i] in "),"

    def disjunction(self) -> Expr:
        parts = [self.conjunction()]
        while self.keyword("OR"):
            parts.append(self.conjunction())
        return parts[0] if len(parts) == 1 else Or(tuple(parts))

    def conjunction(self) -> Expr:
        parts = [self.unary()]
        while True:
            if self.keyword("AND"):
                parts.append(self.unary())
            elif not self.at_end_of_group() and not self.text.startswith("OR ", self.i):
                parts.append(self.unary())
            else:
                break
        return parts[0] if len(parts) == 1 else And(tuple(parts))

    def unary(self) -> Expr:
        self.space()
        if self.text.startswith("-", self.i) or self.keyword("NOT"):
            if self.text.startswith("-", self.i):
                self.i += 1
            return Not(self.primary())
        return self.primary()

    def primary(self) -> Expr:
        self.space()
        text = self.text
        if text.startswith("(", self.i):
            self.i += 1
            expr = self.disjunction()
            self.expect(")")
            return expr
        if text.startswith("*:*", self.i):
            self.i += 3
            return And(())
        match = re.compile(r"(=?)([a-z_]+)([:(])").match(text, self.i)
        if match is None:
            # A bare word or phrase: ADS searches it in the default fields.
            value, quoted = self.value_text()
            return self.leaf("abs", value, quoted, exact=False)
        self.i = match.end()
        exact, name, kind = match.group(1) == "=", match.group(2), match.group(3)
        if kind == "(":
            return self.function(name)
        return self.clause(name, exact)

    def expect(self, char: str) -> None:
        self.space()
        if not self.text.startswith(char, self.i):
            raise UnsupportedByFake(f"expected {char!r} at {self.text[self.i:]!r} in {self.text!r}")
        self.i += 1

    def function(self, name: str) -> Expr:
        if name == "docs":
            end = self.text.index(")", self.i)
            target = self.text[self.i : end].strip()
            self.i = end + 1
            if not target.startswith("library/"):
                raise UnsupportedByFake(f"docs({target})")
            return _Operator("docs", (target.removeprefix("library/"),))
        args: list[Any] = []
        while True:
            self.space()
            number = re.compile(r"\d+(?=\s*[,)])").match(self.text, self.i)
            order = re.compile(r"([a-z_]+) (asc|desc)(?=\s*\))").match(self.text, self.i)
            if number is not None:
                args.append(int(number.group()))
                self.i = number.end()
            elif order is not None:
                args.append((order.group(1), order.group(2) == "desc"))
                self.i = order.end()
            else:
                args.append(self.disjunction())
            self.space()
            if self.text.startswith(",", self.i):
                self.i += 1
                continue
            self.expect(")")
            break
        if name == "pos":
            start = args[1]
            end = args[2] if len(args) > 2 else start
            return _Pos(args[0], start, end)
        return _Operator(name, tuple(args))

    def value_text(self) -> tuple[str, bool]:
        """The next value: a quoted phrase (unescaped) or a bare term (escapes kept)."""
        self.space()
        text = self.text
        if text.startswith('"', self.i):
            j = self.i + 1
            out = []
            while text[j] != '"':
                if text[j] == "\\":
                    out.append(text[j : j + 2])
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            self.i = j + 1
            return "".join(out), True
        j = self.i
        while j < len(text) and not text[j].isspace() and text[j] not in "),]":
            j += 2 if text[j] == "\\" else 1
        value = text[self.i : j]
        self.i = j
        return value, False

    def clause(self, name: str, exact: bool) -> Expr:
        self.space()
        text = self.text
        if text.startswith("[", self.i):
            self.i += 1
            lo, lo_quoted = self.value_text()
            if not self.keyword("TO"):
                raise UnsupportedByFake(f"expected TO in {text!r}")
            hi, hi_quoted = self.value_text()
            self.expect("]")
            return self.range(name, lo, lo_quoted, hi, hi_quoted)
        if text.startswith("(", self.i):
            self.i += 1
            values = []
            while True:
                values.append(self.value_text())
                if self.keyword("OR"):
                    continue
                self.expect(")")
                break
            return Or(tuple(self.leaf(name, value, quoted, exact) for value, quoted in values))
        value, quoted = self.value_text()
        return self.leaf(name, value, quoted, exact)

    def leaf(self, name: str, value: str, quoted: bool, exact: bool) -> Expr:
        pattern = _pattern(value, quoted)
        if name in _VIRTUAL:
            return Or(tuple(_leaf(_BY_SOLR_NAME[stored], value, quoted, exact, pattern) for stored in _VIRTUAL[name]))
        return _leaf(_field(name), value, quoted, exact, pattern)

    def range(self, name: str, lo: str, lo_quoted: bool, hi: str, hi_quoted: bool) -> Expr:
        field = _field(name)
        bounds = [None if raw == "*" and not quoted else _value(field, _unescape(raw, quoted)) for raw, quoted in ((lo, lo_quoted), (hi, hi_quoted))]
        return Range(field, bounds[0], bounds[1])


def _field(solr_name: str) -> Field[Any, Any]:
    try:
        return _BY_SOLR_NAME[solr_name]
    except KeyError:
        raise UnsupportedByFake(f"unknown field {solr_name!r}") from None


def _unescape(value: str, quoted: bool) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _pattern(value: str, quoted: bool) -> str | None:
    """The client's wildcard pattern for a Solr value with unescaped ``*`` or ``?``, else ``None``."""
    out, wild = [], False
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\\" + nxt if nxt in "*?\\" else nxt)
            i += 2
            continue
        if char in "*?":
            wild = True
        out.append(char)
        i += 1
    return "".join(out) if wild else None


def _value(field: Field[Any, Any], text: str) -> Any:
    if isinstance(field, IntegerField):
        return int(text)
    if isinstance(field, FloatField):
        return float(text)
    if isinstance(field, DateTimeField):
        now = _NOW.match(text)
        if now is not None:
            return datetime.now(timezone.utc) - timedelta(days=int(now.group(1) or 0))
        return field.to_python(text)
    return text


def _leaf(field: Field[Any, Any], value: str, quoted: bool, exact: bool, pattern: str | None) -> Expr:
    if pattern is not None:
        return Like(field, pattern)
    return Match(field, _value(field, _unescape(value, quoted)), exact)


# Evaluating parsed queries over the sample.


def _document(row: dict[str, Any]) -> Document:
    document = Document._from_row(row)
    for name in Document._fields:
        document._data.setdefault(name, None)
    return document


def _select(expr: Expr, rows: list[dict[str, Any]], api: DocsAPI) -> list[dict[str, Any]]:
    test = _test(expr, api)
    return [row for row in rows if test(row)]


def _test(expr: Expr, api: DocsAPI) -> Callable[[dict[str, Any]], bool]:
    if isinstance(expr, And):
        parts = [_test(op, api) for op in expr.operands]
        return lambda row: all(part(row) for part in parts)
    if isinstance(expr, Or):
        parts = [_test(op, api) for op in expr.operands]
        return lambda row: any(part(row) for part in parts)
    if isinstance(expr, Not):
        inner = _test(expr.operand, api)
        return lambda row: not inner(row)
    if isinstance(expr, _Pos):
        return _positional(expr, api)
    if isinstance(expr, _Operator):
        members = {row["bibcode"] for row in api.operator(expr)}
        return lambda row: row["bibcode"] in members
    check = evaluate.predicate(expr)
    return lambda row: check(_document(_as_indexed(row)))


def _as_indexed(row: dict[str, Any]) -> dict[str, Any]:
    """The row as SciX searches it: an ``aff_id`` entry naming several institutions matches each of them."""
    entries = row.get("aff_id")
    if not entries or not any(";" in entry for entry in entries):
        return row
    return dict(row, aff_id=[part.strip() for entry in entries for part in entry.split(";")])


def _positional(expr: _Pos, api: DocsAPI) -> Callable[[dict[str, Any]], bool]:
    names = {node.field.solr_name for node in _leaves(expr.inner)}
    inner = _test(expr.inner, api)

    def test(row: dict[str, Any]) -> bool:
        window = dict(row)
        for name in names:
            if isinstance(row.get(name), list):
                window[name] = row[name][expr.start - 1 : expr.end]
        return inner(window)

    return test


def _leaves(expr: Expr) -> list[Match | Like | Range | In]:
    if isinstance(expr, (And, Or)):
        return [leaf for op in expr.operands for leaf in _leaves(op)]
    if isinstance(expr, Not):
        return _leaves(expr.operand)
    if isinstance(expr, (Match, Like, Range, In)):
        return [expr]
    return []


def _sort_rows(rows: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    keys = [clause.split() for clause in sort.split(",") if clause.strip()]
    result = list(rows)
    for name, direction in reversed(keys):
        if name == "score":
            continue
        present = [row for row in result if row.get(name) is not None]
        missing = [row for row in result if row.get(name) is None]
        present.sort(key=lambda row: row[name], reverse=direction == "desc")
        result = present + missing
    return result


# The API.


def _json(status: int, body: Any, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


class DocsAPI:
    """The offline API: an ``httpx.MockTransport`` handler holding the sample records and libraries.

    Every request is recorded in ``requests``. A request the stand-in does not
    understand is answered with HTTP 599, which the client reports as an
    ``APIError``, so an example that strays outside it fails loudly.
    """

    def __init__(self) -> None:
        self.docs = [dict(doc) for doc in SAMPLE_DOCUMENTS]
        self.libraries = {key: dict(value, documents=list(value["documents"])) for key, value in SAMPLE_LIBRARIES.items()}
        self.requests: list[httpx.Request] = []
        self._created = 0
        self._used: dict[str, int] = {}

    def client(self) -> Client:
        transport = httpx.MockTransport(self)
        return Client(token=DOCS_TOKEN, transport=transport, async_transport=transport, sleep=lambda _: None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(PREFIX)
        service = service_for(path)
        self._used[service] = self._used.get(service, 0) + 1
        reset = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": str(5000 - self._used[service]),
            "X-RateLimit-Reset": str(int(reset.timestamp())),
        }
        try:
            response = self._route(request, path)
        except UnsupportedByFake as exc:
            response = _json(599, {"error": f"not supported by the offline docs API: {exc}"})
        response.headers.update(headers)
        return response

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(request.content) if request.content and request.headers.get("content-type") == "application/json" else None
        if path in ("/search/query", "/search/bigquery"):
            return self._search(request, path)
        if path.startswith("/biblib/"):
            return self._biblib(request.method, path.removeprefix("/biblib/"), request, body)
        if path.startswith("/export/"):
            return self._export(path.removeprefix("/export/"), body)
        if path.startswith("/resolver/"):
            return self._resolver(path.removeprefix("/resolver/"))
        if path in ("/vis/author-network", "/vis/paper-network"):
            return _json(200, {"data": {"root": {"name": [], "children": []}, "bibcode_dict": {b: {} for b in body["bibcodes"]}}})
        if path == "/citation_helper":
            return self._citation_helper(body["bibcodes"])
        raise UnsupportedByFake(f"{request.method} {path}")

    # Search.

    def operator(self, node: _Operator) -> list[dict[str, Any]]:
        """The sample records an operator selects."""
        if node.name == "docs":
            library = self.libraries.get(node.args[0])
            members = set(library["documents"]) if library else set()
            return [row for row in self.docs if row["bibcode"] in members]
        if node.name in ("citations", "references"):
            inner = {row["bibcode"]: row for row in _select(node.args[0], self.docs, self)}
            if node.name == "citations":
                return [row for row in self.docs if inner.keys() & set(row.get("reference", ()))]
            cited = {b for row in inner.values() for b in row.get("reference", ())}
            return [row for row in self.docs if row["bibcode"] in cited]
        if node.name == "topn":
            n, expr = node.args[0], node.args[1]
            rows = _select(expr, self.docs, self)
            if len(node.args) > 2:
                name, descending = node.args[2]
                rows = _sort_rows(rows, f"{name} {'desc' if descending else 'asc'}")
            return rows[:n]
        raise UnsupportedByFake(f"the {node.name}() operator")

    def _search(self, request: httpx.Request, path: str) -> httpx.Response:
        params = request.url.params
        rows = _select(_Parser(params["q"]).parse(), self.docs, self)
        if path == "/search/bigquery":
            uploaded = set(request.content.decode().split("\n")[1:])
            rows = [row for row in rows if row["bibcode"] in uploaded]
        rows = _sort_rows(rows, params.get("sort", ""))
        start, count = int(params.get("start", 0)), int(params.get("rows", 10))
        fields = params.get("fl", "id").split(",")
        page = [{key: value for key, value in row.items() if key in fields} for row in rows[start : start + count]]
        return _json(200, {"responseHeader": {"status": 0}, "response": {"numFound": len(rows), "start": start, "docs": page}})

    # Libraries.

    def _metadata(self, library_id: str) -> dict[str, Any]:
        library = self.libraries[library_id]
        metadata = {key: value for key, value in library.items() if key != "documents"}
        return dict(metadata, id=library_id, num_documents=len(library["documents"]))

    def _biblib(self, method: str, path: str, request: httpx.Request, body: Any) -> httpx.Response:
        kind, _, library_id = path.partition("/")
        if kind == "libraries" and library_id.startswith("operations/"):
            return self._operation(library_id.removeprefix("operations/"), body)
        if kind == "libraries" and not library_id:
            if method == "GET":
                return _json(200, {"libraries": [self._metadata(key) for key in self.libraries]})
            return _json(200, self._create(body.get("name"), body.get("description"), body.get("public", False), body.get("bibcode", [])))
        if library_id not in self.libraries:
            return _json(404, {"error": "Library does not exist"})
        library = self.libraries[library_id]
        if kind == "libraries" and method == "GET":
            start = int(request.url.params.get("start", 0))
            rows = int(request.url.params.get("rows", 20))
            return _json(200, {"documents": library["documents"][start : start + rows], "metadata": self._metadata(library_id)})
        if kind == "documents" and method == "PUT":
            library.update(body)
            return _json(200, body)
        if kind == "documents" and method == "POST":
            return self._add_or_remove(library, body["action"], body["bibcode"])
        if kind == "documents" and method == "DELETE":
            del self.libraries[library_id]
            return _json(200, {})
        if kind == "permissions" and method == "GET":
            return _json(200, [{"example_user@example.org": ["owner"]}])
        if kind in ("permissions", "transfer") and method == "POST":
            return _json(200, {})
        raise UnsupportedByFake(f"{method} /biblib/{path}")

    def _create(self, name: str | None, description: str | None, public: bool, bibcodes: list[str]) -> dict[str, Any]:
        self._created += 1
        library_id = f"NewLibrary{self._created:02d}xQ7vT2mKp9w"
        self.libraries[library_id] = {
            "name": name or f"Untitled Library {self._created}",
            "description": description or "My ADS library",
            "public": public,
            "owner": "example_user",
            "permission": "owner",
            "num_users": 1,
            "date_created": "2026-11-13T10:00:00.000000",
            "date_last_modified": "2026-11-13T10:00:00.000000",
            "documents": list(dict.fromkeys(bibcodes)),
        }
        library = self.libraries[library_id]
        return {"id": library_id, "name": library["name"], "description": library["description"], "bibcode": library["documents"]}

    def _add_or_remove(self, library: dict[str, Any], action: str, bibcodes: list[str]) -> httpx.Response:
        documents: list[str] = library["documents"]
        if action == "add":
            new = [b for b in dict.fromkeys(bibcodes) if b not in documents]
            documents.extend(new)
            return _json(200, {"number_added": len(new)})
        gone = [b for b in documents if b in bibcodes]
        library["documents"] = [b for b in documents if b not in bibcodes]
        return _json(200, {"number_removed": len(gone)})

    def _operation(self, library_id: str, body: Any) -> httpx.Response:
        if library_id not in self.libraries:
            return _json(404, {"error": "Library does not exist"})
        mine = self.libraries[library_id]["documents"]
        others = [self.libraries[other]["documents"] for other in body.get("libraries", [])]
        action = body["action"]
        if action == "empty":
            self.libraries[library_id]["documents"] = []
            return _json(200, {})
        if action == "copy":
            target = self.libraries[body["libraries"][0]]["documents"]
            target.extend(b for b in mine if b not in target)
            return _json(200, {})
        if action == "union":
            combined = list(dict.fromkeys([*mine, *(b for other in others for b in other)]))
        elif action == "intersection":
            combined = [b for b in mine if all(b in other for other in others)]
        elif action == "difference":
            combined = [b for b in mine if not any(b in other for other in others)]
        else:
            raise UnsupportedByFake(f"library operation {action!r}")
        return _json(200, self._create(body.get("name"), body.get("description"), body.get("public", False), combined))

    # Other services.

    def _known(self, bibcodes: Iterable[str]) -> list[dict[str, Any]]:
        wanted = list(bibcodes)
        by_bibcode = {row["bibcode"]: row for row in self.docs}
        return [by_bibcode[b] for b in wanted if b in by_bibcode]

    def _export(self, fmt: str, body: Any) -> httpx.Response:
        rows = self._known(body["bibcode"])
        if fmt == "bibtex":
            entries = [
                f"@ARTICLE{{{row['bibcode']},\n"
                f"       author = {{{' and '.join(row['author'][: body.get('maxauthor', 10) or None])}}},\n"
                f"        title = \"{{{row['title'][0]}}}\",\n"
                f"         year = {row['year']},\n}}\n"
                for row in rows
            ]
            text = "\n".join(entries)
        else:
            text = "\n".join(f"{row['bibcode']}  {row['title'][0]}" for row in rows) + "\n"
        return _json(200, {"msg": f"Retrieved {len(rows)} abstracts, starting with number 1.", "export": text})

    def _resolver(self, path: str) -> httpx.Response:
        bibcode, _, link_type = path.partition("/")
        gateway = f"https://ui.adsabs.harvard.edu/link_gateway/{bibcode}"
        if link_type:
            return _json(200, {"action": "redirect", "link": f"{gateway}/{link_type.upper()}", "link_type": link_type.upper(), "service": gateway})
        records = [
            {"type": "esource", "title": "Full text sources", "url": f"{gateway}/ESOURCE", "count": 2},
            {"type": "abstract", "title": "Abstract", "url": f"https://ui.adsabs.harvard.edu/abs/{bibcode}/abstract", "count": 1},
        ]
        return _json(200, {"service": gateway, "links": {"count": len(records), "records": records}})

    def _citation_helper(self, bibcodes: list[str]) -> httpx.Response:
        given = set(bibcodes)
        suggestions = []
        for row in self.docs:
            if row["bibcode"] in given:
                continue
            links = set(row.get("reference", ())) | set(row.get("citation", ()))
            score = len(links & given)
            if score:
                suggestions.append({"bibcode": row["bibcode"], "score": score, "title": row["title"][0], "author": row["first_author"]})
        return _json(200, suggestions[:10])


def _journals_page() -> str:
    """The packaged journal list as the HTML page ``data.refresh("journals")`` downloads."""
    from scixplorer.data import read_text

    titles = json.loads(read_text("journals", "journals.json"))
    lines = [f"<a href=\"#\" onClick=\"setfields('{abbreviation}');\">{abbreviation}</a> {title}" for abbreviation, title in titles.items()]
    return "<html><body><pre>\n" + "\n".join(lines) + "\n</pre></body></html>\n"


def refresh_transport() -> httpx.MockTransport:
    """A transport for ``scixplorer.data.refresh`` serving the packaged data and the vocabulary extracts."""
    from importlib import resources

    def packaged(name: str) -> str:
        return (resources.files("scixplorer.data") / name).read_text(encoding="utf-8")

    sources: dict[str, Callable[[], str]] = {
        "journals.json": _journals_page,
        "affiliations.tsv": lambda: packaged("affiliations.tsv"),
        "affiliations_country.tsv": lambda: packaged("affiliations_country.tsv"),
        "uat.json": lambda: UAT_JSON,
        "planetary_features.csv": lambda: PLANETARY_FEATURES_CSV,
    }
    by_url = {remote.url: sources[remote.filename] for dataset in DATASETS.values() for remote in dataset.files}

    def handler(request: httpx.Request) -> httpx.Response:
        source = by_url.get(str(request.url))
        if source is None:
            return httpx.Response(404, text=f"unexpected download {request.url}")
        return httpx.Response(200, text=source())

    return httpx.MockTransport(handler)
