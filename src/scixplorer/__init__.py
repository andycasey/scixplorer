"""A Python client for SciX (Science Explorer), the successor to NASA/ADS.

Search with Python expressions on the model classes::

    from scixplorer import Document

    docs = (
        Document.select(Document.bibcode, Document.title)
        .where((Document.author[0] == "Casey, A") & (Document.year >= 2020))
        .order_by(Document.citation_count.desc())
        .limit(10)
    )

Several conditions about the same author go inside one ``Document.authors.any(...)``::

    Document.authors.any((Author.name == "Casey, A") & (Author.affiliation.country == "Australia"))
"""

__version__ = "1.0.0.dev0"

# The version is defined before these imports because the client and scixplorer.data read it.
from . import data, exceptions
from ._config import Config, config
from .client import Client, get_default_client, set_default_client
from .expr import days_ago, now
from .models import Affiliation, Author, Document, Journal, Library, PlanetaryFeature, SearchQuery, UATConcept
from .services import citation_helper, export, resolver, vis

__all__ = [
    "Affiliation",
    "Author",
    "Client",
    "Config",
    "Document",
    "Journal",
    "Library",
    "PlanetaryFeature",
    "SearchQuery",
    "UATConcept",
    "citation_helper",
    "config",
    "data",
    "days_ago",
    "exceptions",
    "export",
    "get_default_client",
    "now",
    "resolver",
    "set_default_client",
    "vis",
]
