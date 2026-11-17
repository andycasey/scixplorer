"""Write the ``sciexplorer`` alias project: a distribution that only installs scixplorer.

``sciexplorer`` is a likely misspelling of ``scixplorer``. So that
``pip install sciexplorer`` works, each release also publishes a distribution
of that name with no code of its own and one dependency: scixplorer, at exactly
the same version. This script writes that project to a directory, taking the
version from the installed scixplorer, so that the two cannot disagree::

    uv run python scripts/make_alias.py build/sciexplorer
    uv build build/sciexplorer --out-dir dist-alias

The release workflow (``.github/workflows/release.yml``) runs these two commands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import scixplorer

ALIAS = "sciexplorer"

PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{alias}"
version = "{version}"
description = "An alias of scixplorer, the Python client for SciX: installing {alias} installs scixplorer."
readme = "README.md"
requires-python = ">=3.10"
license = "MIT"
authors = [{{ name = "Andy Casey" }}]
dependencies = ["scixplorer=={version}"]

[project.urls]
Documentation = "https://andycasey.github.io/scixplorer/"
Repository = "https://github.com/andycasey/scixplorer"
Issues = "https://github.com/andycasey/scixplorer/issues"

# The wheel has no files of its own: it only carries the dependency.
[tool.hatch.build.targets.wheel]
bypass-selection = true
"""

README = """\
# {alias}

`{alias}` is an alias of [`scixplorer`](https://pypi.org/project/scixplorer/), a Python client for
[SciX](https://scixplorer.org) (Science Explorer), the successor to the NASA Astrophysics Data System.
It exists so that a misspelt `pip install {alias}` still works: it has no code of its own, and
installing it installs the same version of `scixplorer`.

Install `scixplorer` itself where you can:

```shell
pip install scixplorer
```

Either way, the package you import is `scixplorer`:

```python
from scixplorer import Document
```

The documentation is at <https://andycasey.github.io/scixplorer/>.
"""


def write_alias(directory: Path, version: str = scixplorer.__version__) -> None:
    """Write the alias project for scixplorer ``version`` to ``directory``, creating it if needed."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(PYPROJECT.format(alias=ALIAS, version=version), encoding="utf-8")
    (directory / "README.md").write_text(README.format(alias=ALIAS), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="where to write the alias project")
    args = parser.parse_args(argv)
    write_alias(args.directory)
    print(f"wrote the {ALIAS} {scixplorer.__version__} project to {args.directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
