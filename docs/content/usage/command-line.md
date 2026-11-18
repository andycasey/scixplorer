---
title: Command line
weight: 16
description: Search SciX from the terminal with the scix command, which reads plain words as authors, years, journals and concepts.
---

After this page you can search SciX from a terminal with `scix`, know how it reads what you type, browse
the results with the keyboard, and print them as a list, BibTeX or JSON.

Installing scixplorer installs the command `scix`. To have it everywhere, not only in one project's
environment, install it as a tool:

```shell
uv tool install scixplorer
```

It uses the same [API token]({{< ref "getting-started/token-management" >}}) as the Python package.

## Type what you mean

```shell
scix Kewley 2008
scix exoplanets 2026
scix ApJ 608 119
```

`scix` reads each word and says how it read it. The three searches above become:

```text
author Kewley · year 2008
concept Exoplanets (UAT 498) · year 2026
journal The Astrophysical Journal (ApJ) · volume 608 · page 119
```

`--explain` shows this, and the query that would be sent, without sending it:

```shell
scix --explain Kewley et al. 2019 ApJ interstellar medium
```

```text
Kewley et al.        first author  Kewley
2019                 year          2019
ApJ                  journal       The Astrophysical Journal (ApJ)
interstellar medium  concept       Interstellar medium (UAT 847)

query  first_author:"Kewley" AND year:2019 AND bibstem:"ApJ" AND uat_id:847
sort   most cited first
```

## How words are read

Each word is tried against these rules in order, and the first that fits wins:

| You type | It is read as |
|---|---|
| `author:"Kewley, L"`, `title:gaia`, `a:Kewley`, `j:ApJ`, `y:2019` | a condition on that field: any [document field]({{< ref "reference/document-fields" >}}), or the short forms `a` (author), `fa` (first author), `t` (title), `kw` (keyword), `j` (journal), `y` (year), `vol`, `p` (page), `uat` |
| `citations:2019ApJ...880...16K`, `trending:exoplanets` | an [operator]({{< ref "usage/operators" >}}) applied to the rest, which is read by these same rules: `citations`, `references`, `trending`, `useful`, `reviews`, `similar` |
| `"dark matter halo"` | a phrase in the title, abstract or keywords; `"Kewley, L."` in quotes is an author, and a journal's title in quotes is that journal |
| `2019ApJ...880...16K`, `10.3847/1538-4357/ab1b3a`, `arXiv:2301.12345`, `0000-0002-1825-0097` | a bibcode, DOI, arXiv identifier or ORCID iD; a link to a SciX, ADS, DOI, arXiv or ORCID page works too |
| `2005`, `2005-2010`, `2015-`, `since 2020`, `before 2010` | a year or a range of years |
| `ApJ`, `mnras`, `Nature`, `Astrophysical Journal` | a journal, by abbreviation or title; the numbers after it are its volume and page |
| `Kewley, L. J.`, `L. J. Kewley`, `van der Marel` | an author with initials |
| `exoplanets`, `hot jupiters`, `black hole` | the longest run of words that names a [UAT concept]({{< ref "usage/vocabularies" >}}), ignoring case and a plural ending |
| `Kewley` | a capitalised word is an author's surname |
| `halos`, `kewley` | any other word is searched in the title, abstract and keywords; a lower-case word that the thesaurus does not use may be a surname typed without its capital, so it matches an author or the text |

A few more things it understands:

- `Kewley et al. 2019` and `^Kewley 2019` mean Kewley is the first author.
- A reference pasted from a paper is read piece by piece:
  `scix "Kewley, L. J., Nicholls, D. C., Sutherland, R. S. 2019, ARA&A, 57, 511"`.
- `refereed` limits the search to refereed documents, as does `--refereed`.
- A search that already looks like Solr syntax (`AND`, `OR`, `[2000 TO 2010]`, a wildcard) is sent as it
  is. `--query` sends any words that way.

If a search matches nothing, `scix` widens it once and tells you: a surname is also searched as a
word, and a concept also as its words. So `scix Hubble tension` finds papers about the Hubble tension,
not papers by Edwin Hubble.

{{< callout type="info" >}}
Concepts are recognised once the thesaurus is downloaded, because it is
[not packaged with scixplorer]({{< ref "usage/reference-data" >}}). Run `scix --refresh-data` once. Until
then, topics are searched as words.
{{< /callout >}}

Your shell reads some characters before `scix` sees them. Quote a word that holds `&`, `*`, `?` or
parentheses, such as `'A&A'`; `A+A` and `AA` also mean Astronomy and Astrophysics.

