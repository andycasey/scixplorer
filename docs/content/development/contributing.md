---
title: Contributing
weight: 2
description: Set up a development environment, run the tests and the type checker, compare the model with the Solr schema, and regenerate and build this documentation.
---

After this page you can run every check scixplorer's code must pass, and update and build this site.

## Set up

The project is managed with [uv](https://docs.astral.sh/uv/). From a checkout:

```shell
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

Link between pages with the `ref` shortcode, `[text]({{</* ref "guide/searching" */>}})`, so that a broken
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
them to Markdown and writes public names as users write them after `from scixplorer import *`:
`Document`, `exceptions.QueryError`.

The generator replaces a few docstrings (listed in `OVERRIDES` in the script), because the documentation
describes a single API token. If one of those docstrings changes, the generator stops and asks for the
replacement to be reviewed.

### Write examples that run

Every `python` block on a hand-written page is executed by `tests/test_docs_examples.py`, in order and
in one namespace per page, against the offline sample in `tests/docs_fakes.py`: a few real papers (with
illustrative counts and affiliations), two libraries, and small extracts of the UAT and the planetary
gazetteer. No token is set and nothing reaches the network.

- Start the first Python block of a user-facing page with `from scixplorer import *`, and write public
  names bare: `Document`, `data.refresh()`. Import an internal module only where the page is about it
  (`from scixplorer import solr`). The test enforces this. Pages under `development/` may import
  anything.
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
      .where(Document.author == "Hogg, D")
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
