# Annotations and overlays

A deployment can change the annotations it serves without rebuilding its
bundle, for example to change a display hint. The structure cache and the ETag
must then change with them, or clients keep the old annotations: the cache
serves the old body, and a client revalidating with `If-None-Match` gets a
304 for it.

## Fingerprints

An annotator whose output can change while the service runs implements
`SupportsFingerprint` beside `MessageAnnotator`:

```python
class SupportsFingerprint(Protocol):
    def fingerprint(self) -> str: ...
```

The fingerprint changes whenever `annotate` would produce different output.
The runtime reads it on every request, so return a stored value or a version
counter and do no I/O there.

The runtime folds it into one token:

```text
effective_token = sha256("v2" + "\n" + token + "\n" + fingerprint)
```

The `v2` line is the one sdmxlib's `effective_token` starts its hashed text
with, so an `OverlayAnnotator`'s token matches sdmxlib's exactly (below).

With an empty fingerprint, `effective_token` equals `token`, and the ETags
match a service with no annotator.

| Key | Token |
| --- | --- |
| `StructureQuery.token` (structure cache) | `effective_token` |
| `SpliceContext.token` | `effective_token` |
| The ETag | `effective_token` |
| `AvailabilityKey.token` | `token`: annotations cannot change availability |
| `annotate(token=)` | `token`, so an annotator can memoise per publication |

`Last-Modified` moves too. With a fingerprint, it is the later of the
publication time and the moment this process first saw the current
fingerprint, and at least one second past its previous value, since HTTP dates
have whole seconds. A restart counts as a change, so clients refetch once
after one.

`CompositeMessageAnnotator` runs several annotators in order. Its fingerprint
combines theirs and is empty while none has one.

## `OverlayAnnotator`

An sdmxlib overlay (`sdmxlib.bundle.Overlay`) is a versioned document of
entries that set, add or remove annotations of one type on the artefacts and
items a selector picks. It changes annotations only: never ids, codes,
structure or data. sdmxlib's
[bundle format](https://statcat-suite.github.io/sdmxlib/bundle/) specifies it.

`OverlayAnnotator` applies overlays to every structure message:

```python
from sdmxlib_rest.overlays import OverlayAnnotator

annotator = OverlayAnnotator.from_files(["overlays/display.json"])
runtime = CatalogRuntime(BundleProvider("path/to/bundle/<token>"), message_annotator=annotator)
```

`merge=` maps an annotation type to a `sdmxlib.bundle.MergeHook`, which
decides how a `set` entry combines with the annotations already present; the
default replaces them. A hook carries a package, a name and a version, and the
token covers them, so bump the version whenever the hook's output changes.

`audience=` applies the entries scoped to that audience and the unscoped ones;
without it every entry applies. An overlay that changes a name or description
(`texts`, overlay format 1.2) is refused with `OverlayError` when it is set:
sdmxlib applies those only with `fold_overlays`, into a new publication.

Its fingerprint is the hooks line and the overlays' digests as applied to the
audience, one per line in apply order, so `effective_token` equals sdmxlib's
`effective_token(manifest, overlays, merge=..., audience=...)`. A tool that
computes the token with sdmxlib predicts the service's ETag.

To change overlays on a running service, call `replace`, from an admin
endpoint or a file watcher the consumer owns:

```python
annotator.replace([sdmxlib.bundle.read_overlay(path) for path in paths])
```

A request already in flight may pair the new body with the old ETag. The next
request carries the new ETag, so a client holds a stale body for at most one
revalidation.

### Cost

Each request the structure cache does not answer copies every artefact an
overlay entry selects. A `*` selector, or one naming a class, that reaches
the codes of a very large codelist pays that copy on each such request. Keep
selectors specific, or fold a settled overlay into the bundle with
`sdmxlib.bundle.fold_overlays`, which writes a new publication.
