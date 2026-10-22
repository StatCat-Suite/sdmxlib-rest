"""Behavioural coverage for ``/availability``, and the key-shape gap under it.

Two jobs.

The first is ordinary regression cover: the route builds a ``DataConstraint``
and serialises it, and sdmxlib validates on serialise — so these tests are
what catches an artefact this route assembles that cannot be written.

The second is the pair of key-length tests, which pin the *shape* of a key
on a 2.2.2 route in both directions — see
:func:`test_a_key_omitting_the_time_slot_is_rejected`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _values(payload: dict[str, Any], dimension_id: str) -> list[str]:
    """Available values for one dimension, out of the single CubeRegion."""
    constraints = payload["data"]["dataConstraints"]
    region = constraints[0]["cubeRegions"][0]
    for key_value in region["keyValues"]:
        if key_value["id"] == dimension_id:
            return sorted(key_value["values"])
    return []


def test_unfiltered_availability_lists_every_observed_value(client: TestClient) -> None:
    """The bare-flow URL is the cold path .Stat hits; it enumerates the whole cube."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/all/all")

    assert resp.status_code == 200
    payload = resp.json()
    assert _values(payload, "REF_AREA") == ["CA", "US"]
    assert _values(payload, "SEX") == ["F", "M"]
    assert _values(payload, "TIME_PERIOD") == ["2020", "2021"]


def test_a_dotted_key_narrows_the_available_values(client: TestClient) -> None:
    """Constraining REF_AREA to CA drops the US-only period from every dimension.

    The fixture is asymmetric precisely so this assertion can fail: US exists
    only in 2021, so a key filter that silently did nothing would still leave
    ``2021`` in the result.
    """
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/CA../all")

    assert resp.status_code == 200
    payload = resp.json()
    assert _values(payload, "REF_AREA") == ["CA"]
    assert _values(payload, "TIME_PERIOD") == ["2020"]


def test_a_fully_specified_key_reduces_to_one_cell(client: TestClient) -> None:
    """Every slot constrained — the narrowest query the grammar allows."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/CA.M.2020/all")

    assert resp.status_code == 200
    payload = resp.json()
    assert _values(payload, "REF_AREA") == ["CA"]
    assert _values(payload, "SEX") == ["M"]


def test_a_c_parameter_filters_the_same_way_a_key_does(client: TestClient) -> None:
    """``c[...]`` is the 2.2.2 spelling of a key constraint and must agree with it."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/all/all", params={"c[REF_AREA]": "CA"})

    assert resp.status_code == 200
    assert _values(resp.json(), "TIME_PERIOD") == ["2020"]


def test_an_unknown_component_is_rejected_with_the_valid_ones(client: TestClient) -> None:
    """A componentID typo should name what was acceptable, not 404."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/all/NOT_A_DIMENSION")

    assert resp.status_code == 400
    assert "REF_AREA" in resp.json()["detail"]


def test_a_flow_without_a_binding_is_404(client: TestClient) -> None:
    """Availability is answered from the data plane, so an unbound flow has none."""
    resp = client.get("/availability/dataflow/AG/NO_SUCH_FLOW/1.0/all/all")
    assert resp.status_code == 404


def test_the_constraint_reports_the_observation_count(client: TestClient) -> None:
    """The ``obs_count`` annotation drives the "(N observations)" badge in .Stat."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/all/all")

    assert resp.status_code == 200
    annotations = resp.json()["data"]["dataConstraints"][0]["annotations"]
    counts = [a["title"] for a in annotations if a.get("id") == "obs_count"]
    assert counts == ["3"]


def test_availability_is_serialisable_as_sdmx_ml(client: TestClient) -> None:
    """The XML branch runs the same message through a stricter writer than JSON.

    Worth its own test through the migration: SDMX-ML validation is where a
    missing ``name`` or a malformed attachment surfaces first.
    """
    resp = client.get(
        "/availability/dataflow/AG/DF_POP/1.0/all/all",
        headers={"Accept": "application/vnd.sdmx.structure+xml;version=3.0"},
    )

    assert resp.status_code == 200
    assert resp.content.startswith(b"<?xml")


def test_a_key_omitting_the_time_slot_is_rejected(client: TestClient) -> None:
    """A short key is a client error on this route, and that is correct.

    Tempting to read as a 1.5-compatibility gap, because SDMX-REST 1.5/2.1
    does keep the time dimension out of the dotted key. But every path this
    library publishes is 2.2.2 grammar
    (``/availability/{context}/{agencyID}/{resourceID}/{version}/{key}/{componentID}``),
    and 2.2.2 makes time a key slot — so ``CA.M`` is genuinely short for this
    three-slot DSD rather than a valid 1.5 spelling arriving at the wrong
    parser.

    A service that also wants to answer 1.5 URLs puts a forwarder in front
    that translates the older grammar, which is where the version difference
    belongs: the forwarder knows which generation it is serving, and this
    route always knows it is serving one.
    """
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/CA.M/all")

    assert resp.status_code == 400
    assert "segments" in resp.json()["detail"]


def test_a_key_longer_than_the_slot_list_is_rejected(client: TestClient) -> None:
    """Over-long keys stay a 400 whatever the API version — pins the other side."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/CA.M.2020.EXTRA/all")
    assert resp.status_code == 400


# ── mode=available: relax only the enumerated component's own constraint ─────
#
# The fixture: CA has M and F in 2020; US has only M in 2021.


def test_mode_available_relaxes_the_enumerated_component_only(client: TestClient) -> None:
    """Under ``CA..``, REF_AREA lists every area; the other components stay narrowed to CA."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/CA../all", params={"mode": "available"})

    assert resp.status_code == 200
    payload = resp.json()
    assert _values(payload, "REF_AREA") == ["CA", "US"]
    assert _values(payload, "SEX") == ["F", "M"]
    assert _values(payload, "TIME_PERIOD") == ["2020"]


def test_mode_available_relaxes_a_c_parameter_on_the_enumerated_component(client: TestClient) -> None:
    """``c[SEX]=F`` is dropped for SEX itself and kept for everything else."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/all/all", params={"mode": "available", "c[SEX]": "F"})

    assert resp.status_code == 200
    payload = resp.json()
    assert _values(payload, "SEX") == ["F", "M"]
    assert _values(payload, "REF_AREA") == ["CA"]
    assert _values(payload, "TIME_PERIOD") == ["2020"]


def test_mode_available_counts_under_the_full_filter(client: TestClient) -> None:
    """The badge count is the user's actual query, not any of the relaxations."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/CA../all", params={"mode": "available"})

    annotations = resp.json()["data"]["dataConstraints"][0]["annotations"]
    assert [a["title"] for a in annotations if a["id"] == "obs_count"] == ["2"]


def test_mode_available_keeps_period_bounds_on_other_components(client: TestClient) -> None:
    """``startPeriod`` constrains TIME_PERIOD, so only TIME_PERIOD's own query relaxes it.

    From 2021 on, only US-male remains, so SEX must list M alone. The route
    before #6 dropped the bounds from every component's relaxed query and
    listed F too, which exists only in 2020.
    """
    resp = client.get(
        "/availability/dataflow/AG/DF_POP/1.0/all/all", params={"mode": "available", "startPeriod": "2021"}
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert _values(payload, "SEX") == ["M"]
    assert _values(payload, "REF_AREA") == ["US"]
    assert _values(payload, "TIME_PERIOD") == ["2020", "2021"]
