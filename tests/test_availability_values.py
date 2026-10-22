"""The precomputed-values seam: consulted for unfiltered requests only (#7).

The seam replaced a response-level cache keyed on the raw URL plus both
negotiation headers. That key made every client spelling its own slot — a
consumer baking files had to plant each under every accept variant and query
suffix, and still missed ``?lang=fr``. Keying on values instead collapses all of that to one entry
per (build, flow, component).

These tests pin the three claims that design rests on:

- **unfiltered-only is safe** — a filter, a partial key, or a constraint a shaper
  adds all turn the seam off, because a precomputed unfiltered answer would be
  wrong for any of them;
- **mode can be dropped from the key** — with no constraints, ``mode=available``
  and ``mode=exact`` return the same thing;
- **one key serves every spelling** — mode, format, language, ``references=``
  and .Stat's ``dimensionAtObservation`` suffix all land on the same entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import sdmxlib as sl
from fastapi.testclient import TestClient
from sdmxlib.model.constraint import KeyValue

from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.availability_values import AvailabilityKey, ResolvedAvailability
from sdmxlib_rest.runtime import CatalogRuntime
from tests.conftest import BUILD_ID, _WorldProvider

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sdmxlib_rest.filter_shaper import FilterContext
    from tests.conftest import World

pytestmark = pytest.mark.unit

URL = "/availability/dataflow/AG/DF_POP/1.0/all/all"
FLOW = sl.Dataflow.urn_for(agency="AG", id="DF_POP", version="1.0")
KEY_DIMS = ["REF_AREA", "SEX", "TIME_PERIOD"]


class Recorder:
    """An `AvailabilityValues` that records every call and optionally answers."""

    def __init__(self, answer: ResolvedAvailability | None = None) -> None:
        self.answer = answer
        self.lookups: list[AvailabilityKey] = []
        self.stores: list[tuple[AvailabilityKey, ResolvedAvailability]] = []

    def lookup(self, key: AvailabilityKey) -> ResolvedAvailability | None:
        self.lookups.append(key)
        return self.answer

    def store(self, key: AvailabilityKey, resolved: ResolvedAvailability) -> None:
        self.stores.append((key, resolved))


class ConstrainEverything:
    """A shaper that narrows every request to REF_AREA=CA, named or not."""

    def shape(self, component_filter: sl.ComponentFilter, ctx: FilterContext) -> sl.ComponentFilter:
        return sl.parse_components("CA..", {}, KEY_DIMS)


def _client(world: World, values: Recorder, **runtime: Any) -> Iterator[TestClient]:
    app = create_app(
        CatalogRuntime(_WorldProvider(world), availability_values=values, **runtime),
        RestSettings(title="test-sdmx-api", version="0.1.0"),
        routers=list(library_routers()),
    )
    with TestClient(app) as client:
        yield client


def _values(payload: dict[str, Any], dimension_id: str) -> list[str]:
    region = payload["data"]["dataConstraints"][0]["cubeRegions"][0]
    for key_value in region["keyValues"]:
        if key_value["id"] == dimension_id:
            return sorted(key_value["values"])
    return []


# ── What lets the key be this small ──────────────────────────────────────────


def test_unfiltered_mode_available_answers_exactly_what_mode_exact_does(client: TestClient) -> None:
    """Why mode is not in the key.

    ``mode=available`` relaxes each component's own constraint. With no
    constraints there is nothing to relax, so the two modes must agree — and if
    they ever stopped agreeing, a precomputed entry would serve the wrong one.
    """
    exact = client.get(URL, params={"mode": "exact"})
    available = client.get(URL, params={"mode": "available"})
    assert exact.status_code == available.status_code == 200
    # Compare the answer, not the envelope: `meta.prepared` is the wall-clock
    # time the message was built, so two whole bodies differ whenever the
    # requests straddle a second. This test compared them whole and flaked in
    # CI on exactly that — passing locally and once before by luck.
    assert exact.json()["data"] == available.json()["data"]


def test_one_key_serves_every_client_spelling(world: World) -> None:
    """The headline: what took a cartesian product of slots is one entry.

    Mode, format, language, ``references=`` and the ``dimensionAtObservation``
    suffix .Stat appends all change the request and none change the values, so
    every one of these must resolve to the same key.
    """
    values = Recorder(ResolvedAvailability(key_values=(), obs_count=1))
    spellings: list[dict[str, Any]] = [
        {},
        {"params": {"mode": "exact"}},
        {"params": {"mode": "available"}},
        {"params": {"dimensionAtObservation": "AllDimensions"}},
        {"params": {"references": "codelist"}},
        {"params": {"lang": "fr"}},
        {"headers": {"Accept-Language": "fr"}},
        {"headers": {"Accept": "application/vnd.sdmx.structure+xml;version=2.1"}},
        {"headers": {"Accept": "application/vnd.sdmx.structure+json;version=2.0.0;urn=true"}},
    ]
    for client in _client(world, values):
        for spelling in spellings:
            resp = client.get(URL, **spelling)
            assert resp.status_code == 200, (spelling, resp.text)

    assert len(values.lookups) == len(spellings)
    assert set(values.lookups) == {AvailabilityKey(build_id=BUILD_ID, flow_urn=FLOW, component_id="all")}


def test_the_key_is_per_component(world: World) -> None:
    """``all`` and one dimension are different answers, so different entries."""
    values = Recorder()
    for client in _client(world, values):
        client.get(URL)
        client.get("/availability/dataflow/AG/DF_POP/1.0/all/SEX")
    assert [k.component_id for k in values.lookups] == ["all", "SEX"]


# ── Served from the seam ─────────────────────────────────────────────────────


def test_precomputed_values_are_served_instead_of_a_scan(world: World) -> None:
    """The answer carries values the lake does not contain, so it cannot have been scanned."""
    baked = ResolvedAvailability(
        key_values=(KeyValue(dimension_id="REF_AREA", values=frozenset({"ZZ"})),),
        obs_count=999,
    )
    values = Recorder(baked)
    for client in _client(world, values):
        payload = client.get(URL).json()

    assert _values(payload, "REF_AREA") == ["ZZ"]
    annotation = payload["data"]["dataConstraints"][0]["annotations"][0]
    assert annotation["title"] == "999"
    assert values.stores == [], "a hit must not be offered back as if it were scanned"


def test_a_miss_offers_the_scanned_values_back(world: World) -> None:
    """A consumer with no producer-side bake can still scan once per build."""
    values = Recorder()
    for client in _client(world, values):
        client.get(URL)

    assert len(values.stores) == 1
    key, resolved = values.stores[0]
    assert key == AvailabilityKey(build_id=BUILD_ID, flow_urn=FLOW, component_id="all")
    assert resolved.obs_count == 3
    scanned = {kv.dimension_id: sorted(kv.values) for kv in resolved.key_values}
    assert scanned == {"REF_AREA": ["CA", "US"], "SEX": ["F", "M"], "TIME_PERIOD": ["2020", "2021"]}


def test_an_empty_answer_is_stored_and_served_as_a_204(world: World) -> None:
    """A zero count is as expensive to derive as a full one, so it is worth keeping."""
    values = Recorder(ResolvedAvailability(key_values=(), obs_count=0))
    for client in _client(world, values):
        resp = client.get(URL)
    assert resp.status_code == 204


# ── Not served: every way a request can be filtered ──────────────────────────


def test_a_c_filter_never_consults_the_seam(world: World) -> None:
    values = Recorder(ResolvedAvailability(key_values=(), obs_count=999))
    for client in _client(world, values):
        payload = client.get(URL, params={"c[REF_AREA]": "CA"}).json()
    assert values.lookups == []
    assert values.stores == []
    assert _values(payload, "REF_AREA") == ["CA"], "the filtered request must still be scanned"


def test_a_partial_key_counts_as_filtered(world: World) -> None:
    values = Recorder(ResolvedAvailability(key_values=(), obs_count=999))
    for client in _client(world, values):
        client.get("/availability/dataflow/AG/DF_POP/1.0/CA../all")
    assert values.lookups == []
    assert values.stores == []


def test_period_bounds_count_as_filtered(world: World) -> None:
    values = Recorder(ResolvedAvailability(key_values=(), obs_count=999))
    for client in _client(world, values):
        client.get(URL, params={"startPeriod": "2021"})
    assert values.lookups == []


def test_a_constraint_the_shaper_adds_turns_the_seam_off(world: World) -> None:
    """Why "unfiltered" is judged after the shaper, not on the URL.

    This request names no filter at all, but the shaper narrows it to CA. A
    precomputed unfiltered answer would include US and be wrong — so the seam
    must not be asked, and the scan must reflect the shaped filter.
    """
    values = Recorder(ResolvedAvailability(key_values=(), obs_count=999))
    for client in _client(world, values, filter_shaper=ConstrainEverything()):
        payload = client.get(URL).json()

    assert values.lookups == [], "the URL is unfiltered but the request is not"
    assert values.stores == []
    assert _values(payload, "REF_AREA") == ["CA"]
