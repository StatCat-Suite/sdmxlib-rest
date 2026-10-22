"""A populated catalog the route tests can drive.

The suite this file joins asserted on *assembly* — does the app boot, is the
middleware in order — because the only catalog available was empty. Nothing
exercised the three library routes against real artefacts, so the whole
structure/metadata/availability surface was uncovered. This fixture closes
that, and exists so the sdmxlib 0.48 -> 0.62 migration has something to be
safe against: every test here is written against current behaviour and must
stay green through the port.

Two choices are load-bearing for that second job:

**A real ``Registry``, not ``InMemoryRegistry``.** The wildcard enumerator
asserts ``isinstance(registry, sl.LocalRegistry)`` — the concrete DuckDB class, not
a Protocol — so an in-memory registry 500s on every ``all`` route. The DuckDB
registry is also what production runs and what returns the ``Lazy*`` wrappers
the stub fast path is built around, so the tests exercise the real shapes.

**A real bundle: ``meta.duckdb`` + ``lake.ducklake`` + ``parquet/``.**
``/availability`` asks sdmxlib's query DSL for its answers (#6), and the DSL
reads observations through the registry's own data plane, which ATTACHes the
lake when something first asks for rows. A ``lake`` schema created on a side
connection is invisible to it, so the fixture is the layout every consumer
actually serves, opened with `LocalRegistry.bundle`.

The world is deliberately small but not degenerate: two codelists, a DSD with
two coded dimensions plus a time dimension, a dataflow, a binding that renames
every physical column, and three observations. The renames matter — a binding
whose SDMX ids and column names coincide would hide every id/column confusion.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
import sdmxlib as sl
from attrs import define
from fastapi.testclient import TestClient

from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.provider import OpenedCatalog
from sdmxlib_rest.runtime import CatalogRuntime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI

AGENCY = sl.Agency(id="AG", name=sl.InternationalString(en="Test Agency"))

BUILD_ID = "testbuild"
BUILT_AT = "2026-01-01T00:00:00Z"


def istr(text: str) -> sl.InternationalString:
    """An English-only :class:`sl.InternationalString` — the fixtures' only language."""
    return sl.InternationalString(en=text)


@define
class World:
    """The fixture catalog plus the handles a test needs to assert against it.

    Attributes:
        registry: The registry the app serves — structural, bindings and data.
        conn: The registry's own connection, handed to the routes as the meta
            connection. The lake is attached to it lazily, on first observation query.
        flow_urn: URN of the one dataflow, the subject of most route tests.
    """

    registry: sl.LocalRegistry
    conn: duckdb.DuckDBPyConnection
    flow_urn: sl.SdmxUrn[sl.Dataflow]


def _codelists(registry: sl.LocalRegistry) -> None:
    registry.add(
        sl.Codelist(
            id="CL_GEO",
            maintainer=AGENCY,
            version="1.0",
            name=istr("Geography"),
            codes=sl.ItemList(
                [
                    sl.Code(id="CA", name=istr("Canada")),
                    sl.Code(id="US", name=istr("United States")),
                ]
            ),
        )
    )
    registry.add(
        sl.Codelist(
            id="CL_SEX",
            maintainer=AGENCY,
            version="1.0",
            name=istr("Sex"),
            codes=sl.ItemList(
                [
                    sl.Code(id="M", name=istr("Male")),
                    sl.Code(id="F", name=istr("Female")),
                ]
            ),
        )
    )


def _concepts(registry: sl.LocalRegistry) -> None:
    """One concept per component.

    Not decoration: every SDMX wire format rejects a component without a
    ConceptIdentity, so a DSD whose dimensions have no ``concept`` cannot be
    serialised at all.
    """
    registry.add(
        sl.ConceptScheme(
            id="CS_POP",
            maintainer=AGENCY,
            version="1.0",
            name=istr("Population concepts"),
            concepts=sl.ItemList(
                [
                    sl.Concept(id="REF_AREA", name=istr("Reference area")),
                    sl.Concept(id="SEX", name=istr("Sex")),
                    sl.Concept(id="TIME_PERIOD", name=istr("Time period")),
                    sl.Concept(id="OBS_VALUE", name=istr("Observation value")),
                ]
            ),
        )
    )


