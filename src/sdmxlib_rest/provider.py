"""The catalog-provider seam.

The library owns the HTTP shape of SDMX-REST over a
`sdmxlib.LocalRegistry`; it does NOT know how that registry is
opened. A consumer supplies a `CatalogProvider` whose `open`
returns an `OpenedCatalog` — the registry, a raw DuckDB connection,
and an opaque build manifest.

**The registry must reach its own observations.** ``/availability`` reads
rows through ``registry.obs(flow)``, sdmxlib's query DSL, which runs
on the registry's own connection and attaches the lake there itself. A
registry opened with ``LocalRegistry.bundle(path)`` does; so does statcatlib's
``Catalog``. A lake attached only to some other connection is invisible to it.
``meta_conn`` is no longer what any library route queries: it is handed to a
consumer's seams (``ComponentFilterShaper`` via ``FilterContext.conn``,
``MessageAnnotator``, ``MetadataStore``) for SQL of their own.

``LocalRegistry`` is named concretely rather than behind a Protocol because
``binding_for`` — which every data-plane helper needs — exists only there;
no Protocol in sdmxlib covers it. sdmxlib 0.48's ``FederatedCatalog`` was
equally concrete, so this is a like-for-like swap rather than a narrowing
of what a provider could already return.

``manifest`` is typed ``object | None`` on purpose: the library never
introspects it (only for equality-of-identity in ETag/cache keys via the
consumer's own accessor). Consumer code — its manifest reader, its response
splices, its ``/health`` payload — casts it back to the concrete type.

How a catalog is opened — bundle format, storage, lake — is the consumer's
provider implementing this protocol; nothing about it leaks into the library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from attrs import define, field

if TYPE_CHECKING:
    from collections.abc import Callable

    import duckdb
    import sdmxlib as sl


@define
class OpenedCatalog:
    """A live registry plus its meta connection and build manifest.

    Returned by `CatalogProvider.open`. ``close`` runs the
    provider-supplied teardown (e.g. closing a statcatlib ``Catalog``); it is
    a no-op when the provider passes none.
    """

    registry: sl.LocalRegistry
    meta_conn: duckdb.DuckDBPyConnection
    manifest: object | None = None
    # Build identity, from the provider's own manifest: ``build_id`` names the
    # bundle build (for logs), ``built_at`` is its ISO-8601 timestamp.
    build_id: str = ""
    built_at: str = ""
    # What caches and the ETag key on. Empty means the registry's sdmxlib
    # manifest token, else ``build_id``: a producer may keep ``build_id`` across
    # a metadata-only rebuild, but a publication always mints a new token. Set
    # it only to key on something other than the manifest.
    token: str = ""
    on_close: Callable[[], None] | None = field(default=None, repr=False)

    def close(self) -> None:
        """Run the provider's teardown callback, if any."""
        if self.on_close is not None:
            self.on_close()


@runtime_checkable
class CatalogProvider(Protocol):
    """Opens an `OpenedCatalog`. Implemented by the consumer.

    Called once per `CatalogRuntime` lifetime
    (lazily, on first use or at lifespan startup). Implementations own all
    storage-backend concerns — bundle layout, credentials, connection
    tuning — and must return a registry the generic routes can query plus
    a DuckDB connection for the custom-SQL data paths.
    """

    def open(self) -> OpenedCatalog:
        """Open (or re-open) the registry and return the live handle."""
        ...