## Browse the results

In a terminal, the results open in a browser: a list, with the selected document's abstract below it.
This is `scix "^Kewley, L" 2019`:

```text
 first author Kewley, L · year 2019                                                     1/3
 most cited first
  2019 Kewley et al.     Understanding Galaxy Evolution Through Emission Lin…   ARA&A    642
  2019 Kewley et al.     Theoretical ISM Pressure and Electron Density Diagn…     ApJ    130
  2019 Kewley            Diversity and inclusion in Australian astronomy        NatAs      3
────────────────────────────────────────────────────────────────────────────────────────────
 Understanding Galaxy Evolution Through Emission Lines
 Kewley, Lisa J.; Nicholls, David C.; Sutherland, Ralph S.
 ARA&A 57, 511 (2019) · 642 citations · refereed · 2019ARA&A..57..511K

 We review the use of emission lines for understanding galaxy evolution, focusing on
 excitation source, metallicity, ionization parameter, ISM pressure, and electron density.…
 ↑↓ move   ⏎ read   o open   f full text   b BibTeX   c cited by   r references   s sort…
```

| Key | What it does |
|---|---|
| `↑` `↓` or `j` `k`, `PgUp` `PgDn`, `g` `G` | move; more results load as you near the end |
| `Enter` | read the whole abstract; `←` or `Esc` goes back |
| `o`, `f` | open the document's SciX page, or its full text, in your web browser |
| `c`, `r` | search for the documents that cite it, or that it cites; `←` returns to the previous search |
| `space` | mark a document |
| `b`, `y` | copy the BibTeX of the marked documents (or the selected one), or the bibcode, to the clipboard |
| `s` | change the order: newest, most cited, most read, most relevant, oldest, by first author |
| `/` | search again |
| `?` | show how the search was read, and these keys |
| `q` | quit |

Results are ordered newest first, unless the search names a year: then the most cited come first.
`--sort` chooses an order: `date`, `citations`, `reads`, `relevance`, `oldest` or `author`.

## Print the results

When the output is not a terminal, or with `--plain`, the results are printed instead. `-n` says how many
(10 by default, or `all`):

```shell
scix ApJ 608 119 --plain
```

```text
journal The Astrophysical Journal (ApJ) · volume 608 · page 119
1 document

1  Distribution of Faraday Rotation Measure in Jets from Active Galactic Nuclei. II.
   Prediction from Our Sweeping Magnetic Twist Model for the Wiggled Parts of Active
   Galactic Nucleus Jets and Tails
   Kigure et al. · ApJ 608, 119 (2004) · 12 citations
   2004ApJ...608..119K
```

Other formats print only the data on standard output, so they can be piped or saved; how the search was
read goes to standard error:

```shell
scix Kewley et al. 2019 ApJ --bibtex >> references.bib
scix exoplanets 2026 -n 100 --bibcodes | sort
scix "hot jupiters" since 2020 -s citations --json
```

`--format` takes `list`, `bibcodes`, `json`, or a format of the
[export service]({{< ref "usage/services" >}}): `bibtex`, `bibtexabs`, `aastex`, `ris`, `endnote` and the
others. `--open` opens the first result in your web browser.

| Option | Meaning |
|---|---|
| `-n N`, `--limit N` | print `N` documents, or `all` |
| `-s ORDER`, `--sort ORDER` | the order of the results |
| `-r`, `--refereed` | only refereed documents |
| `-c NAME`, `--collection NAME` | only one [collection]({{< ref "usage/collections" >}}), such as `astronomy` |
| `-q`, `--query` | send the words as a Solr query, without reading them |
| `-x`, `--explain` | show how the words are read, and send nothing |
| `-p`, `--plain` | print the results instead of browsing them |
| `-o`, `--open` | open the first result in your web browser |
| `-f FORMAT`, `--format FORMAT` | print in another format; `--bibtex`, `--json` and `--bibcodes` are short for three of them |
| `--refresh-data` | download the [reference data]({{< ref "usage/reference-data" >}}), then stop |
| `--mcp` | run as an [MCP server]({{< ref "usage/mcp" >}}) for an AI assistant; `--read-only` leaves out the tools that change libraries |

The exit status is 0 if documents were found, 1 if none matched, and 2 if the search could not be run.

## How fast it is

`scix` starts in about a tenth of a second: reading a search, with the journal list and the thesaurus
loaded, needs no request. A search then takes as long as SciX takes to answer, which is mostly the three
round trips of a new encrypted connection. How it read your words is printed before the request is sent,
so you see at once what is being searched.
