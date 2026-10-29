"""Per-app registry runtime + FastAPI dependencies.

Rather than a module-global cache of the opened registry, a `CatalogRuntime`
holds one lazily-opened `OpenedCatalog` and
is stored on ``app.state.rest_runtime``. Sub-apps mounted under a parent
(e.g. a consumer's forwarder surfaces) share the parent's runtime via
`share_runtime`, so one registry open serves every surface — and two
independent registries can coexist in one process, each on its own app.

The request-scoped dependencies (`get_runtime`, `get_registry`,
`get_meta_conn`) read ``request.app.state.rest_runtime``. Consumers
that need the runtime outside a request (lifespan warmers, ASGI
middleware) hold the `CatalogRuntime` instance directly.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from attrs import define, field
from fastapi import Request

from sdmxlib_rest.availability_values import NullAvailabilityValues
from sdmxlib_rest.filter_shaper import NullComponentFilterShaper
from sdmxlib_rest.message_annotator import NullMessageAnnotator, annotator_fingerprint
from sdmxlib_rest.metadata_store import NullMetadataStore
from sdmxlib_rest.splice import SpliceRegistry
from sdmxlib_rest.structure_cache import NullStructureCache

if TYPE_CHECKING:
    import duckdb
    import sdmxlib as sl
    from starlette.applications import Starlette

    from sdmxlib_rest.availability_values import AvailabilityValues
    from sdmxlib_rest.filter_shaper import ComponentFilterShaper
    from sdmxlib_rest.message_annotator import MessageAnnotator
    from sdmxlib_rest.metadata_store import MetadataStore
    from sdmxlib_rest.provider import CatalogProvider, OpenedCatalog
    from sdmxlib_rest.structure_cache import StructureCache


@define
class CatalogRuntime:
    """Owns one lazily-opened registry for the lifetime of an app.

    ``ensure_open`` is idempotent — the first call runs the provider, every
    subsequent call returns the cached handle. Not thread-safe on the very
    first open; callers warm it during single-threaded lifespan startup
    (or accept that two concurrent cold requests may both open, last wins),
    matching the previous ``_state``-dict semantics.

    ``splices`` is the canonical-surface `SpliceRegistry` the structure
    route runs on its serialised JSON (default: empty → the runner still
    applies its built-in URN injection). The consumer installs its registry
    here; it is uniform across the parent app and its shared mounts (a
    forwarder's add-on splices are layered separately by it).
    """

    provider: CatalogProvider
    splices: SpliceRegistry = field(factory=SpliceRegistry)
    structure_cache: StructureCache = field(factory=NullStructureCache)
    metadata_store: MetadataStore = field(factory=NullMetadataStore)
    availability_values: AvailabilityValues = field(factory=NullAvailabilityValues)
    filter_shaper: ComponentFilterShaper = field(factory=NullComponentFilterShaper)
    message_annotator: MessageAnnotator = field(factory=NullMessageAnnotator)
    _opened: OpenedCatalog | None = field(default=None, init=False)
    # The annotator fingerprint `last_modified` last saw, and when it first saw
    # it. A benign race: two requests seeing a new fingerprint at once both
    # record "now".
    _fingerprint_seen: str = field(default="", init=False)
    _fingerprint_since: datetime | None = field(default=None, init=False)

    def ensure_open(self) -> OpenedCatalog:
        """Open the registry on first use; return the cached handle thereafter."""
        if self._opened is None:
            self._opened = self.provider.open()
        return self._opened

    @property
    def is_open(self) -> bool:
        """True once `ensure_open` has run and the registry is live."""
        return self._opened is not None

    @property
    def registry(self) -> sl.BundleRegistry:
        """The registry (opening it if needed)."""
        return self.ensure_open().registry

    @property
    def meta_conn(self) -> duckdb.DuckDBPyConnection:
        """The shared read-only ``meta.duckdb`` connection (opening if needed)."""
        return self.ensure_open().meta_conn

    @property
    def manifest(self) -> object | None:
        """The opaque build manifest supplied by the provider (opening if needed)."""
        return self.ensure_open().manifest

    @property
    def build_id(self) -> str:
        """Build id of the open bundle, or ``""`` if not open (does NOT force open).

        Non-forcing on purpose: ETag / cache-key callers want "the identity if
        the registry is up, else skip" — the empty string is the skip signal,
        mirroring the old ``get_manifest() is None`` guard.
        """
        return self._opened.build_id if self._opened is not None else ""

    @property
    def built_at(self) -> str:
        """ISO-8601 build timestamp of the open bundle, or ``""`` if not open (non-forcing)."""
        return self._opened.built_at if self._opened is not None else ""

    @property
    def token(self) -> str:
        """What every cache and the ETag key on, or ``""`` if not open (non-forcing).

        The provider's ``token`` if it set one, else the sdmxlib bundle
        manifest's (new at every publication), else ``build_id``.
        """
        if self._opened is None:
            return ""
        if self._opened.token:
            return self._opened.token
        manifest = self._opened.registry.manifest
        return manifest.token if manifest is not None else self._opened.build_id

    @property
    def effective_token(self) -> str:
        """``token`` with the annotator's fingerprint folded in, or ``""`` if not open (non-forcing).

        What the structure cache, the splices and the ETag key on (GH-2). Equal
        to ``token`` while the annotator has no fingerprint, so ETags stay
        byte-identical for a consumer that never sets one. The folding mirrors
        sdmxlib's ``effective_token`` for overlays: a sha256 over the lines
        ``v2``, the token and the fingerprint. `AvailabilityKey` keeps the bare
        ``token``: annotations cannot change availability.
        """
        token = self.token
        fingerprint = annotator_fingerprint(self.message_annotator)
        if not token or not fingerprint:
            return token
        return hashlib.sha256(f"v2\n{token}\n{fingerprint}".encode()).hexdigest()

    @property
    def last_modified(self) -> str:
        """ISO-8601 ``Last-Modified`` source, or ``""`` if not open (non-forcing).

        Follows ``token``: when the token is the sdmxlib manifest's, so is the
        timestamp. A provider's ``built_at`` can stay put across a republish
        that minted a new token, and an ``If-Modified-Since`` would then 304 a
        stale body that the ETag correctly misses.

        With an annotator fingerprint it is the later of that and the moment
        this process first saw the current fingerprint, so an annotation change
        moves it too. A restart counts as a change: clients refetch once rather
        than risk a 304 for annotations they never saw.
        """
        if self._opened is None:
            return ""
        published = self._published_at()
        fingerprint = annotator_fingerprint(self.message_annotator)
        if not fingerprint:
            return published
        if fingerprint != self._fingerprint_seen or self._fingerprint_since is None:
            now = datetime.now(UTC)
            previous = self._fingerprint_since
            # HTTP dates have whole seconds, so a change inside the second the
            # previous fingerprint was first seen must still move the header.
            if previous is not None and now < previous + timedelta(seconds=1):
                now = previous + timedelta(seconds=1)
            self._fingerprint_seen = fingerprint
            self._fingerprint_since = now
        since = self._fingerprint_since
        try:
            published_at = datetime.fromisoformat(published)
        except ValueError:
            return since.isoformat()
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        return max(published_at, since).isoformat()

    def _published_at(self) -> str:
        """The publication timestamp `last_modified` starts from."""
        assert self._opened is not None  # noqa: S101 — callers check first
        if self._opened.token:
            return self._opened.built_at
        manifest = self._opened.registry.manifest
        return manifest.created_at.isoformat() if manifest is not None else self._opened.built_at

    def close(self) -> None:
        """Release the opened registry, if any; the runtime can be re-opened after."""
        if self._opened is not None:
            self._opened.close()
            self._opened = None


def attach_runtime(app: Starlette, runtime: CatalogRuntime) -> None:
    """Store ``runtime`` on ``app.state.rest_runtime`` (the request-dep lookup key)."""
    app.state.rest_runtime = runtime


def share_runtime(parent_app: Starlette, sub_app: Starlette) -> None:
    """Point a mounted sub-app at its parent's runtime so one open serves both."""
    sub_app.state.rest_runtime = parent_app.state.rest_runtime


def get_runtime(request: Request) -> CatalogRuntime:
    """FastAPI dependency: the `CatalogRuntime` on this request's app."""
    runtime = request.app.state.rest_runtime
    assert isinstance(runtime, CatalogRuntime)  # noqa: S101
    return runtime


def get_registry(request: Request) -> sl.BundleRegistry:
    """FastAPI dependency: the process/app-wide registry."""
    return get_runtime(request).registry


def get_meta_conn(request: Request) -> duckdb.DuckDBPyConnection:
    """FastAPI dependency: the shared read-only ``meta.duckdb`` connection."""
    return get_runtime(request).meta_conn


def get_splices(request: Request) -> SpliceRegistry:
    """FastAPI dependency: the canonical structure `SpliceRegistry` for this app."""
    return get_runtime(request).splices


def get_build_id(request: Request) -> str:
    """FastAPI dependency: the open bundle's build id (``""`` before open)."""
    return get_runtime(request).build_id


def get_token(request: Request) -> str:
    """FastAPI dependency: what caches key on for the open bundle (``""`` before open)."""
    return get_runtime(request).token


def get_effective_token(request: Request) -> str:
    """FastAPI dependency: ``token`` with the annotator's fingerprint folded in."""
    return get_runtime(request).effective_token


def get_structure_cache(request: Request) -> StructureCache:
    """FastAPI dependency: the consumer's structure-response cache for this app."""
    return get_runtime(request).structure_cache


def get_metadata_store(request: Request) -> MetadataStore:
    """FastAPI dependency: the consumer's reference-metadata store for this app."""
    return get_runtime(request).metadata_store


def get_availability_values(request: Request) -> AvailabilityValues:
    """FastAPI dependency: the consumer's precomputed availability values for this app."""
    return get_runtime(request).availability_values


def get_filter_shaper(request: Request) -> ComponentFilterShaper:
    """FastAPI dependency: the consumer's component-filter shaper for this app."""
    return get_runtime(request).filter_shaper


def get_message_annotator(request: Request) -> MessageAnnotator:
    """FastAPI dependency: the consumer's pre-serialise message annotator for this app."""
    return get_runtime(request).message_annotator
