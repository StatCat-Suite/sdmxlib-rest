"""Mark vendor parameters + paths in the published OpenAPI document.

The OpenAPI spec allows `x-`-prefixed extensions on any object. We use
``x-vendor: true`` to tag query params + paths that aren't part of the
SDMX-REST contract but that we expose for our own clients. Consumers
generating spec-conformant SDKs (openapi-generator and friends) can
filter these out by walking the spec for the marker.

Applied as a post-processor on the FastAPI app's ``openapi()`` cache so
the published ``/openapi.json`` and ``/v1/openapi.json`` carry the
markers. See :func:`apply_vendor_markers`.

Closes #37.
"""

from __future__ import annotations

from typing import Any, Final

# Vendor query parameters across all routes. The shared
# :func:`apply_vendor_markers` walks every operation and tags any
# parameter whose name is in this set as ``x-vendor: true``.
_VENDOR_PARAM_NAMES: Final[frozenset[str]] = frozenset(
    {
        # /data extensions
        "format",
        "pivot",
        "debug",
        "_chunk",
        # /metadata listing cap
        "limit",
        # HTTP header — declared via fastapi.Header(alias="Range"); appears as
        # a parameter in OpenAPI even though it's a request header, not a query.
        "Range",
    },
)

# Path templates we expose that aren't part of any SDMX-REST spec version.
# Treated as the route's own ``x-vendor: true`` at the path-item level so
# every operation under them is implicitly vendor.
_VENDOR_PATH_TEMPLATES: Final[frozenset[str]] = frozenset(
    {
        # Synthesised ContentConstraint endpoint for .Stat (cached per dataflow).
        "/v1/contentconstraint/{agencyID}/{resourceID}/{version}",
        "/contentconstraint/{agencyID}/{resourceID}/{version}",
        # SDMX 2.1 envelope shim over SDMX 3 Hierarchy (closes #40).
        "/hierarchicalcodelist/{agencyID}/{resourceID}/{version}",
        "/v1/hierarchicalcodelist/{agencyID}/{resourceID}/{version}",
        # SDMX 3-only binding artefact (no 1.5 / 2.2.2 spec entry).
        "/v1/hierarchyassociation/{agencyID}/{resourceID}/{version}",
        # Liveness probe.
        "/health",
        # Optional UI sub-app.
        "/explorer",
    },
)


def _tag_vendor_param(param: dict[str, Any]) -> None:
    """Set ``x-vendor: true`` on one OpenAPI parameter object."""
    param["x-vendor"] = True


_PATH_ITEM_NON_METHOD_KEYS: Final[frozenset[str]] = frozenset(
    {"parameters", "summary", "description", "servers", "$ref"},
)


def _tag_vendor_operation(op: dict[str, Any]) -> None:
    """Set ``x-vendor: true`` on one OpenAPI operation object."""
    op["x-vendor"] = True


def apply_vendor_markers(spec: dict[str, Any]) -> dict[str, Any]:
    """Mutate an OpenAPI document in place, adding ``x-vendor: true`` to vendor params + ops.

    Vendor paths get the marker stamped on every operation under them
    (rather than at the path-item level) — Swagger UI 5.x renders a
    ``x-vendor`` sibling of ``get`` / ``post`` as a phantom "invalid"
    operation at the bottom of the docs page. Per-operation extensions
    are well-known territory and render cleanly.

    Idempotent — re-running on an already-marked spec doesn't double-write.
    Returns the same dict for caller convenience.
    """
    paths = spec.get("paths") or {}
    for path_template, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        path_is_vendor = path_template in _VENDOR_PATH_TEMPLATES
        for method_name, op in path_item.items():
            if method_name in _PATH_ITEM_NON_METHOD_KEYS or method_name.startswith("x-"):
                continue
            if not isinstance(op, dict):
                continue
            if path_is_vendor:
                _tag_vendor_operation(op)
            for param in op.get("parameters", []) or []:
                if not isinstance(param, dict):
                    continue
                if param.get("name") in _VENDOR_PARAM_NAMES:
                    _tag_vendor_param(param)
    return spec


__all__ = ["apply_vendor_markers"]