def _concept(registry: sl.LocalRegistry, concept_id: str) -> sl.Ref[sl.Concept]:
    """A Ref to one concept in ``CS_POP``, by its item URN.

    Built with ``Concept.urn_for``, not by appending the id to the scheme's URN.
    The appended form reads ``ConceptScheme=AG:CS_POP(1.0).REF_AREA`` — an item
    URN carrying its scheme's class name — and SDMX-ML 2.1's reader rejects it as
    schema-invalid, while JSON readers accept it. Every route test passed with
    that fixture for months because none of them parsed an XML response back; the
    round trip in ``test_round_trip.py`` was the first thing to.
    """
    return registry.ref(sl.Concept.urn_for(agency="AG", scheme="CS_POP", id=concept_id, version="1.0"))


def _structure(registry: sl.LocalRegistry) -> None:
    """Add the DSD and the dataflow that points at it."""
    geo = registry.ref(sl.Codelist.urn_for(agency="AG", id="CL_GEO", version="1.0"))
    sex = registry.ref(sl.Codelist.urn_for(agency="AG", id="CL_SEX", version="1.0"))
    registry.add(
        sl.DataStructure(
            id="DSD_POP",
            maintainer=AGENCY,
            version="1.0",
            name=istr("Population structure"),
            dimensions=sl.ItemList(
                [
                    sl.Dimension(id="REF_AREA", representation=geo, concept=_concept(registry, "REF_AREA")),
                    sl.Dimension(id="SEX", representation=sex, concept=_concept(registry, "SEX")),
                ]
            ),
            time_dimension=sl.TimeDimension(id="TIME_PERIOD", concept=_concept(registry, "TIME_PERIOD")),
            measures=sl.ItemList([sl.Measure(id="OBS_VALUE", concept=_concept(registry, "OBS_VALUE"))]),
        )
    )
    registry.add(
        sl.Dataflow(
            id="DF_POP",
            maintainer=AGENCY,
            version="1.0",
            name=istr("Population"),
            structure=registry.ref(sl.DataStructure.urn_for(agency="AG", id="DSD_POP", version="1.0")),
        )
    )


