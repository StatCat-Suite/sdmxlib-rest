"""Unit tests for the bracketed query-param parser used by the data route."""

from __future__ import annotations

import pytest

from sdmxlib_rest.data.params import C_PARAM, parse_bracketed

pytestmark = pytest.mark.unit


def test_single_c_param() -> None:
    items = [("c[GEO_LEVEL]", "Province")]
    assert parse_bracketed(items, C_PARAM) == {"GEO_LEVEL": ["Province"]}


def test_plus_or_within_one_value() -> None:
    items = [("c[GEO_LEVEL]", "Province+Country")]
    assert parse_bracketed(items, C_PARAM) == {"GEO_LEVEL": ["Province", "Country"]}


def test_repeated_keys_merge() -> None:
    items = [("c[GEO_LEVEL]", "Province"), ("c[GEO_LEVEL]", "Country")]
    assert parse_bracketed(items, C_PARAM) == {"GEO_LEVEL": ["Province", "Country"]}


def test_repeated_keys_and_plus_or_merge() -> None:
    items = [
        ("c[GEO_LEVEL]", "Province+Country"),
        ("c[GEO_LEVEL]", "Territory"),
    ]
    assert parse_bracketed(items, C_PARAM) == {"GEO_LEVEL": ["Province", "Country", "Territory"]}


def test_multiple_components() -> None:
    items = [("c[GEO_LEVEL]", "Province"), ("c[OBS_STATUS]", "A")]
    assert parse_bracketed(items, C_PARAM) == {"GEO_LEVEL": ["Province"], "OBS_STATUS": ["A"]}


def test_ignores_non_matching_params() -> None:
    items = [
        ("startPeriod", "2021"),
        ("c[GEO_LEVEL]", "Province"),
        ("within[REF_AREA]", "ON"),
        ("format", "csv"),
    ]
    assert parse_bracketed(items, C_PARAM) == {"GEO_LEVEL": ["Province"]}


def test_empty_input() -> None:
    assert parse_bracketed([], C_PARAM) == {}


def test_empty_value_yields_empty_string_segment() -> None:
    items = [("c[GEO_LEVEL]", "")]
    assert parse_bracketed(items, C_PARAM) == {"GEO_LEVEL": [""]}


def test_value_with_spaces_preserved() -> None:
    items = [("c[GEO_LEVEL]", "Federal electoral district (2013 Representation Order)")]
    expected = {"GEO_LEVEL": ["Federal electoral district (2013 Representation Order)"]}
    assert parse_bracketed(items, C_PARAM) == expected
