# Live tests

The offline tests prove that the client builds the requests it means to build.
They cannot prove that the SciX / ADS server understands those requests the way
the client assumes. The live suite in `tests/live/` does that: each test sends a
handful of real requests and checks **one assumption** about the server. Its
docstring states the assumption in plain words and names what in the client
depends on it, so the list of failures reads as a list of broken assumptions.

## Running it

| Command | What runs |
|---|---|
| `uv run pytest` | Everything offline. Every live test is skipped, and the run is guarded: any attempt to resolve a host name or connect a network socket fails the test that makes it. |
| `uv run pytest --live` | Also the read-only live tests. Tests that need the API are skipped if no token can be found; the schema-drift test needs no token (it reads GitHub). |
| `uv run pytest --live --live-library ID` | Also the tests that read a **public** library (`ID` is its id; or set `SCIX_LIVE_LIBRARY=ID`). |
| `uv run pytest --live --live-write` | Also the tests that create, change and delete libraries. They only touch libraries they create, and delete them all at the end. `--live-write` without `--live` is a usage error. |

Useful additions:

- `uv run pytest --live tests/live` runs only the live tests; add `-rs` to see why tests were skipped.
- `uv run pytest --live tests/live/test_query_syntax.py -k negation` runs part of the suite. Shared results (the pinned records, the probes) are still fetched once, so a subset costs a few requests.
- `-m "live and not live_write"` selects by marker. The markers are `live` (every live test, registered in `pyproject.toml`) and `live_write` (registered by `tests/conftest.py`).

### The token

The token is found exactly as the client finds it, by `ads._config.config.resolve_tokens()`:
the environment variable `SCIX_API_TOKEN` (or `ADS_API_TOKEN`, `ADS_DEV_KEY`), else the first line of
`~/.scix/token` (or `~/.ads/token`, `~/.ads/dev_key`). Without one, the API tests are skipped with
the message "no API token: set SCIX_API_TOKEN ...". The token is never printed:
the transport records no request headers, error texts are redacted where the
suite builds them, and a report hook replaces the token in every failure text
and captured output with `[REDACTED]`.

## The request budget

ADS allows about 5000 search requests a day per token, and far fewer for some
services. The suite is designed to need fewer than 130 requests in the worst
case and about 60 to 100 in a normal run:

| Part | Worst case | When everything passes |
|---|---:|---:|
| Shared fixtures: pinned records (1), default fields (1), sample (1), probes (at most 10) | 13 | 4 |
| `test_records.py` (deferred fields, at most 4) | 4 | 1 |
| `test_fields.py` (fields missing from the schema, at most 3) | 3 | 1 |
| `test_exactness.py` (`=field` table at most 7, author counts 2, `=bibcode` 2) | 11 | 6 |
| `test_query_syntax.py` | 27 | 26 |
| `test_operators.py` | 9 | 9 |
| `test_paging.py` | 8 | 8 |
| `test_services.py` (one request per service) | 6 | 6 |
| `test_libraries.py` | 2 | 2 |
| `test_authors.py` (per-author arrays and `pos()`: all checks in one request, then one per check, at most 9) | 10 | 1 |
| **`--live`** | **93** | **64** |
| `test_libraries.py` with `--live-library` (paging at most 4) | +10 | +9 |
| `test_libraries_write.py` with `--live-write` (16 changes, 6 deletions) | +22 | +22 |
| **With both flags** | **125** | **95** |

Retries are extra: the live client retries a 502/503/504 once, and the retry
counts. The schema-drift test makes one request to GitHub, which is not counted.

How the budget is kept, all in `tests/live/support.py` (`LiveTransport`):

- One session-scoped client sends every request through a counting transport, and is made the default client, so a lazy load or a library method cannot bypass it.
- Identical search requests (`/search/query`, `/search/bigquery`; same parameters, body and token) are answered from a session cache and not counted. Tests that need the same count share it.
- **Hard limit.** The transport refuses request 151: the test making it fails with `BudgetExhausted` (a `BaseException`, so no `except Exception` can swallow it), and the session stops after that test, running every teardown.
- **HTTP 429.** After a 429 from any service, that test and every later live test is skipped with "ADS answered HTTP 429 (rate limit exceeded) for SERVICE (it resets at ...)". Nothing more is sent.
- Library cleanup bypasses both the limit and the 429 stop.
- Where many fields or probes are checked, one request checks them all together and failing groups are split to find the culprits ("group testing", `support.isolate` and `support.peel`), under a fixed cap of requests. Anything not settled within the cap is reported as undetermined (a skip, or a row of a table). The per-author checks (`test_authors.py`) instead send everything together and, only if that fails, each check alone: their failures tend to come several at a time (a whole class of fields ignoring positions), where that needs fewer requests and settles every check.

