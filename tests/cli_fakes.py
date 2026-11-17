"""What the tests of the ``scix`` command share: a small thesaurus, sample documents and a fake API."""

from __future__ import annotations

import json
from typing import Any

import httpx

from scixplorer import data
from scixplorer.client import Client

from search_fakes import PREFIX, FakeSearch

#: (number, name, alternative names) of the concepts in the test thesaurus.
CONCEPTS = [
    (498, "Exoplanets", ["Extrasolar planets"]),
    (753, "Hot Jupiters", []),
    (353, "Dark matter", []),
    (1880, "Galaxy dark matter halos", ["Dark matter halos"]),
    (162, "Black holes", []),
    (573, "Galaxies", []),
    (1964, "Magnetohydrodynamics", ["MHD"]),
    (1693, "The Sun", []),
    (1476, "Solar physics", []),
    (1834, "Young stellar objects", ["YSOs"]),
    (2360, "Gaia", []),
    (351, "Dark energy", ["DE"]),
]


def install_thesaurus() -> None:
    """Download the test thesaurus into the cache directory in use."""
    concepts = [
        {"uri": f"http://astrothesaurus.org/uat/{number}", "name": name, "altNames": alt or None}
        for number, name, alt in CONCEPTS
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=json.dumps(concepts)))
    [result] = data.refresh("uat", transport=transport)
    assert result.ok, result.message


def documents(n: int) -> list[dict[str, Any]]:
    """``n`` documents with the fields the command shows."""
    return [
        {
            "bibcode": f"2019ApJ...880.{i:04d}C",
            "id": str(1000 + i),
            "title": [f"Paper {i} about <SUB>2</SUB> things &amp; more"],
            "author": ["Casey, Andrew R.", "Ji, Alexander P.", "Hogg, David W."][: 1 + i % 3],
            "year": 2019,
            "pub": "The Astrophysical Journal",
            "volume": "880",
            "page": [str(100 + i)],
            "doctype": "article",
            "citation_count": 100 - i,
            "abstract": f"Abstract of paper {i}. " + "We measure things carefully. " * 12,
            "doi": [f"10.3847/1538-4357/{i}"],
            "identifier": [f"arXiv:1902.{i:05d}"],
            "property": ["ARTICLE", "REFEREED"] if i % 2 == 0 else ["ARTICLE", "NOT REFEREED"],
            "esources": ["PUB_PDF", "EPRINT_PDF"] if i % 2 == 0 else [],
        }
        for i in range(n)
    ]


class FakeAPI(FakeSearch):
    """:class:`FakeSearch`, which also answers the BibTeX export."""

    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        super().__init__(docs if docs is not None else documents(3))
        self.exported: list[list[str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.removeprefix(PREFIX).startswith("/export/"):
            self.requests.append(request)
            bibcodes = json.loads(request.content)["bibcode"]
            self.exported.append(bibcodes)
            return httpx.Response(200, json={"export": "".join(f"@ARTICLE{{{bibcode},\n}}\n\n" for bibcode in bibcodes)})
        return super().__call__(request)

    def client(self) -> Client:
        transport = httpx.MockTransport(self)
        return Client(token="t", transport=transport, async_transport=transport, sleep=lambda _: None)

    @property
    def queries(self) -> list[str]:
        """The ``q`` of every search sent, in order."""
        return [request.url.params["q"] for request in self.searches]
