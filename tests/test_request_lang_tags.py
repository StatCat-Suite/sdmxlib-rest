"""Request language tags are normalised before they reach sdmxlib (GH-80).

sdmxlib's ``lang`` parameters run every tag through `sdmxlib.normalize_tag`,
which lowercases it and raises `sdmxlib.InvalidTagError` (a `ValueError`) for
one that is not well-formed by RFC 5646. A tag taken from a request reaches
sdmxlib only after the HTTP boundary has normalised it: `parse_accept_language`
drops the entries `normalize_tag` refuses, and a refused ``?lang=`` value or
``label_maps_for_dims`` tag answers 400.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sdmxlib as sl
from fastapi import HTTPException

from sdmxlib_rest.data.projection import label_maps_for_dims
from sdmxlib_rest.localization import normalize_request_tag, parse_accept_language

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.conftest import World

pytestmark = pytest.mark.unit

_CODELIST = "/structure/codelist/AG/CL_GEO/1.0"

#: Tags `sdmxlib.normalize_tag` refuses, as a client might send them.
_REFUSED = ["en_US", "en US", "*", "fr,en", "-"]


def test_accept_language_entries_are_lowercased() -> None:
    assert parse_accept_language("fr-CA, fr;q=0.9, EN;q=0.5") == ["fr-ca", "fr", "en"]


def test_accept_language_drops_entries_normalize_tag_refuses() -> None:
    header = "en_US, en US;q=0.9, fr-CA;q=0.8, *;q=0.1, x_y"
    assert parse_accept_language(header) == ["fr-ca"]


def test_normalize_request_tag_lowercases_a_well_formed_tag() -> None:
    assert normalize_request_tag("fr-CA") == "fr-ca"


@pytest.mark.parametrize("tag", _REFUSED)
def test_normalize_request_tag_refuses_with_a_400(tag: str) -> None:
    with pytest.raises(HTTPException) as caught:
        normalize_request_tag(tag)
    assert caught.value.status_code == 400
    assert caught.value.detail.startswith("lang: ")
    assert "well-formed language tag" in caught.value.detail


def test_normalize_request_tag_names_the_parameter() -> None:
    with pytest.raises(HTTPException) as caught:
        normalize_request_tag("en_US", param="labelLang")
    assert caught.value.detail.startswith("labelLang: ")


@pytest.mark.parametrize("tag", _REFUSED)
def test_a_refused_lang_query_value_answers_400(client: TestClient, tag: str) -> None:
    resp = client.get(_CODELIST, params={"lang": tag})
    assert resp.status_code == 400
    assert resp.headers["content-type"] == "application/json"
    assert resp.json()["detail"].startswith("lang: ")


def test_a_refused_lang_query_value_on_head_answers_400(client: TestClient) -> None:
    resp = client.head(_CODELIST, params={"lang": "en_US"})
    assert resp.status_code == 400
    assert resp.content == b""


def test_a_lang_query_value_in_any_case_is_served(client: TestClient) -> None:
    resp = client.get(_CODELIST, params={"lang": "EN"})
    assert resp.status_code == 200
    assert resp.headers["content-language"] == "en"


def test_a_malformed_accept_language_entry_is_ignored(client: TestClient) -> None:
    resp = client.get(_CODELIST, headers={"Accept-Language": "en_US, EN;q=0.5"})
    assert resp.status_code == 200
    assert resp.headers["content-language"] == "en"


def test_label_maps_accept_a_tag_in_any_case(world: World) -> None:
    binding = world.registry.binding_for(world.flow_urn)
    assert binding is not None
    urns = {"REF_AREA": sl.Codelist.urn_for(agency="AG", id="CL_GEO", version="1.0")}
    labels = label_maps_for_dims(world.registry, urns, binding, "EN")
    assert labels == {"geo": {"CA": "Canada", "US": "United States"}}


def test_label_maps_refuse_a_malformed_tag_with_a_400(world: World) -> None:
    binding = world.registry.binding_for(world.flow_urn)
    assert binding is not None
    with pytest.raises(HTTPException) as caught:
        label_maps_for_dims(world.registry, {}, binding, "en_US")
    assert caught.value.status_code == 400
