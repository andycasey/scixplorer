"""What the tests of the ``scix`` command share: a small thesaurus."""

from __future__ import annotations

import json

import httpx

from scixplorer import data

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
