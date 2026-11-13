# Data files in this directory

These data files are not covered by the MIT licence of this package. Each is
distributed under its own terms, given below.

## affiliations.tsv and affiliations_country.tsv

Canonical institutions, their parent/child hierarchy and their countries, as
used by SciX and ADS to identify affiliations (the `aff_id` search field).

- Source: the `adsabs/CanonicalAffiliations` repository,
  https://github.com/adsabs/CanonicalAffiliations
- `affiliations.tsv` is `parent_child.tsv` and `affiliations_country.tsv` is
  `country_parent_child.tsv`, redistributed unmodified (only renamed), from
  commit 42b44d1a6f6b9e391170622f2b4ff2e5faca7bb7 (2026-10-02).
- SHA-256: `affiliations.tsv`
  f16da73eb8d10e63d66ff25167c43a4da1e23878ba25e15b1a80138947b7e644,
  `affiliations_country.tsv`
  ea06a6a351c17fbe8b0d920ab634dbbd6b96fdcbb09ff0ce6dcd65c9bd06898f.
- Licence: GPL-3.0 (adsabs/CanonicalAffiliations). The licence text is in
  `LICENSE-GPL-3.0.txt` in this directory. Source:
  https://github.com/adsabs/CanonicalAffiliations

`data.refresh("affiliations")` replaces these files in the user's cache with
the current versions from the same repository.

## journals.json

Journal abbreviations (bibstems) and titles, as a JSON object
`{abbreviation: title}`, derived from the ADS list of bibliographic code
abbreviations, https://adsabs.harvard.edu/abs_doc/journals.html (a snapshot
taken in 2021).

- Licence: No licence stated (ADS bibliographic code list).

`data.refresh("journals")` replaces it in the user's cache with a conversion
of the current version of that page.

## Not shipped

These datasets are not included in the package. `data.refresh()` downloads
them to the user's cache (see `data.cache_dir()`), on request only:

- The Unified Astronomy Thesaurus, from https://github.com/astrothesaurus/UAT
  (`UAT_list.json`). Licence: CC BY-SA 3.0 (American Astronomical Society).
- The Gazetteer of Planetary Nomenclature (IAU Working Group for Planetary
  System Nomenclature, USGS), as exported for ADS in
  https://github.com/adsabs/ADSPlanetaryNamesPipeline. Licence: Public domain
  (USGS Gazetteer of Planetary Nomenclature), via an MIT-licensed adsabs
  repository.
