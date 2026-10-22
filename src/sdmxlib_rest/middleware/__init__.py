"""Generic ASGI middleware for the SDMX-REST surface.

Catalog-agnostic HTTP concerns: conditional GET, compression, HEAD support,
media-type version advertising, per-request perf logging, ``?lang=`` override,
unknown-query-param rejection, and OpenAPI vendor markers.
"""