At the end of a `--live` run two sections are printed: **live API requests**
(requests per service, cache hits, and the rate limits the server last reported)
and **live API findings** (observations that are recorded rather than asserted:
accept/reject tables, measured limits, values the client coerces, schema drift).

## Reading a failure

A failure message starts with the assumption, then shows the request and the
answer. For example:

```
AssertionError: =title:"<the whole title>" (no synonyms) matches the record with that title.
  probe:    record 1998AJ....116.1009R must match =title:"..."
  q:        bibcode:"1998AJ....116.1009R" AND =title:"..."
  request:  GET /search/query {'fl': 'bibcode', 'sort': 'score desc,id asc', 'start': 0, 'rows': 0}
  error:    400 GET /search/query: <the server's error text>
```

- `q` is exactly the Solr query the client sent, `request` the other parameters, and `error` the server's own message (or `numFound` if it answered).
- A **probe** checks that one pinned record matches (or does not match) one clause, as `bibcode:"R" AND clause`. "Found failing by elimination" means the evidence is the request of the group that held the probe.
- "**the compiler no longer emits the form this live test checks**" means `solr.py` changed: the test would now check something else, so update the expected form in the test.
- An **error in a fixture** (`pinned`, `sample`, `full_records`) fails every test that uses it with the same message. A pinned bibcode that no longer exists is replaced in `tests/live/harness.py:PINNED`.
- A skip saying "**not isolated**" means a group holding that check failed and the request cap was reached; the checks that were isolated report their own failures. Fix those and run again.
- **SchemaDriftWarning** (and the findings) list schema fields the model does not cover and other differences that are not errors.

What to change when an assumption is broken:

