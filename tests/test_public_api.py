"""The names users import from the top-level package."""

from __future__ import annotations

import ads


def test_every_exported_name_exists():
    for name in ads.__all__:
        assert hasattr(ads, name), name


def test_config_is_the_settings_instance_used_by_the_default_client():
    assert isinstance(ads.config, ads.Config)
    ads.set_default_client(None)
    try:
        assert ads.get_default_client().config is ads.config
    finally:
        ads.set_default_client(None)


def test_models_services_and_helpers_are_reachable():
    assert ads.Document.select().model is ads.Document
    assert ads.Library.documents is not None
    assert callable(ads.export.bibtex)
    assert callable(ads.citation_helper)
    assert ads.days_ago(7).text == "NOW-7DAYS"
    assert issubclass(ads.exceptions.RateLimitExceeded, ads.exceptions.APIError)


def test_library_documents_is_a_document_query():
    library = ads.Library(id="abc", name="x")
    query = library.documents
    assert query.model is ads.Document
    assert ads.search.to_solr(query) == "docs(library/abc)"
