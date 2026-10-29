# Tokens and cache keys

Every cache the library keys derives from the publication's token, and so
does the ETag. A new publication mints a new token, so nothing keyed on the
old one is served again, and no cache needs an invalidation path. The ETag
also hashes the app version, so a release of the service changes every ETag
too.

## The token

`CatalogRuntime.token` is the provider's `OpenedCatalog.token` when it is set.
`OpenedCatalog.token` defaults to `""`, and the runtime then uses the bundle
manifest's `token`, the publication's directory name, or `build_id` when the
registry has no manifest (`runtime.py`).

## The effective token

A `MessageAnnotator` can change a response without a new publication, for
example when a deployment replaces an annotation overlay. Keyed on the token
alone, the structure cache would keep serving the old body, and a client
sending `If-None-Match` would get a 304 for it
([GitLab #12](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues/12)).

An annotator whose output can change implements `SupportsFingerprint`. The
runtime folds the fingerprint into the token:

```text
effective_token = sha256("v2\n" + token + "\n" + fingerprint)
```

An empty fingerprint leaves `effective_token` equal to `token`, so a service
with no annotator keeps the ETags it had. When the provider sets no token and
an `OverlayAnnotator` is the only annotator, the hashed text matches sdmxlib's
`effective_token`, so a tool that computes the token from a manifest and its
overlays with sdmxlib predicts the service's ETag. A
`CompositeMessageAnnotator` prefixes each member's fingerprint with its
position, and the two tokens then differ.

The runtime reads the fingerprint on every request, so a fingerprint is a
stored value or a counter and does no I/O.

## What keys on which

| Key | Token | Why |
| --- | --- | --- |
| ETag | `effective_token` | An annotation change changes the body |
| `StructureQuery.token` | `effective_token` | The cached body includes annotations |
| `SpliceContext.token` | `effective_token` | A splice that caches sees the annotated body |
| `AvailabilityKey.token` | `token` | Annotations cannot change which values occur |
| `annotate(token=)` | `token` | An annotator memoises per publication |

The ETag hashes the app version, the effective token, the path, the query
string, `Accept` and the resolved language. It never reads the body, so it is
computed before the route runs and is identical with or without compression.

`Last-Modified` has its own source. With a fingerprint it is the later of the
publication time and the moment this process first saw the fingerprint, so a
restart moves `Last-Modified` while the ETag holds.

## Open proposal

[GH-10](https://github.com/StatCat-Suite/sdmxlib-rest/issues/10) proposes
keying the caches and ETags on a content token, so that a new publication
whose content did not change keeps its ETags, and shipping a baker and
pluggable cache backends. It would change the first section of this page.
