"""The names users import from the top-level package."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

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


def test_vocabulary_models_and_data_are_exported():
    from ads.models.planetary import PlanetaryFeature
    from ads.models.uat import UATConcept

    assert ads.UATConcept is UATConcept and ads.models.UATConcept is UATConcept
    assert ads.PlanetaryFeature is PlanetaryFeature and ads.models.PlanetaryFeature is PlanetaryFeature
    assert {"UATConcept", "PlanetaryFeature", "data"} <= set(ads.__all__)
    for name in ("refresh", "status", "clear", "cache_dir", "notices", "DATASETS"):
        assert hasattr(ads.data, name), name
    assert ads.data.DATASETS["uat"].packaged is False


def test_author_is_exported():
    from ads.models.author import Author

    assert ads.Author is Author and ads.models.Author is Author
    assert "Author" in ads.__all__ and "Author" in ads.models.__all__
    assert ads.search.to_solr(ads.Document.authors.any(ads.Author.name == "Casey, A")) == 'author:"Casey, A"'


def test_version_is_defined_before_any_import():
    tree = ast.parse(Path(ads.__file__).read_text("utf-8"))
    statements = [node for node in tree.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    first = statements[0]
    assert isinstance(first, ast.Assign) and [t.id for t in first.targets if isinstance(t, ast.Name)] == ["__version__"]


def test_import_reads_no_data_files_and_opens_no_sockets(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "uat.json").write_text("[]")
    script = f"""
import os, sys
events = []
def hook(event, args):
    if event.startswith("socket."):
        events.append(event)
    elif event == "open" and not isinstance(args[0], int):
        path = os.fsdecode(os.fspath(args[0]))
        if path.startswith({str(cache)!r}) or path.endswith((".json", ".tsv", ".csv", ".md", ".txt")):
            events.append(path)
sys.addaudithook(hook)
import ads
assert events == [], events
ads.Document.select().where(ads.Document.uat == 498)
ads.UATConcept.select().where(ads.UATConcept.name == "Stars")
ads.data.cache_dir()
assert events == [], events
assert ads.__version__ and ads.data.USER_AGENT.endswith(ads.__version__)
"""
    env = {**os.environ, "SCIX_CACHE_DIR": str(cache)}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
