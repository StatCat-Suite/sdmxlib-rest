# Seams

A seam is a Protocol that `CatalogRuntime` carries and the library calls at a
fixed point in a request. Install a seam by passing it to the runtime:

```python
runtime = CatalogRuntime(
    BundleProvider("path/to/bundle"),
    structure_cache=MyStructureCache(),
    message_annotator=MyAnnotator(),
)
```

Every seam except the provider has a default, and a service with only the
defaults answers every request correctly. A seam exists because a production
service needed it, so each one below names the case it serves.

## `CatalogProvider`

Opens the registry. [Serving a bundle](serving.md#the-provider) covers it.

## `StructureCache`

Caches rendered structure responses. The structure route asks `lookup` before
it renders and offers the SDMX-JSON response to `store` after. SDMX-ML
responses are looked up and not yet stored, and an empty SDMX-JSON result is
offered as its 204
([GH-74](https://github.com/StatCat-Suite/sdmxlib-rest/issues/74)).

```python
class StructureCache(Protocol):
    def lookup(self, query: StructureQuery) -> Response | None: ...
    def store(self, query: StructureQuery, response: Response) -> None: ...
```

`StructureQuery` holds the resolved request: structure type, agency, id,
version after `latest` is resolved, `detail`, `references`, `Accept`,
`Accept-Language` and `token`. The token is the runtime's `effective_token`,
so a new publication or an annotation change misses every entry. The
consumer decides what is worth keeping; a full codelist of 580,000 codes
takes about 5 s to render and is the usual candidate.

Default: `NullStructureCache`, which never hits and keeps nothing.

## `AvailabilityValues`

Supplies precomputed `/availability` answers. The route asks `lookup` before
it scans and offers the scanned values to `store` after. It asks only for
unfiltered requests: a request with a key, a `c[]` filter or a period bound
always scans, because no producer can precompute every filter.

```python
class AvailabilityValues(Protocol):
    def lookup(self, key: AvailabilityKey) -> ResolvedAvailability | None: ...
    def store(self, key: AvailabilityKey, resolved: ResolvedAvailability) -> None: ...
```

`AvailabilityKey` is the bare `token`, the resolved dataflow URN and the
component id (`all` or one id). It keys on the flow URN, so a consumer's
SDMX-REST 1.5 forwarder and the 2.2.2 routes share an entry. The library
renders the response from the values either way, so a precomputed answer
negotiates format and language like a scanned one.

Default: `NullAvailabilityValues`, which always scans.

## `MetadataStore`

Answers which reference-metadata sets are attached to an artefact, for the
`/metadata` routes. The library then reads those sets from the registry.

```python
class MetadataStore(Protocol):
    def sets_for_structure(
        self, conn, *, structure_urn: str, provider_id: str | None, limit: int
    ) -> list[str]: ...
    def sets_for_target(
        self, conn, *, target_urn: str, item_pattern: str | None, limit: int
    ) -> list[str]: ...
```

Each returns metadata-set URNs, at most `limit`, read from the consumer's own
tables through `conn`.

Default: `NullMetadataStore`, which reports no attached sets.

## `ComponentFilterShaper`

Rewrites a parsed `ComponentFilter` before the library queries with it. A
consumer uses it for two kinds of change:

- **Semantics.** A vendor parameter such as `within[GEO]=<parent>` widens the
  filter to every code beneath that parent in a hierarchy the consumer knows.
- **Physics.** A consumer that knows its partition layout adds a redundant
  predicate that lets DuckDB prune files.

```python
class ComponentFilterShaper(Protocol):
    def shape(self, component_filter: sl.ComponentFilter, ctx: FilterContext) -> sl.ComponentFilter: ...
```

`FilterContext` carries the registry, the connection, the flow URN, its
`DataflowBinding` and every query parameter. A shaper returns a new filter
and raises `ValueError` or `KeyError` for a malformed parameter, which
becomes a 400.

Default: `NullComponentFilterShaper`, which returns the filter unchanged.

## `MessageAnnotator`

Adds annotations to a `StructureMessage` before it is written, so they reach
SDMX-JSON and SDMX-ML alike. The .Stat Data Explorer, for example, finds a
data structure's metadata structure through a `METADATA` annotation on it.

```python
class MessageAnnotator(Protocol):
    def annotate(self, message, *, registry, conn, token) -> StructureMessage: ...
```

An annotator whose output can change while the service runs also implements
`SupportsFingerprint`. [Annotations and overlays](annotations.md) covers
that, and `OverlayAnnotator`, which applies sdmxlib annotation overlays.

Default: `NullMessageAnnotator`, which returns the message unchanged.

## `SpliceRegistry`

Holds splices: functions that edit a structure response's JSON after it is
written. A splice takes the parsed body and a `SpliceContext` and returns
`True` if it changed the body.

```python
def add_contact(body: dict, ctx: SpliceContext) -> bool:
    ...
    return True

runtime.splices.structure.append(add_contact)
```

The route parses the body once, runs every splice, and re-encodes only if one
returned `True`. A splice may be `async`. `SpliceContext` carries the
request's artefact, `detail`, `references`, the registry, the connection and
the `effective_token`, so a splice that caches keys correctly. A splice that
rewrites the body into another format sets `ctx.media_override`.

Splices see JSON only. To change what every format carries, use a
`MessageAnnotator`.

Default: an empty registry.
