# sdmxlib-rest — CLAUDE.md

## What this is

A generic SDMX-REST HTTP surface over a [sdmxlib](../sdmxlib) `BundleRegistry`.
It owns the parts of an SDMX-REST service that are the same for every catalog —
URL grammar, content negotiation, localisation, streaming, response assembly —
and pushes everything catalog-specific behind explicit seams.

Extracted in place from its first consumer service over four phases and split
out here in the fifth. Every seam exists because a real service needed it.
Don't add a seam speculatively.

## The rule

**The library holds no catalog-specific logic.** If something needs to know
about a particular bundle, lake layout, agency, codelist or vendor parameter, it
belongs in the consumer, behind a seam. The library provides the Protocol and
an inert default; the consumer provides the implementation.

A default must be inert: a consumer installing none of the seams must get a
*correct* service that recomputes more than it needs to.

## Seams

All are Protocols carried on `CatalogRuntime` (`runtime.py`).

`CatalogProvider` is the one seam that names a concrete type: it returns an
`sl.BundleRegistry`, the read-only registry `LocalRegistry.bundle` opens,
because `binding_for` — which every data-plane helper needs — exists only
there and no sdmxlib Protocol covers it.

| Seam | File | Default |
|---|---|---|
| `CatalogProvider` | `provider.py` | **required** |
| `SpliceRegistry` | `splice.py` | empty |
| `StructureCache` | `structure_cache.py` | `NullStructureCache` |
| `AvailabilityValues` | `availability_values.py` | `NullAvailabilityValues` |
| `MetadataStore` | `metadata_store.py` | `NullMetadataStore` |
| `ComponentFilterShaper` | `filter_shaper.py` | `NullComponentFilterShaper` |
| `MessageAnnotator` (+ optional `SupportsFingerprint`) | `message_annotator.py` | `NullMessageAnnotator` |

## Layout

```
src/sdmxlib_rest/
    app.py               # create_app — middleware stack, mounts, multi-spec /docs
    runtime.py           # CatalogRuntime + request deps (get_registry, get_meta_conn, ...)
    provider.py          # CatalogProvider / OpenedCatalog
    splice.py            # post-serialise JSON transform pipeline
    negotiation.py       # Accept / content negotiation
    localization.py      # Accept-Language localisation
    structure_cache.py   metadata_store.py   availability_values.py  filter_shaper.py
    overlays.py          # OverlayAnnotator: sdmxlib annotation overlays at serve time
    data/                # data-plane helpers; consumers own the route (see below)
        params.py        # c[] filters, sort, dimensionAtObservation, id patterns
        projection.py    # attributes=/measures= projection, key_dimension_ids
        encoding.py      # SDMX-CSV+labels, BOM, streaming zip, streaming parquet
        paging.py        # Range request -> Content-Range response headers
        sql.py           # binding-driven SQL fragments (DECIMALS precision, QUALIFY)
    routes/
        structure.py     metadata.py     availability.py
    middleware/          # etag, compression, head_method, lang_override,
                         # version_token, query_param_guard, perf_log, openapi_extensions
    responses/           # asof, empty (204), availability (render from value lists),
                         # unwritable (sdmxlib ValidationError -> 500 naming artefact + rule)
```

## The data plane

**The library owns data-plane *helpers*; consumers own the `/data` route.**

Orchestration is where response caching, partition pushdown and vendor
extensions live, and every catalog shapes those differently. A consumer gets the
fiddly parts — key parsing, projection, paging headers, SQL fragments, the
arrow→CSV/parquet encoders — and writes its own handler around them.