def _binding(registry: sl.LocalRegistry, flow_urn: sl.SdmxUrn[sl.Dataflow]) -> None:
    """Bind the flow to ``lake.obs``, renaming every column on the way.

    Every SDMX id maps to a different physical name so that a test asserting
    on ``REF_AREA`` cannot pass by accidentally reading the ``geo`` column.
    """
    registry.add(
        sl.DataflowBinding(
            dataflow=registry.ref(flow_urn),
            table_location="obs",
            database_role="lake",
            dimension_column_map={"REF_AREA": "geo", "SEX": "sex", "TIME_PERIOD": "period"},
            measure_column_map={"OBS_VALUE": "val"},
            attribute_column_map={},
            structural_version="1.0",
            source_registry="test",
            materialized_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            bound_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
    )


def _observations(conn: duckdb.DuckDBPyConnection) -> None:
    """Three rows: CA has both sexes in 2020, US only male in 2021.

    Asymmetric on purpose — a filtered availability query returns a strictly
    smaller value set than the unfiltered one, so a filter that silently
    failed to apply would still change the assertion.
    """
    conn.execute("CREATE TABLE lake.obs (geo VARCHAR, sex VARCHAR, period VARCHAR, val DOUBLE)")
    conn.execute("INSERT INTO lake.obs VALUES ('CA','M','2020',1.0),('CA','F','2020',2.0),('US','M','2021',3.0)")


def _lake(root: Path, extra: Callable[[duckdb.DuckDBPyConnection], None] | None) -> None:
    """Write the bundle's DuckLake catalog and parquet before the registry opens it.

    Written on a connection of its own and closed again, because the registry
    attaches the same catalog itself; ``extra`` lets a test add tables of its
    own (a sidecar, say) while the lake is still writable here.
    """
    conn = duckdb.connect()
    conn.execute("INSTALL ducklake")
    conn.execute("LOAD ducklake")
    conn.execute(f"ATTACH 'ducklake:{root / 'lake.ducklake'}' AS lake (DATA_PATH '{root / 'parquet'}/')")
    _observations(conn)
    if extra is not None:
        extra(conn)
    conn.close()


def _metadata(registry: sl.LocalRegistry) -> None:
    """An MSD, a Metadataflow onto it, and one MetadataSet reported against it.

    Reference metadata is a separate resource family from the structural
    artefacts above and is reached by its own routes, so it needs its own
    artefacts — ``/metadata`` resolves a MetadataSet by URN and never consults
    the DSD or the dataflow.
    """
    registry.add(sl.MetadataStructure(id="MSD_INFO", maintainer=AGENCY, version="1.0", name=istr("Info MSD")))
    msd_ref = registry.ref(sl.MetadataStructure.urn_for(agency="AG", id="MSD_INFO", version="1.0"))
    registry.add(
        sl.Metadataflow(
            id="MDF_INFO",
            maintainer=AGENCY,
            version="1.0",
            name=istr("Info metadataflow"),
            structure=msd_ref,
        )
    )
    registry.add(
        sl.MetadataSet(
            id="MDS_1",
            maintainer=AGENCY,
            # SDMX-JSON requires a semantic version on a MetadataSet; SDMX-ML
            # states no such pattern, so "1.0.0" is the spelling that satisfies
            # both wire formats.
            version="1.0.0",
            name=istr("Info set"),
            # A set references the *flow*, never the MSD — SDMX-ML 3.0 and
            # SDMX-JSON both model that as a choice of flow or provision
            # agreement, with no element for a structure.
            structure=registry.ref(sl.Metadataflow.urn_for(agency="AG", id="MDF_INFO", version="1.0")),
            # What the metadata is *about*. Required 1..n by both formats.
            targets=[registry.ref(sl.Dataflow.urn_for(agency="AG", id="DF_POP", version="1.0"))],
        )
    )


def build_world(
    extra_lake: Callable[[duckdb.DuckDBPyConnection], None] | None = None,
    codelists: Callable[[sl.LocalRegistry], None] | None = None,
) -> World:
    """A populated catalog over a real bundle in a temp directory.

    Args:
        extra_lake: Runs against the lake before the registry opens it, for a
            test that needs tables beyond ``lake.obs``.
        codelists: Adds ``CL_GEO`` and ``CL_SEX`` in place of the default
            ones, for a test that needs richer codelists.
    """
    root = Path(tempfile.mkdtemp()) / "bundle"
    root.mkdir()
    _lake(root, extra_lake)
    registry = sl.LocalRegistry.bundle(root)
    (codelists or _codelists)(registry)
    _concepts(registry)
    _structure(registry)
    _metadata(registry)
    flow_urn = sl.Dataflow.urn_for(agency="AG", id="DF_POP", version="1.0")
    _binding(registry, flow_urn)
    return World(registry=registry, conn=registry.connection, flow_urn=flow_urn)


@pytest.fixture
def world() -> Iterator[World]:
    """A populated catalog over a real bundle: registry, bindings and observations."""
    built = build_world()
    yield built
    built.registry.close()


class _WorldProvider:
    """A ``CatalogProvider`` handing out one already-built :class:`World`."""

    def __init__(self, world: World) -> None:
        self._world = world

    def open(self) -> OpenedCatalog:
        return OpenedCatalog(
            registry=self._world.registry,
            meta_conn=self._world.conn,
            manifest=None,
            build_id=BUILD_ID,
            built_at=BUILT_AT,
        )


def build_app(world: World) -> FastAPI:
    """The library's own app over ``world`` — every seam left at its default.

    Defaults are the point: a consumer that installs no seam must get a
    correct service, so the routes are exercised through the inert
    implementations rather than test doubles.
    """
    return create_app(
        CatalogRuntime(_WorldProvider(world)),
        RestSettings(title="test-sdmx-api", version="0.1.0"),
        routers=list(library_routers()),
    )


@pytest.fixture
def client(world: World) -> Iterator[TestClient]:
    """A ``TestClient`` over the library app, catalog already open."""
    with TestClient(build_app(world)) as test_client:
        yield test_client
