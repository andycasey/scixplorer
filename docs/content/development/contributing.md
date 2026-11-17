---
title: Contributing
weight: 2
description: Set up a development environment, run the tests and the type checker, measure test coverage, compare the model with the Solr schema, regenerate and build this documentation, and release a version.
---

After this page you can run every check scixplorer's code must pass, update and build this site, and
release a version.

## Set up

The source is on GitHub at [andycasey/scixplorer](https://github.com/andycasey/scixplorer). Report a
problem or propose a change in its [issues](https://github.com/andycasey/scixplorer/issues).

The project is managed with [uv](https://docs.astral.sh/uv/). Clone the repository and install it:

```shell
git clone https://github.com/andycasey/scixplorer.git
cd scixplorer
uv sync
```

This creates `.venv` with the package (editable) and the development dependencies, `pytest` and `mypy`.

## Tests

```shell
uv run pytest
```

The offline suite never touches the network: `tests/conftest.py` makes any attempt to resolve a host name
or open a network socket fail the test, and points the reference-data cache at an empty temporary
directory. HTTP is faked with `httpx.MockTransport`. The suite should also run without warnings:

```shell
uv run python -W error -m pytest
```

The tests against the real API are opt-in; see [Live tests]({{< ref "development/live-tests" >}}).

### Coverage

To see which lines and branches of `src/scixplorer` the tests run:

```shell
uv run coverage run -m pytest
uv run coverage combine
uv run coverage report
```

`coverage run` writes one data file per process, because some tests start a child Python process and
those are measured too; `coverage combine` merges the files. The report lists each file that is not fully
covered, with the lines the tests missed. `uv run coverage html` writes a line-by-line report to
`htmlcov/`.

## Type checking

```shell
uv run mypy
```

mypy runs in strict mode over `src/scixplorer` (configured in `pyproject.toml`). The typing tests in
`tests/test_typing*.py` also check what a type checker infers for user code.

## Comparing the model with the Solr schema

`scripts/schema_report.py` compares `Document.describe()` with the search index's published Solr schema
(`deploy/adsabs/server/solr/collection1/conf/schema.xml` in the public `adsabs/montysolr` repository):

```shell
uv run python scripts/schema_report.py path/to/schema.xml
```

It lists fields the model lacks, model fields the schema lacks, and every difference in field class,
multi-valuedness, storage or indexing. The live test `test_schema.py` runs the same comparison against the
current schema.

## The documentation

The site is built with [Hugo](https://gohugo.io) (extended edition, 0.146 or later) and the
[Hextra](https://github.com/imfing/hextra) theme, which is a Hugo module pinned in `docs/go.mod` and
`docs/go.sum`, so Go must be installed too.

| Path | Contents |
|---|---|
| `docs/hugo.yaml` | site configuration |
| `docs/content/` | the pages; the sidebar follows this tree, ordered by each page's `weight` |
| `docs/content/reference/` | generated pages: do not edit them by hand |
| `scripts/gen_docs.py` | the generator of the reference pages |
| `tests/test_docs_examples.py` | runs every example on the site and in the README |
| `tests/docs_fakes.py` | the offline sample API those examples run against |

### Build and preview

```shell
hugo server --source docs
```

serves the site at <http://localhost:1313/> and rebuilds it as you edit. To build it as it is deployed:

```shell
hugo --source docs --gc --minify --panicOnWarning --printPathWarnings
```

The first build downloads the theme (and the search library it bundles), and Hugo caches them. The
output goes to `docs/public/`, which git ignores. `baseURL` in `docs/hugo.yaml` is `/`; a deployment must
set the real one, for example with `--baseURL`.

Link between pages with the `ref` shortcode, `[text]({{</* ref "usage/searching" */>}})`, so that a broken
link fails the build. The build test runs this build into a temporary directory and checks the key pages;
it is opt-in, because it needs Hugo and, the first time, the network:

```shell
SCIX_TEST_DOCS=1 uv run pytest tests/test_docs_build.py
```

### Regenerate the reference pages

The pages under `docs/content/reference/` are generated from the code: the field tables from
`describe()`, the API pages from signatures and docstrings, and the expression cheat sheet by compiling
each expression. After changing a field, a docstring or the compiler, run:

```shell
uv run python scripts/gen_docs.py
```

`tests/test_docs_generated.py` runs `scripts/gen_docs.py --check`, which fails if any generated page is
out of date. Docstrings are written in reST (`:class:`, `:param x:`, `::` blocks); the generator converts
them to Markdown and writes public names as users write them once imported from `scixplorer`:
`Document`, `exceptions.QueryError`.

The generator replaces a few docstrings (listed in `OVERRIDES` in the script), because the documentation
describes a single API token. If one of those docstrings changes, the generator stops and asks for the
replacement to be reviewed.

### Write examples that run

Every `python` block on a hand-written page is executed by `tests/test_docs_examples.py`, in order and
in one namespace per page, against the offline sample in `tests/docs_fakes.py`: a few real papers (with
illustrative counts and affiliations), two libraries, and small extracts of the UAT and the planetary
gazetteer. No token is set and nothing reaches the network.

- Import only the names a page uses. Start the first Python block of a user-facing page with one line
  such as `from scixplorer import Document, search`, with the names sorted, and write those names bare:
  `Document`, `search.to_solr()`. Never write `from scixplorer import *` or `import scixplorer`. Import an
  internal module only where the page is about it (`from scixplorer import solr`). The test enforces
  this: every public name a page uses must be imported, and every imported name must be used. Pages
  under `development/` may import anything.
- Write a query as a parenthesised chain with one method per line, starting from the model on its own
  line. Give `select()` one field per line when it has more than one. Assign the query to a name, then
  call `count()`, `explain()` and the like on that name:

  ```text
  query = (
      Document
      .select(
          Document.bibcode,
          Document.title,
      )
      .where(Document.author == "Kewley, L")
      .order_by(Document.year.desc())
      .limit(10)
  )
  ```
- Show a value with a line `# -> text` right after an expression. The test checks that `text` is in the
  `repr` or `str` of the value, or in what the statement printed. Output shown any other way is not
  checked, so prefer this form. Pick text that is true of the real data too, not only of the sample.
- Mark a block with an HTML comment on the line before it:
  `<!-- docs-test: raises QueryError -->` when it must raise exactly that exception,
  `<!-- docs-test: warns LazyLoadWarning -->` when it may emit that warning (any other warning fails the
  block), or `<!-- docs-test: skip -->` when it cannot run offline.

Run only the examples with:

```shell
uv run pytest tests/test_docs_examples.py
```

A failure names the page, the block's line and the problem, and shows the block.

## Continuous integration

Four GitHub Actions workflows in `.github/workflows/` run on GitHub:

| Workflow | Runs on | What it does |
|---|---|---|
| `tests.yml` | a push to `main`, a pull request | Runs the offline suite, with warnings as errors, on Python 3.10 to 3.14, then reports the coverage of the five runs combined. |
| `docs.yml` | a push to `main`, a pull request | Runs the build test and builds the site. From `main` it also publishes the site to GitHub Pages, at <https://andycasey.github.io/scixplorer/>. |
| `live.yml` | every night, or on request | Runs the read-only [live tests]({{< ref "development/live-tests" >}}) against the real API. |
| `release.yml` | a pushed tag `v…` | Releases that version; see below. |

The coverage table is on the summary page of each run of `tests.yml`, and the line-by-line report is the
`coverage-html` artifact of that run.

The live tests do not run with the others, because they need the network and an API token. `live.yml`
runs them with the token in the repository secret `SCIX_API_TOKEN`, and never with `--live-write`. To
run them at another time, choose **Run workflow** on the workflow's page, or:

```shell
gh workflow run live.yml
```

## Release a version

1. Set the version in `pyproject.toml`, in `__version__` in `src/scixplorer/__init__.py` and in the badge
   on the site's home page (`docs/content/_index.md`), and run `uv lock`, which records it in `uv.lock`.
   Commit, and push to `main`.
2. Tag that commit with `v` and the version, and push the tag:

   ```shell
   git tag v1.0.0
   git push origin v1.0.0
   ```

The tag starts `release.yml`. It runs the tests, checks that the tag, `pyproject.toml` and `__version__`
agree, and builds the source distribution and the wheel. It then publishes them to
[PyPI](https://pypi.org/project/scixplorer/) and creates the GitHub release, with both files attached and
notes listing the changes since the last release. A version with an `a`, `b`, `rc` or `.dev` part is
marked as a pre-release on GitHub. PyPI never accepts the same version twice, so a release that has been
published is corrected with a new version, not a moved tag.

No PyPI token is stored on GitHub. PyPI accepts the upload by
[trusted publishing](https://docs.pypi.org/trusted-publishers/): the `scixplorer` project on PyPI lists
this repository, the workflow `release.yml` and the environment `pypi` as a publisher.
