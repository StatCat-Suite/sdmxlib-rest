"""`/availability` over a flow whose ``GEO_LEVEL`` lives on a series-attributes sidecar.

The DSL decides per query whether the sidecar join is needed (sdmxlib#388),
which replaced this route's own search of its WHERE fragments for sidecar
column names (#3). The case that search existed for is pinned here as
behaviour instead: a `ComponentFilterShaper` constraining a sidecar column the
request never named, as a partition-pushing shaper does with ``GEO_LEVEL``.

A sidecar with more than one row per join key would multiply every joined
count (#8). sdmxlib refuses one when the flow's query root is built
(sdmxlib#412), and the route turns that into a 500 naming the table.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import duckdb
import pytest
import sdmxlib as sl
from fastapi.testclient import TestClient

from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.filter_shaper import FilterContext
from sdmxlib_rest.runtime import CatalogRuntime
from tests.conftest import AGENCY, World, _concept, _WorldProvider, build_world, istr

pytestmark = pytest.mark.unit

FLOW = "/availability/dataflow/AG/DF_SIDE/1.0"
KEY_DIMS = ["REF_AREA", "SEX", "TIME_PERIOD"]

#: CA is a country and US a province, so a GEO_LEVEL filter is visible in the answer.
LEVELS = {"CA": "Country", "US": "Province"}


def _sidecar(fan: int) -> Callable[[duckdb.DuckDBPyConnection], None]:
    """A sidecar with ``fan`` rows per area, all carrying that area's level."""

    def write(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute("CREATE TABLE lake.side (geo VARCHAR, geo_level VARCHAR, vintage VARCHAR)")
        rows = ", ".join(f"('{area}', '{level}', '20{n:02d}')" for area, level in LEVELS.items() for n in range(fan))
        conn.execute(f"INSERT INTO lake.side VALUES {rows}")

    return write


def _side_flow(world: World) -> None:
    """``DF_SIDE``: the shared observations, with ``GEO_LEVEL`` declared and bound to the sidecar.

    Its own DSD, because the DSL resolves components against the structure:
    a ``GEO_LEVEL`` in the binding alone is not a component it will filter on.
    """
    registry = world.registry
    registry.add(
        sl.DataStructure(
            id="DSD_SIDE",
            maintainer=AGENCY,
            version="1.0",
            name=istr("Population, with geography attributes"),
            dimensions=sl.ItemList(
                [
                    sl.Dimension(id="REF_AREA", concept=_concept(registry, "REF_AREA")),
                    sl.Dimension(id="SEX", concept=_concept(registry, "SEX")),
                ]
            ),
            time_dimension=sl.TimeDimension(id="TIME_PERIOD", concept=_concept(registry, "TIME_PERIOD")),
            attributes=sl.ItemList([sl.Attribute(id="GEO_LEVEL", concept=_concept(registry, "GEO_LEVEL"))]),
            measures=sl.ItemList([sl.Measure(id="OBS_VALUE", concept=_concept(registry, "OBS_VALUE"))]),
        )
    )
    flow_urn = sl.Dataflow.urn_for(agency="AG", id="DF_SIDE", version="1.0")
    registry.add(
        sl.Dataflow(
            id="DF_SIDE",
            maintainer=AGENCY,
            version="1.0",
            name=istr("Population, with a sidecar"),
            structure=registry.ref(sl.DataStructure.urn_for(agency="AG", id="DSD_SIDE", version="1.0")),
        )
    )
    registry.add(
        sl.DataflowBinding(
            dataflow=registry.ref(flow_urn),
            table_location="obs",
            series_attributes_location="side",
            database_role="lake",
            dimension_column_map={"REF_AREA": "geo", "SEX": "sex", "TIME_PERIOD": "period"},
            measure_column_map={"OBS_VALUE": "val"},
            attribute_column_map={"GEO_LEVEL": "geo_level"},
            structural_version="1.0",
            source_registry="test",
            materialized_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            bound_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
    )


@contextmanager
def _client(*, fan: int = 1, **runtime: Any) -> Iterator[TestClient]:
    world = build_world(extra_lake=_sidecar(fan))
    _side_flow(world)
    app = create_app(
        CatalogRuntime(_WorldProvider(world), **runtime),
        RestSettings(title="test-sdmx-api", version="0.1.0"),
        routers=list(library_routers()),
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        world.registry.close()


def _values(payload: dict[str, Any], component_id: str) -> set[str]:
    region = payload["data"]["dataConstraints"][0]["cubeRegions"][0]
    return {v for kv in region["keyValues"] if kv["id"] == component_id for v in kv["values"]}


def _obs_count(payload: dict[str, Any]) -> int:
    constraint = payload["data"]["dataConstraints"][0]
    (count,) = [a["title"] for a in constraint["annotations"] if a["id"] == "obs_count"]
    return int(count)


class OnlyCountries:
    """A shaper that adds ``GEO_LEVEL=Country``, which no request names: a partition push."""

    def shape(self, component_filter: sl.ComponentFilter, ctx: FilterContext) -> sl.ComponentFilter:
        return sl.ComponentFilter(
            dimension_ids=component_filter.dimension_ids,
            dimensions=component_filter.dimensions,
            attributes={**component_filter.attributes, "GEO_LEVEL": ["Country"]},
        )


# ── The join, when a query needs it ──────────────────────────────────────────


def test_a_sidecar_filter_narrows_the_answer() -> None:
    with _client() as client:
        resp = client.get(f"{FLOW}/all/REF_AREA", params={"c[GEO_LEVEL]": "Country"})
    assert resp.status_code == 200, resp.text
    assert _values(resp.json(), "REF_AREA") == {"CA"}
    assert _obs_count(resp.json()) == 2


def test_a_sidecar_column_can_be_the_component_asked_about() -> None:
    with _client() as client:
        resp = client.get(f"{FLOW}/all/GEO_LEVEL")
    assert resp.status_code == 200, resp.text
    assert _values(resp.json(), "GEO_LEVEL") == {"Country", "Province"}


@pytest.mark.parametrize("component", ["REF_AREA", "all"])
def test_a_shaper_added_sidecar_constraint_is_honoured(component: str) -> None:
    """The case #3's fragment search existed for, now pinned as behaviour.

    The request names no sidecar column; the shaper adds one. A route that
    decided the join from the request alone would answer for both areas.
    """
    with _client(filter_shaper=OnlyCountries()) as client:
        resp = client.get(f"{FLOW}/all/{component}")
    assert resp.status_code == 200, resp.text
    assert _values(resp.json(), "REF_AREA") == {"CA"}
    assert _obs_count(resp.json()) == 2


def test_a_one_to_one_sidecar_does_not_change_the_count() -> None:
    with _client() as client:
        resp = client.get(f"{FLOW}/all/SEX")
    assert resp.status_code == 200, resp.text
    assert _obs_count(resp.json()) == 3


# ── A sidecar that would multiply rows (#8) ──────────────────────────────────


def test_a_fan_out_sidecar_is_refused_naming_the_table() -> None:
    """Three sidecar rows per area would have reported 9 observations out of 3."""
    with _client(fan=3) as client:
        resp = client.get(f"{FLOW}/all/SEX", params={"c[GEO_LEVEL]": "Country"})
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "DF_SIDE" in detail
    assert "lake.side" in detail, "the error must name the table the producer has to fix"


def test_a_fan_out_sidecar_refuses_queries_that_would_not_join_too() -> None:
    """sdmxlib refuses the flow when its query root is built, not when a query joins.

    So an unfiltered count, which never touches the sidecar and would be
    correct, is refused as well. Pinned so a change upstream is noticed.
    """
    with _client(fan=3) as client:
        resp = client.get(f"{FLOW}/all/SEX")
    assert resp.status_code == 500
