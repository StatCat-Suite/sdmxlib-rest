"""The dotted-key slot list — the one migration change with a silent failure mode.

Dotted-key parsing is positional: segment *i* means whatever component sits at
index *i* of the slot list. Get the list wrong and the key is not rejected, it
is *misread* — every value lands one slot over, and the response is a
confidently wrong answer rather than an error.

sdmxlib splits the slot list by REST generation, because the two disagree
about where the time dimension goes:

| API                | slots                          | time filtered by        |
| ------------------ | ------------------------------ | ----------------------- |
| ``2.1`` (REST 1.5) | dimensions, without time       | startPeriod / endPeriod |
| ``3.0`` (REST 2.2) | dimensions, then time          | a segment, or the above |

This library publishes 2.2.2 paths only, so it asks for the ``"3.0"`` list and
these tests pin that choice. Anything serving 1.5 URLs translates them in a
forwarder ahead of these routes.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sdmxlib as sl

from sdmxlib_rest.data.projection import key_dimension_ids

if TYPE_CHECKING:
    from tests.conftest import World

pytestmark = pytest.mark.unit


def test_the_slot_list_follows_dsd_order_not_binding_order(world: World) -> None:
    """The DSD is authoritative for order; the binding's map is only a fallback.

    Asserted against the fixture's binding, whose column map happens to be
    written in the same order — so this pins the *source*, and the next test
    pins that the two are not interchangeable.
    """
    binding = world.registry.binding_for(world.flow_urn)
    assert binding is not None

    assert key_dimension_ids(world.registry, world.flow_urn, binding) == [
        "REF_AREA",
        "SEX",
        "TIME_PERIOD",
    ]


def test_time_occupies_a_slot(world: World) -> None:
    """Time is a key slot, which is the SDMX-REST 2.2.2 shape this library serves.

    The counterpart matters: ``key_slot_ids("2.1")`` drops the trailing
    ``TIME_PERIOD``, so reading a key against the wrong list shifts every
    value one slot and answers confidently wrong rather than erroring.
    """
    binding = world.registry.binding_for(world.flow_urn)
    assert binding is not None

    slots = key_dimension_ids(world.registry, world.flow_urn, binding)
    assert slots[-1] == "TIME_PERIOD"
    assert len(slots) == 3


def test_a_key_is_parsed_positionally_against_those_slots(world: World) -> None:
    """The contract the whole table is about: segment *i* binds slot *i*."""
    binding = world.registry.binding_for(world.flow_urn)
    assert binding is not None
    slots = key_dimension_ids(world.registry, world.flow_urn, binding)

    parsed = sl.parse_components("CA.M.2020", {}, slots)

    assert parsed.dimension_ids == ["REF_AREA", "SEX", "TIME_PERIOD"]
    assert parsed.dimensions == [["CA"], ["M"], ["2020"]]


def test_an_empty_segment_leaves_its_slot_unconstrained(world: World) -> None:
    """``CA..2020`` constrains the outer two slots and wildcards the middle one."""
    binding = world.registry.binding_for(world.flow_urn)
    assert binding is not None
    slots = key_dimension_ids(world.registry, world.flow_urn, binding)

    parsed = sl.parse_components("CA..2020", {}, slots)

    assert parsed.dimensions == [["CA"], None, ["2020"]]


@pytest.mark.parametrize("key", ["CA.M", "CA.M.2020.EXTRA"])
def test_a_key_of_the_wrong_length_is_refused(world: World, key: str) -> None:
    """Length is checked against the slot list, which is why the list must be right.

    Both directions: ``CA.M`` is short for this three-slot DSD and
    ``CA.M.2020.EXTRA`` is long. Neither is silently padded or truncated.
    """
    binding = world.registry.binding_for(world.flow_urn)
    assert binding is not None
    slots = key_dimension_ids(world.registry, world.flow_urn, binding)

    with pytest.raises(ValueError, match="segments"):
        sl.parse_components(key, {}, slots)


def test_the_binding_column_map_is_the_fallback_order(world: World) -> None:
    """With no resolvable DSD the binding supplies the order, lossily but in order.

    Pinned because the fallback is easy to lose in the port: 0.62 moves the
    DSD lookup onto the registry, and a refactor that dropped this branch
    would only show up on flows whose DSD is missing.
    """
    binding = world.registry.binding_for(world.flow_urn)
    assert binding is not None
    unknown_flow = sl.Dataflow.urn_for(agency="AG", id="NO_SUCH_FLOW", version="1.0")

    assert key_dimension_ids(world.registry, unknown_flow, binding) == list(binding.dimension_column_map.keys())


def _registry_ordering(dimension_ids: list[str]) -> sl.LocalRegistry:
    """A registry whose ``AG:DF(1.0)`` is built on a DSD with ``dimension_ids`` in that order."""

    registry = sl.LocalRegistry(Path(tempfile.mkdtemp()) / "meta.duckdb").open()
    agency = sl.Agency(id="AG")

    def concept(cid: str) -> sl.SdmxUrn[sl.Concept]:
        return sl.Concept.urn_for(agency="AG", scheme="CS", id=cid, version="1.0")

    registry.add(
        sl.DataStructure(
            id="DSD",
            maintainer=agency,
            name=sl.InternationalString(en="d"),
            dimensions=sl.ItemList([sl.Dimension(id=d, concept=concept(d)) for d in dimension_ids]),
            measures=sl.ItemList([sl.Measure(id="OBS_VALUE", concept=concept("OBS_VALUE"))]),
        )
    )
    flow_urn = sl.Dataflow.urn_for(agency="AG", id="DF", version="1.0")
    registry.add(
        sl.Dataflow(
            id="DF",
            maintainer=agency,
            name=sl.InternationalString(en="f"),
            structure=registry.ref(sl.DataStructure.urn_for(agency="AG", id="DSD", version="1.0")),
        )
    )
    registry.add(
        sl.DataflowBinding(
            dataflow=registry.ref(flow_urn),
            table_location="obs",
            database_role="lake",
            dimension_column_map={d: d.lower() for d in sorted(dimension_ids)},
            measure_column_map={"OBS_VALUE": "val"},
            attribute_column_map={},
            structural_version="1.0",
            source_registry="test",
            materialized_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            bound_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
    )
    return registry


def test_a_reopened_bundle_gets_its_own_slot_order() -> None:
    """The memo is per registry: a rebuilt bundle may reorder a non-final DSD under the same URN.

    Keyed on the flow URN alone, the second registry was answered from the
    first one's entry, and every dotted key against it read its segments
    against the wrong dimensions.
    """
    flow_urn = sl.Dataflow.urn_for(agency="AG", id="DF", version="1.0")
    before = _registry_ordering(["REF_AREA", "SEX"])
    after = _registry_ordering(["SEX", "REF_AREA"])

    def slots(registry: sl.LocalRegistry) -> list[str]:
        binding = registry.binding_for(flow_urn)
        assert binding is not None
        return key_dimension_ids(registry, flow_urn, binding)

    assert slots(before) == ["REF_AREA", "SEX"]
    assert slots(after) == ["SEX", "REF_AREA"]
    before.close()
    after.close()
