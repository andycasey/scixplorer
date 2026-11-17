"""The names users import from the top-level package."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import scixplorer


def test_every_exported_name_exists():
    for name in scixplorer.__all__:
        assert hasattr(scixplorer, name), name


def test_config_is_the_settings_instance_used_by_the_default_client():
    assert isinstance(scixplorer.config, scixplorer.Config)
    scixplorer.set_default_client(None)
    try:
        assert scixplorer.get_default_client().config is scixplorer.config
    finally:
        scixplorer.set_default_client(None)


def test_models_services_and_helpers_are_reachable():
    assert scixplorer.Document.select().model is scixplorer.Document
    assert scixplorer.Library.documents is not None
    assert callable(scixplorer.export.bibtex)
    assert callable(scixplorer.citation_helper)
    assert scixplorer.days_ago(7).text == "NOW-7DAYS"
    assert issubclass(scixplorer.exceptions.RateLimitExceeded, scixplorer.exceptions.APIError)


def test_search_and_utils_modules_are_exported():
    namespace: dict[str, object] = {}
    exec("from scixplorer import *", namespace)
    assert namespace["search"] is scixplorer.search and namespace["utils"] is scixplorer.utils
    assert callable(namespace["search"].to_solr) and callable(namespace["utils"].flatten)


def test_library_documents_is_a_document_query():
    library = scixplorer.Library(id="abc", name="x")
    query = library.documents
    assert query.model is scixplorer.Document
    assert scixplorer.search.to_solr(query) == "docs(library/abc)"


def test_vocabulary_models_and_data_are_exported():
    from scixplorer.models.planetary import PlanetaryFeature
    from scixplorer.models.uat import UATConcept

    assert scixplorer.UATConcept is UATConcept and scixplorer.models.UATConcept is UATConcept
    assert scixplorer.PlanetaryFeature is PlanetaryFeature and scixplorer.models.PlanetaryFeature is PlanetaryFeature
    assert {"UATConcept", "PlanetaryFeature", "data"} <= set(scixplorer.__all__)
    for name in ("refresh", "status", "clear", "cache_dir", "notices", "DATASETS"):
        assert hasattr(scixplorer.data, name), name
    assert scixplorer.data.DATASETS["uat"].packaged is False


def test_author_is_exported():
    from scixplorer.models.author import Author

    assert scixplorer.Author is Author and scixplorer.models.Author is Author
    assert "Author" in scixplorer.__all__ and "Author" in scixplorer.models.__all__
    assert scixplorer.search.to_solr(scixplorer.Document.authors.any(scixplorer.Author.name == "Casey, A")) == 'author:"Casey, A"'


def test_version_is_defined_before_any_import():
    tree = ast.parse(Path(scixplorer.__file__).read_text("utf-8"))
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
import scixplorer
assert events == [], events
scixplorer.Document.select().where(scixplorer.Document.uat_concepts == 498)
scixplorer.UATConcept.select().where(scixplorer.UATConcept.name == "Stars")
scixplorer.data.cache_dir()
assert events == [], events
assert scixplorer.__version__ and scixplorer.data.USER_AGENT.endswith(scixplorer.__version__)
"""
    env = {**os.environ, "SCIX_CACHE_DIR": str(cache)}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
