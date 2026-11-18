"""Request language tags are normalised before they reach sdmxlib (GH-80).

sdmxlib's ``lang`` parameters run every tag through `sdmxlib.normalize_tag`,
which lowercases it and raises `sdmxlib.InvalidTagError` (a `ValueError`) for
one that is not well-formed by RFC 5646. A tag taken from a request reaches
sdmxlib only after the HTTP boundary has normalised it: `parse_accept_language`
drops the entries `normalize_tag` refuses, ``?lang=`` is parsed the same way
and ignored when no entry survives, and a refused ``label_maps_for_dims`` tag
answers 400.

``?lang=`` never answers an error, as .Stat's ``GetLanguagePreference``
(dotstatsuite-core-getdata, ``DotStatServices.GetData/Utils/Extensions.cs``
at ``c52a9630``) never does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sdmxlib as sl
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from sdmxlib_rest.data.projection import label_maps_for_dims
from sdmxlib_rest.localization import normalize_request_tag, parse_accept_language
from sdmxlib_rest.middleware.lang_override import LangOverrideMiddleware

if TYPE_CHECKING:
    from tests.conftest import World

pytestmark = pytest.mark.unit

_CODELIST = "/structure/codelist/AG/CL_GEO/1.0"

#: Tags `sdmxlib.normalize_tag` refuses, as a client might send them.
_REFUSED = ["en_US", "en US", "*", "fr,en", "-"]

#: ``?lang=`` values from which `parse_accept_language` keeps no tag.
_REFUSED_ONLY = ["en_US", "en US", "*", "-", "en_US,x_y"]


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


def _echo(request: Request) -> PlainTextResponse:
    return PlainTextResponse(request.headers.get("accept-language", "<absent>"))


@pytest.fixture
def echo() -> TestClient:
    """`LangOverrideMiddleware` over a route that echoes ``accept-language``."""
    return TestClient(LangOverrideMiddleware(Starlette(routes=[Route("/", _echo)])))


@pytest.mark.parametrize(
    ("value", "header"),
    [
        ("fr-CA", "fr-ca"),
        ("fr-CA,en", "fr-ca, en"),
        ("en;q=0.5,fr", "fr, en"),
        ("en_US,fr,*", "fr"),
    ],
)
def test_the_lang_override_is_parsed_like_the_header(echo: TestClient, value: str, header: str) -> None:
    assert echo.get("/", params={"lang": value}, headers={"Accept-Language": "de"}).text == header


@pytest.mark.parametrize("value", _REFUSED_ONLY)
def test_an_override_with_no_usable_tag_leaves_the_header_alone(echo: TestClient, value: str) -> None:
    assert echo.get("/", params={"lang": value}, headers={"Accept-Language": "de"}).text == "de"
    assert echo.get("/", params={"lang": value}).text == "<absent>"


def test_a_lang_list_falls_back_within_the_list(client: TestClient) -> None:
    resp = client.get(_CODELIST, params={"lang": "fr,en"})
    assert resp.status_code == 200
    assert resp.headers["content-language"] == "en"


@pytest.mark.parametrize("value", _REFUSED_ONLY)
def test_an_unusable_lang_value_serves_the_header_language(client: TestClient, value: str) -> None:
    resp = client.get(_CODELIST, params={"lang": value}, headers={"Accept-Language": "EN"})
    assert resp.status_code == 200
    assert resp.headers["content-language"] == "en"


def test_an_unusable_lang_value_on_head_is_served(client: TestClient) -> None:
    resp = client.head(_CODELIST, params={"lang": "en_US"})
    assert resp.status_code == 200
    assert resp.content == b""


def test_the_etag_keys_on_the_header_when_the_override_is_ignored(client: TestClient) -> None:
    url = f"{_CODELIST}?lang=en_US"
    fr = client.get(url, headers={"Accept-Language": "fr"}).headers["etag"]
    en = client.get(url, headers={"Accept-Language": "en"}).headers["etag"]
    assert fr != en


def test_the_etag_keys_on_a_usable_override_and_not_the_header(client: TestClient) -> None:
    url = f"{_CODELIST}?lang=FR,en"
    bare = client.get(url).headers["etag"]
    with_header = client.get(url, headers={"Accept-Language": "de"}).headers["etag"]
    assert bare == with_header


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
