---
title: Reference data
weight: 11
description: Which reference data ships with scixplorer, which is downloaded, where it is kept, how to update it, and under which licences.
---

After this page you know which data scixplorer holds locally, how to download or update it, where it is
stored, and the licence of each part.

## Four datasets

Searches through journals, affiliations and vocabularies are resolved in Python against local copies of
four datasets:

| Dataset | Used by | Packaged | Licence |
|---|---|---|---|
| `journals` | [`scix.Journal`]({{< ref "guide/journals" >}}) | yes (a 2021 snapshot) | no licence stated (the ADS bibliographic code list) |
| `affiliations` | [`scix.Affiliation`]({{< ref "guide/affiliations" >}}) | yes | GPL-3.0 (adsabs/CanonicalAffiliations) |
| `uat` | [`scix.UATConcept`]({{< ref "guide/vocabularies" >}}) | no | CC BY-SA 3.0 (American Astronomical Society) |
| `planetary_features` | [`scix.PlanetaryFeature`]({{< ref "guide/vocabularies" >}}) | no | public domain (USGS Gazetteer of Planetary Nomenclature), via an MIT-licensed adsabs repository |

The packaged data files are not covered by scixplorer's MIT licence. `scix.data.notices()` returns the
notice that ships with them: where each file comes from, the commit it was taken from, and its licence.

```python
import scixplorer as scix

print(scix.data.notices())
# -> These data files are not covered by the MIT licence of this package.
```

The datasets and their sources are also in `scix.data.DATASETS`.

## Nothing is downloaded unless you ask

scixplorer never downloads data on its own. Until you download the two vocabularies, anything that needs
them raises `scix.exceptions.DataNotAvailable`, with a message that says what to run.

`scix.data.refresh()` downloads datasets into your cache directory:

```python
results = scix.data.refresh("uat", "planetary_features")
[result.ok for result in results]
# -> [True, True]
```

With no names it refreshes all four. Each result has `name`, `ok`, `files` (the files written) and
`message`. One dataset failing does not stop the others, so check `ok` on each.

The downloads come from the ADS website and from GitHub, never from the SciX API, and they never carry your
API token: they are sent with a separate HTTP client that has only a generic `User-Agent`. `refresh()`
takes `timeout=` (seconds per request, 60 by default) and `transport=` (an `httpx` transport, for a proxy
or for tests).

A download replaces the data in use only if it is good. It is parsed with the same code the model uses,
and rejected, leaving your data untouched, if it does not parse, holds no records, or holds fewer than half
as many records as the data in use. A dataset's files are replaced together or not at all, and each is
written atomically.

Run `refresh()` again whenever you want newer data. For `journals` and `affiliations`, the downloaded
copy then takes precedence over the packaged snapshot.

## Where the data is kept

```python
scix.data.cache_dir()
```

| Platform or setting | Cache directory |
|---|---|
| `SCIX_CACHE_DIR` is set | that directory |
| `XDG_CACHE_HOME` is set | `$XDG_CACHE_HOME/scixplorer` |
| macOS | `~/Library/Caches/scixplorer` |
| elsewhere | `~/.cache/scixplorer` |

The directory is created only when something is written to it.

## Which copy is in use

`scix.data.status()` says, for each dataset, whether the cached copy, the packaged one, or none is in use,
with the paths, the time it was last modified (UTC) and its size in bytes:

```python
[(status.name, status.source) for status in scix.data.status()]
# -> ('journals', 'packaged'), ('affiliations', 'packaged'), ('uat', 'cache'), ('planetary_features', 'cache')
```

A cached copy that cannot be read is skipped with a `scix.exceptions.DataWarning`: the packaged snapshot
is used instead, or, for the vocabularies, `DataNotAvailable` is raised.

## Remove downloaded data

`scix.data.clear()` deletes cached copies (all, or the datasets you name). The packaged snapshots are kept,
so journals and affiliations go back to them:

```python
scix.data.clear("uat")
[status.source for status in scix.data.status() if status.name == "uat"]
# -> ['missing']
```

<!-- docs-test: raises DataNotAvailable -->
```python
scix.UATConcept.lookup(498)
```