`/availability` is the one library route that reads observations. It reads
them through sdmxlib's query DSL, `registry.obs(flow)` (#6), so a provider's
registry must reach its lake on its own connection; `meta_conn` is only for
consumers' seams. The DSL builds one query root per request, ~13–19 ms on a
large census bundle (sdmxlib#426); don't cache roots across requests, since a
root binds the constructing thread's cursor.

Resist requests to "just add a data route". It was considered and rejected with
a quantitative inventory of what each consumer's handler does.

## SDMX-REST 2.2.2 URL grammar

Every published path has the 2.2.2 shape, which settles what a dotted key
means. 2.2.2 makes the time dimension a **key slot**, so a key is parsed
against `dsd.key_slot_ids("3.0")` and a short key returns 400.

SDMX-REST 1.5/2.1 keeps time *out* of the key (`startPeriod` / `endPeriod`
filter it instead), so a 1.5 key is one segment shorter for the same DSD.
Parsing a key against the other generation's slot list shifts every value by
one slot and returns wrong data without raising an error.

**A 1.5 surface is a consumer's forwarder**, translating the older grammar
before it reaches these routes. The forwarder knows which generation it serves
and the routes serve only 2.2.2, so the data plane takes no `ApiVersion`.
`AvailabilityKey` keys on the resolved flow URN, so two flows stay distinct
under any forwarder path shape, and the 1.5 and 2.2.2 surfaces share one
precomputed entry.

Individual 1.5 *conveniences* are backported onto the 2.2.2 routes as alternate
spellings where they cost nothing — the `all` wildcard,
`startPeriod`/`endPeriod`, the no-`componentID` form.

## The middleware ordering invariant

`_install_middleware` in `app.py` builds a chain whose adjacencies each have a
real failure mode — an ETag over compressed bytes, a `?lang=` that hashes wrong,
a CORS preflight falling through to a 404. The docstring documents each one.
**Read it before touching the order.** `tests/test_library_create_app.py` pins
the sequence; change that assertion only together with the docstring.

## Style

- Docstrings: Google convention, plain — cross-references in single backticks
  (`` `CatalogRuntime` ``), no Sphinx roles or NumPy sections
  (`just check-docstrings`). Skip docstrings on trivial getters.
- No comments that restate the code. Only WHY-comments.
- Conventional commits — `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`.
- Nothing is signed with Claude: commit messages carry no `Co-Authored-By`
  trailer, and PR descriptions, issues and comments carry no "Generated with
  Claude Code" footer. The README's note discloses AI use once for the project.
- Install instructions (README, docs, error messages) say `uv add <pkg>` first
  and `pip install <pkg>` second, as the fallback.
- SLF001 is off (`ruff.toml`), as in sdmxlib. Ruff cannot see package
  boundaries, so it flags a module reading its own package's `_helpers` and one
  instance reading another's private state; sdmxlib found no defect in 40
  suppressed sites. Don't reintroduce `# noqa: SLF001`.
- Tests are plain functions; module-level `pytestmark = pytest.mark.unit`.

## Writing style

These rules cover all prose written for this repo: comments, docstrings,
documentation, CHANGELOG and `docs/upgrading.md` entries, commit messages,
issues, PRs and milestones. The section is shared across the StatCat-Suite
repos (GH-58); change it in all three or none.

Write in plain language. State claims directly, in the order the reader
needs them. Use subject-verb-object sentences, concrete nouns, and the
actual number, name, or mechanism instead of an allusion to it.

Follow these rules from Strunk & White:

- Put statements in positive form.
- Use definite, specific, concrete language.
- Omit needless words.

Avoid these constructions everywhere (sentences, headings, titles, lists):

- Contrast by negation: "not X, but Y", "it's not about X, it's about Y",
  "X isn't the problem; Y is". State Y. Mention X only if someone
  actually holds that view.
- Hook clauses that read like half a question: "what X doesn't tell you",
  "the part nobody mentions", "the evidence Y lacks", "why this matters".
  State the fact the clause points at.
- Colon reveals: "The real issue: ...", "Here's the thing: ...",
  "The short version: ...". Drop the setup and write the sentence.
- Punchline structure: ending a sentence or list on a twist, a dramatic
  final item, or a short fragment for emphasis.
- Rhetorical questions that the text then answers.
- Framing work or ideas by what is missing when you can name what is
  present or proposed.

Titles (milestones, issues, PRs, headings):

- Name the deliverable or scope as a plain noun phrase or imperative.
  A reader should know from the title alone what gets merged or decided.
- Use a colon only to separate a component name from the work item
  (e.g. "Query DSL: add DuckDB backend"), never to introduce a hook.

Examples:

```text
Bad:  Conformance evidence: what the green checks do not prove
Good: Conformance: add negative and edge-case test fixtures

Bad:  Query DSL: a second backend, and the evidence the first one lacks
Good: Query DSL: add second backend; add validation tests for SQL backend

Bad:  The green checks tell you less than you'd think.
Good: The green checks only cover valid inputs; there are no tests
      for malformed DSDs or constraint violations.

Bad:  The problem isn't performance — it's correctness.
Good: The cache returns stale codelists after a version bump.

Bad:  Here's what the benchmark doesn't show: cold-start latency.
Good: The benchmark measures warm queries only; cold-start latency
      is untested.
```

Before finishing, reread the output. Rewrite any sentence that delays its
point, contrasts with a claim nobody made, or alludes to a fact instead of
stating it.

## Issue references

Issues live on GitHub (StatCat-Suite/sdmxlib-rest). The project moved from
GitLab in October 2026, and the tree cites the GitLab tracker as a bare `#N` —
`#6`, `#7`. Those still mean GitLab (gitlab.com/pinax-suite/sdmxlib-rest, an
archive now) and stay as written; `sdmxlib#N` likewise means sdmxlib's GitLab
tracker.

**Write a GitHub issue or PR as `GH-N`**, never a bare `#N`: GitHub numbers
from 1, so a bare number would collide with the GitLab one the tree already
means. The issues still open at the move were re-created on GitHub, each
carrying a "Moved from GitLab #N" line.

## Common commands

```bash
just check     # ruff (warnings fail too) + docstrings + prose + Any ratchet + index guard + pyrefly
just check-any # `Any` in src/ against tools/any_baseline.txt; record a drop with
               #   `uv run python tools/count_any.py --write`
just test      # unit tests
just ci        # full gate
just typecheck # pyrefly alone (also part of `check`)
just bench <bundle-dir>   # the bundle tier — ratios against a real lake; `ci` skips it
just bench-synthetic [shape]  # the same tier on a synthetic bundle: `large`, or `census` for a census-sized geography
just docs      # serve the docs site (docs/, mkdocs.yml); `just docs-build` is the strict build in `ci`
```

## Versioning

Versions independently of any consumer — a consumer pins a range and upgrades
deliberately. Pre-1.0, breaking a seam Protocol is a minor bump; after 1.0 it is
a major. `just release-1-preview` through `release-5-pypi`: a release is a
`release/vX` PR like any other change. git-cliff drafts the changelog entry
(steps 1-2, prepended, with a pre-1.0 breaking change bumping the minor); steps
3-4 publish the CHANGELOG.md section as curated after step 2
(`just release-notes VERSION`); step 5 builds the checked-out tag and uploads it
to PyPI, with `UV_PUBLISH_TOKEN` from `.env`.

Every suite library is pre-1.0, so a minor bump may break. `pyproject.toml`
caps each suite dependency below its next minor (`sdmxlib>=0.74,<0.75`); the
PR that adopts a new sdmxlib minor raises the cap.

## Docs site

`docs/` and `mkdocs.yml` build https://statcat-suite.github.io/sdmxlib-rest/,
deployed by `.github/workflows/docs.yml`. `mkdocs.yml` carries the StatCat
Suite block between its "Suite configuration" markers, copied from the hub
(https://statcat-suite.github.io/sites/); change it there first. The guide
pages restate behaviour, so a change to a seam, a route or `create_app`
updates the page that describes it.

## Related repos

- `../sdmxlib` — the domain model and `BundleRegistry` this sits on top of.
- `../statcatlib` (formerly pinax) — `Catalog` bundle format a consumer's
  provider may open. sdmxlib-rest must never depend on it.