| Failing test | Assumption | What to change in the client |
|---|---|---|
| `test_records.py::test_bibcode_list_returns_exactly_the_listed_records` | `bibcode:("A" OR "B")` returns exactly the listed records | `solr._compile_in`; `config.max_inline_bibcodes` |
| `test_records.py::test_default_field_list_is_accepted` | One request with every stored, non-deferred field in `fl` is accepted | `Document._deferred`, or the field that is rejected (`stored=False`) |
| `test_records.py::test_default_fields_load_without_data_warnings` | Every returned value converts to its field's type | The class of the field named in the warning (`models/document.py`) |
| `test_records.py::test_default_fields_have_declared_types` | Loaded values are `None` or the type `Document.describe()` declares | `Field.to_python` / `_to_python` of that field class |
| `test_records.py::test_default_fields_are_lists_exactly_when_multivalued` | The JSON is a list exactly when the field is `multivalued` | `multivalued=` of the named field |
| `test_records.py::test_score_is_returned_by_search_query` | `score` is returned when requested | `Document.score` (make it search-only if never returned) |
| `test_records.py::test_deferred_fields_can_be_selected` | Selecting `ack`, `body`, `caption`, `citation`, `credit`, `mention`, `reference` is accepted | Make the rejected field search-only (`stored=False`), so that it is never put in `fl` |
| `test_records.py::test_deferred_fields_have_declared_types` | Deferred fields have their declared types | The class or `multivalued=` of the named field |
| `test_fields.py::test_search_alias[...]` | `arxiv:`, `collection:`, `affil:`, `inst:`, `orcid:`, `full:`, `abs:` are accepted and match | The `VirtualField` of that name; `schema_report.QUERY_PARSER` |
| `test_fields.py::test_field_missing_from_schema_is_searchable[...]` | `grant_agencies:` and `grant_id:` are accepted although schema.xml lacks them | Remove `Document.grant_agencies` / `grant_id` |
| `test_fields.py::test_not_indexed_fields_are_returned` | `links_data` and `page_range` are returned | Their definitions in `models/document.py` |
| `test_query_syntax.py::test_fielded_phrase` | `title:"w1 w2"` is a phrase match | `solr.QUOTED_FIELD_TYPES` |
| `test_query_syntax.py::test_integer_equality` | `year:1998` matches although Solr keeps year as a string | `solr.QUOTED_FIELD_TYPES`; `Document.year` (IntegerField) |
| `test_query_syntax.py::test_integer_range_partition_on_year` | `[a TO b]` is inclusive, `*` is open, `year > x` is `[x+1 TO *]` | `IntegerField.step`; `solr._compile_range` |
| `test_query_syntax.py::test_integer_range_partition_on_citation_count` | The same on `citation_count` | `IntegerField.step`; `solr._compile_range` |
| `test_query_syntax.py::test_open_ended_range_on_author_count` | `author_count:[101 TO *]` returns only records with more than 100 authors | `Document.author_count` |
| `test_query_syntax.py::test_float_exclusive_bound` | `x > v` as `(x:[v TO *] AND -x:v)` excludes exactly `v` | `solr._compile_range` (fields without `Field.step`) |
| `test_query_syntax.py::test_datetime_range_with_quoted_timestamps` | `entry_date:["2016-01-01T00:00:00Z" TO *]` is accepted and exact | `solr.QUOTED_FIELD_TYPES` (`DateTimeField`); `solr._compile_range` |
| `test_query_syntax.py::test_relative_datetime_literal` | `entry_date:[NOW-7DAYS TO *]` finds last week's records | `expr.days_ago`; `solr.format_value` (SolrLiteral) |
| `test_query_syntax.py::test_month_precision_date_range` | `pubdate:[2020-01 TO 2020-06]` includes all of June | `DateField.to_solr`; `DateField.step` |
| `test_query_syntax.py::test_negation_alone` | `*:* AND -clause` returns records not matching the clause | `solr._compile_and` |
| `test_query_syntax.py::test_and_not` | `a AND -b` excludes `b` | `solr._compile_and` |
| `test_query_syntax.py::test_or_not` | `a OR (*:* AND -b)` is a valid nested negation | `solr._compile_and`; `solr._operand` |
| `test_query_syntax.py::test_not_of_group` | `*:* AND -(a AND b)` negates the whole group | `solr._operand` |
| `test_query_syntax.py::test_nothing_matches_nothing` | `*:* AND -*:*` finds nothing | `solr._NOTHING` |
| `test_query_syntax.py::test_integer_value_list` | `year:(2014 OR 2015)` matches either year | `solr._compile_in` |
| `test_query_syntax.py::test_inline_bibcode_list_at_the_limit` | 50 bibcodes fit inline in one GET | `config.max_inline_bibcodes` |
| `test_query_syntax.py::test_long_affiliation_id_list` | 1000 `aff_id` values fit in one GET (the findings give the query length) | `search.MAX_RELATED_VALUES` |
| `test_query_syntax.py::test_single_token_wildcard` | `title:exoplan*` is a prefix match | `solr._compile_pattern` |
| `test_query_syntax.py::test_escaped_specials_in_wildcard` | `title:star\-form*` is accepted | `solr._LUCENE_SPECIALS` |
| `test_query_syntax.py::test_wildcard_inside_phrase` | `author:"Last, I*"` matches that author | `solr._compile_pattern` (quoted form) |
| `test_query_syntax.py::test_leading_wildcard` | `title:*astro*` is answered, not rejected | `TextExpr.contains` (forbid it on Document if rejected) |
| `test_exactness.py::test_exact_prefix_on_every_text_and_name_field` | `=field:"a"` is accepted on every text and name field (table in the message) | `Field.match_kind` of each rejected field; `solr.EXPANDED_MATCH_KINDS` |
| `test_exactness.py::test_exact_author_matches_its_record` | `=author:"Last, First M."` matches that exact name | `NameField.match_kind` |
| `test_exactness.py::test_exact_author_is_no_broader` | `=author:` never finds more than `author:` | `NameField` documentation (SPEC section 5) |
| `test_exactness.py::test_exact_author_with_full_first_name_is_narrower` | `=author:"Last, First"` finds strictly fewer records than `author:"Last, First"` | `NameField` documentation (SPEC section 5) |
| `test_exactness.py::test_exact_title_matches_its_record` | `=title:"<whole title>"` matches | `TextField.match_kind` |
| `test_exactness.py::test_exact_prefix_on_bibcode_matches_nothing` | `=bibcode:"X"` does not match X; `bibcode:"X"` does | Only SPEC section 5 and the `StringField` docstring (the client never sends `=` on keywords) |
| `test_exactness.py::test_exact_inside_position` | `pos(=author:"…", 2)` is accepted and matches | `solr._compile_func` |
| `test_exactness.py::test_position_one_is_the_first_author` | `pos(author:"…", 1)` is the first author | `TextExpr.__getitem__` |
| `test_exactness.py::test_position_tests_the_position` | `pos(author:"<second>", 1)` does not match | `TextExpr.__getitem__` |
| `test_exactness.py::test_position_two_argument_form` | `pos(author:"…", 2)` is the second author | `solr._compile_func` |
| `test_exactness.py::test_position_three_argument_form` | `pos(author:"…", 1, 3)` covers positions 1 to 3 | `TextExpr.__getitem__` (slices) |
| `test_exactness.py::test_first_author_field` | `first_author:"…"` matches the first author | `Document.first_author` |
| `test_operators.py::test_citations_of_a_record` | `citations(bibcode:"X")` returns records citing X | `Document.citations` |
| `test_operators.py::test_references_of_a_record` | `references(bibcode:"X")` returns X's references | `Document.references` |
| `test_operators.py::test_operator_accepts_fielded_query[...]` | `trending`, `useful`, `reviews`, `similar` accept a fielded sub-query | That operator in `models/document.py` |
| `test_operators.py::test_operator_accepts_raw_string` | `trending(exoplanets)` is accepted | `document._operand` (str arguments) |
| `test_operators.py::test_topn_takes_the_top_of_a_sorted_query` | `topn(5, a AND b, citation_count desc)` is the top five | `Document.top_n`; `solr._func_arg` (parenthesise the sub-query if this fails) |
| `test_paging.py::test_default_sort_is_accepted` | `sort=score desc,id asc` is accepted | `search._sort` (the `id` tiebreaker) |
| `test_paging.py::test_sort_by_citation_count` | `citation_count desc,id asc` sorts | `solr.compile_order`; `search._sort` |
| `test_paging.py::test_consecutive_pages_do_not_overlap` | `start`/`rows` pages do not overlap | `SearchBackend.run` |
| `test_paging.py::test_rows_zero_returns_the_count` | `rows=0` returns `numFound` only | `SearchBackend.count` |
| `test_paging.py::test_largest_page_is_returned_in_full` | `rows=2000` returns 2000 documents | `search.MAX_PAGE_SIZE` |
| `test_paging.py::test_rows_above_the_largest_page` | `rows=2001` is capped or rejected | `search.MAX_PAGE_SIZE` (could be raised) |
| `test_paging.py::test_deep_start` | `start=1000000` works | `SearchBackend.run` |
| `test_paging.py::test_bigquery_upload_with_bitset_filter` | A bigquery POST (`fq={!bitset}`, `big-query/csv`) returns only uploaded records | `SearchPlan.request` |
| `test_paging.py::test_bigquery_returns_score` | Bigquery returns `score` | `Document.score` |
| `test_paging.py::test_bigquery_count_is_the_upload_size` | Bigquery with `rows=0` counts the upload | `SearchBackend.count` |
| `test_paging.py::test_bigquery_query_filters_the_upload` | A bigquery `q` filters the upload | `search._finish` (what stays in `q`) |
| `test_paging.py::test_bigquery_sort_start_and_rows` | Bigquery honours `sort`, `start`, `rows` | `SearchPlan.request`; `SearchBackend.run` |
| `test_libraries.py::test_library_listing` | `GET /biblib/libraries` lists libraries with ids, with the declared types | `LibraryBackend`; the `Library` field classes |
| `test_libraries.py::test_unknown_library_id_yields_no_row` | An unknown library id is answered 404 | `LibraryBackend._fetch` (the 404 check) |
| `test_libraries.py::test_get_public_library_by_id` | A public library's metadata can be fetched by id | `fetch_library` |
| `test_libraries.py::test_fetch_bibcodes_paging_is_consistent` | `start`/`rows` paging of a library's bibcodes is consistent | `models.library.fetch_bibcodes`; `DEFAULT_PAGE_SIZE` |
| `test_libraries.py::test_docs_library_counts_its_documents` | `docs(library/<id>)` finds 1 to `num_documents` documents | `config.library_membership` (switch the default to `"bibcodes"` if this fails) |
| `test_libraries.py::test_docs_library_matches_its_members` | `members AND docs(library/<id>)` finds every member | as above |
| `test_libraries.py::test_not_docs_library_excludes_its_members` | `members AND -docs(library/<id>)` finds none | as above |
| `test_libraries_write.py::test_create_without_documents` | Creating with no bibcodes gives an empty library | `Library.create`; `Library._created` |
| `test_libraries_write.py::test_create_with_documents` | Creating with bibcodes stores them | `models.library._create_request` |
| `test_libraries_write.py::test_add_reports_number_added` | Adding answers `number_added` | `Library.add` |
| `test_libraries_write.py::test_remove_reports_number_removed` | Removing answers `number_removed` | `Library.remove` |
| `test_libraries_write.py::test_add_and_remove_change_num_documents` | `num_documents` follows adds and removes | `Library._change` |
| `test_libraries_write.py::test_update_changes_name_and_description` | `PUT /biblib/documents/<id>` updates metadata | `models.library._update_request` |
| `test_libraries_write.py::test_empty_removes_every_document` | The `empty` operation empties | `Library.empty` |
| `test_libraries_write.py::test_set_operation_creates_a_library[...]` | `union`, `intersection`, `difference` create the combined library | `Library._combine`; `_operation_request` |
| `test_libraries_write.py::test_docs_of_a_new_private_library` | `docs(library/<id>)` works for a private library | `config.library_membership` |
| `test_services.py::test_rate_limit_headers_are_parsed` | `X-RateLimit-*` headers are present and parsed | `Client._observe`; `client._parse_timestamp` |
| `test_services.py::test_invalid_token_raises_authentication_error` | A bad token is answered 401/403 | `client._error_for` |
| `test_services.py::test_export_bibtex` | `/export/bibtex` answers `{"export": text}` | `services.export._export` |
| `test_services.py::test_resolver_lists_external_resources` | `/resolver/<bibcode>` answers `links.records` | `services.resolver.external_resources` |
| `test_services.py::test_resolver_answers_one_link_type_with_json` | `/resolver/<bibcode>/esource` answers 200 JSON, not a redirect | `services.resolver.external_resource` (follow redirects, or return the Location) |
| `test_services.py::test_vis_author_network` | `/vis/author-network` takes `bibcodes` and answers `data` | `services.vis._request`; `_network` |
| `test_services.py::test_citation_helper` | `/citation_helper` answers the documented list of dicts | `services.citation_helper` (its docstring) |
| `test_authors.py::test_per_author_arrays_have_one_entry_per_author` | `aff`, `aff_id`, `email` and the three ORCID arrays have one entry per `author` (the findings show the counts per record) | `ads.model.Collection.records` (`Document.authors`); `Document.affiliation` |
| `test_authors.py::test_missing_per_author_values_are_a_dash` | An author without a value has `"-"` in those arrays, never null or `""` (the findings also show how several affiliations are written in `aff` and `aff_id`) | `placeholder=` in `ads.models.author.document_authors` |
| `test_authors.py::test_position_on_per_author_field[...]` | `pos(field:"v", n)` is accepted, matches author n's value and not another author's, on `aff_id`, `aff`, `email`, `orcid` and each ORCID array. A field in `search.POSITIONAL_FIELDS` must pass; for one that is not, a pass means it could be added and an xfail is what the client already assumes. The findings table lists every field | `search.POSITIONAL_FIELDS` |
| `test_authors.py::test_set_inside_position` | `pos(aff_id:("a" OR "b"), n)` matches when one of the ids is author n's | `search._position` (send one `pos()` per id instead) |
| `test_authors.py::test_aff_id_matches_each_id_of_an_entry_with_several` | `aff_id:"<id>"` matches a record whose entry for an author is `"<id>; <other>"`, for either id (skipped if no pinned author has several) | `Document.affiliation` / `Author.affiliation` (which send single ids); `separator=` in `document_authors` |
| `test_schema.py::test_document_matches_the_published_schema` | Every modelled field is in schema.xml with the same class, multivaluedness and storage | The named field in `models/document.py`; `KNOWN_UNSCHEMED` in `tests/live/test_schema.py`; `schema_report.DELIBERATE` |

## How the tests stay robust as the index changes

- Nothing is assumed about the five pinned records (`harness.PINNED`) except that their bibcodes exist. The first request fetches them, and every expectation (names, title words, year, references, aliases' values) is derived from that answer.
- Counts are compared as identities within a fixed closed set S, `bibstem:"AJ" AND year:[2014 TO 2017]`: for example numFound(S AND year <= 2015) + numFound(S AND year > 2015) == numFound(S). Citation counts change daily, but not between requests seconds apart.
- Otherwise a test returns a few rows and checks each one client-side (every returned title contains "astro"), or checks one record (`bibcode:"R" AND clause`).

## Not covered

- Library permissions (`get_permissions`, `set_permissions`) and `transfer`: they need a second ADS account.
- The lower bound of a month-precision range: `test_month_precision_date_range` checks that the upper month is included in full and that nothing outside the range is returned, but not that all of the first month is included (that would need another request).
- `=author` narrowing is checked on one prolific author; a name with no initial-only variants would make the strict test fail without any client bug.
- `vis.paper_network`, the other export formats and the async client paths make the same kind of requests as the ones tested and are not sent, to save requests.
