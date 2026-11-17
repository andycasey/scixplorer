---
title: Services
weight: 12
description: Export citations in BibTeX and other formats, find full text and data links, get network data for visualisations, and get citation suggestions.
---

After this page you can export references for your documents in any format SciX offers, list the
external resources of a document, get the data behind SciX's author and paper networks, and ask for papers
your set of papers probably should cite.

Each service is a function that sends one request. They take documents in the same forms everywhere: any
mix of bibcodes, `Document` objects, `Library` objects (their bibcodes), and lists or queries of
these. Each also takes `client=` to use a particular `Client`.

## Export

```python
from scixplorer import *

print(export.bibtex("2015ApJ...808...16N", "2013PASP..125..306F"))
# -> @ARTICLE{2015ApJ...808...16N,
```

Every function returns the exported text as SciX sends it.

| Function | Format | Options besides `sort` |
|---|---|---|
| `bibtex` | BibTeX | `max_author=10`, `author_cutoff=200`, `key_format`, `journal_format=1` |
| `bibtexabs` | BibTeX with abstracts | as `bibtex`, but `max_author=0` |
| `aastex` | AASTeX | `journal_format=1` |
| `icarus`, `mnras`, `soph` | the reference styles of Icarus, MNRAS and Solar Physics | |
| `ieee` | IEEE (Unicode) | |
| `csl` | a Citation Style Language style | `style` and `format` (required), `journal_format` |
| `ads` | the ADS generic tagged format | |
| `endnote`, `procite`, `refworks`, `ris`, `medlars` | reference-manager formats | |
| `dcxml`, `refxml`, `refabsxml`, `votable` | XML formats | |
| `custom` | your own format specification | `format` (required); no `sort` |

The options:

| Option | Meaning |
|---|---|
| `sort` | `None` (the default) asks SciX for no sort; a string such as `"date desc"` or a sort key such as `Document.year.desc()` sorts; `False` sends no sort parameter |
| `max_author` | the most authors listed before the list is cut short |
| `author_cutoff` | records with more authors than this list only `max_author` of them |
| `key_format` | how BibTeX keys are built, for example `"%1H:%Y"` gives `Accomazzi:2019`; add `%zm` to number duplicate keys. `None` uses the bibcode |
| `journal_format` | 1: AASTeX macros where they exist, else the full name; 2: abbreviations; 3: full names |
| `style` (`csl`) | one of `aastex`, `icarus`, `mnras`, `soph`, `aspc`, `apsj`, `aasj`, `ieee` |
| `format` (`csl`) | 1: Unicode, 2: HTML, 3: LaTeX |

```python
papers = (
    Document
    .select(Document.bibcode)
    .where(Document.author == "Hogg, D")
)
text = export.bibtex(papers, max_author=3, key_format="%1H:%Y", sort=Document.year.desc())
```

## Links to full text and data

`resolver.external_resources(document)` lists every kind of external resource SciX knows for one
document (full text from the publisher or arXiv, data archives, and so on). Each entry is a dict with keys
such as `type`, `title`, `url` and `count`:

```python
resources = resolver.external_resources("2015ApJ...808...16N")
[resource["type"] for resource in resources]
# -> 'esource'
```

`resolver.external_resource(document, link_type)` returns the resolver's answer for one kind, as
SciX sends it: with one link, a dict with `"action": "redirect"` and the `link`; with several, `"action":
"display"` and the `links`. `link_type` is one of `resolver.LINK_TYPES`, such as `"esource"` (all
full-text sources), `"pub_pdf"`, `"eprint_pdf"` or `"data"`:

```python
answer = resolver.external_resource("2015ApJ...808...16N", "esource")
answer["action"]
# -> redirect
```

## Network visualisations

`vis.author_network(*documents)` and `vis.paper_network(*documents)` return the data SciX uses
to draw its [author and paper networks](https://ui.adsabs.harvard.edu/help/actions/visualize), as SciX
sends it, ready for a graph library such as D3:

```python
network = vis.author_network(Library.get(name="Data-driven spectroscopy"))
```

## Citation suggestions

`citation_helper(*documents)` suggests up to ten papers that the given documents probably should cite:
papers that cite or are cited by them but are not among them. Each suggestion is a dict with `bibcode`,
`score` (an integer), `title` and `author` (each a single string, not a list as in search results):

```python
suggestions = citation_helper("2015ApJ...808...16N", "2016arXiv160303040C")
[suggestion["bibcode"] for suggestion in suggestions]
# -> ['2013PASP..125..306F']
```

## Not covered

The metrics service, myADS notifications and the other SciX services that are not listed above have no
functions in scixplorer yet. Their responses would come straight from SciX, so a `Client` can send
the request if you need one now (see [How it works]({{< ref "guide/how-it-works#sending-your-own-requests" >}})).
