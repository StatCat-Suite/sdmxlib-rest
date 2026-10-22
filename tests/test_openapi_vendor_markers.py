"""Vendor parameters + paths carry an ``x-vendor: true`` marker in the published OpenAPI.

OpenAPI's ``x-`` extension namespace is the spec-sanctioned way to add
non-spec fields without breaking validation. SDK generators
(openapi-generator, swagger-codegen, etc.) can filter out non-spec
surface by walking the document for the marker.

Closes #37.
"""

from __future__ import annotations

import pytest

from sdmxlib_rest.middleware.openapi_extensions import apply_vendor_markers

pytestmark = pytest.mark.unit


def _make_op(*params: dict) -> dict:
    """Build a minimal OpenAPI operation object with the supplied parameter list."""
    return {"parameters": list(params)}


def _param(name: str, location: str = "query") -> dict:
    return {"name": name, "in": location}


def _spec_with(paths: dict) -> dict:
    return {"paths": paths}


def _find_param(spec: dict, path: str, name: str) -> dict | None:
    op = spec["paths"][path]["get"]
    for p in op.get("parameters") or []:
        if p.get("name") == name:
            return p
    return None


# ── vendor param markers (per name) ──────────────────────────────────────────


@pytest.mark.parametrize("vendor_name", ["format", "pivot", "Range", "limit", "debug", "_chunk"])
def test_vendor_param_picks_up_x_vendor(vendor_name: str) -> None:
    """Every name in the vendor allowlist gets ``x-vendor: true`` on every operation it appears on."""
    spec = _spec_with(
        {
            "/some/path": {"get": _make_op(_param(vendor_name), _param("detail"))},
            "/another/path": {"get": _make_op(_param(vendor_name))},
        },
    )
    apply_vendor_markers(spec)
    for path in ("/some/path", "/another/path"):
        p = _find_param(spec, path, vendor_name)
        assert p is not None
        assert p.get("x-vendor") is True


def test_non_vendor_param_does_not_pick_up_x_vendor() -> None:
    """A spec-defined param like `detail` MUST NOT be marked."""
    spec = _spec_with({"/some/path": {"get": _make_op(_param("detail"), _param("references"))}})
    apply_vendor_markers(spec)
    for name in ("detail", "references"):
        p = _find_param(spec, "/some/path", name)
        assert p is not None
        assert "x-vendor" not in p


# ── vendor path markers (whole route) ────────────────────────────────────────


@pytest.mark.parametrize(
    "vendor_path",
    [
        "/v1/contentconstraint/{agencyID}/{resourceID}/{version}",
        "/hierarchicalcodelist/{agencyID}/{resourceID}/{version}",
        "/v1/hierarchyassociation/{agencyID}/{resourceID}/{version}",
        "/health",
    ],
)
def test_vendor_path_picks_up_x_vendor(vendor_path: str) -> None:
    """Every method on a vendor path gets ``x-vendor: true`` stamped on the operation.

    The marker rides on each operation (not the path-item itself) so Swagger UI's
    path-item walker doesn't see a phantom ``x-vendor`` sibling of ``get`` and
    render it as an "invalid" operation at the bottom of the docs page.
    """
    spec = _spec_with({vendor_path: {"get": _make_op()}})
    apply_vendor_markers(spec)
    # Marker lands on the operation, NOT at path-item level.
    assert spec["paths"][vendor_path]["get"].get("x-vendor") is True
    assert "x-vendor" not in spec["paths"][vendor_path]


def test_spec_path_does_not_pick_up_x_vendor() -> None:
    """``/structure/...`` is a real SDMX-REST path; must stay un-marked on every operation."""
    spec = _spec_with({"/structure/{type}/{ag}/{id}/{ver}": {"get": _make_op()}})
    apply_vendor_markers(spec)
    assert "x-vendor" not in spec["paths"]["/structure/{type}/{ag}/{id}/{ver}"]["get"]
    assert "x-vendor" not in spec["paths"]["/structure/{type}/{ag}/{id}/{ver}"]


# ── idempotency ──────────────────────────────────────────────────────────────


def test_apply_is_idempotent() -> None:
    """Running the marker twice on the same spec produces an identical result."""
    spec1 = _spec_with({"/data/x": {"get": _make_op(_param("format"))}})
    spec2 = _spec_with({"/data/x": {"get": _make_op(_param("format"))}})
    apply_vendor_markers(spec1)
    apply_vendor_markers(spec2)
    apply_vendor_markers(spec2)  # run twice
    assert spec1 == spec2


# ── shape tolerance ──────────────────────────────────────────────────────────


def test_path_item_with_no_methods_doesnt_blow_up() -> None:
    spec = _spec_with({"/foo": {}})
    apply_vendor_markers(spec)
    # No-op — the path-item isn't in our vendor set and has no params to tag.
    assert spec["paths"]["/foo"] == {}


def test_path_item_with_non_dict_method_is_skipped() -> None:
    spec = _spec_with({"/foo": {"summary": "a string method-ish key", "get": _make_op(_param("format"))}})
    apply_vendor_markers(spec)
    # `summary` is a string at the path-item level; should be skipped without error.
    assert _find_param(spec, "/foo", "format").get("x-vendor") is True


def test_param_without_name_is_skipped() -> None:
    spec = _spec_with({"/foo": {"get": _make_op({"in": "query"}, _param("format"))}})
    apply_vendor_markers(spec)
    assert _find_param(spec, "/foo", "format").get("x-vendor") is True
