"""sdmxlib-rest — generic SDMX-REST HTTP surface over a sdmxlib ``LocalRegistry``.

It owns the generic
HTTP shape of SDMX-REST — URLs, content negotiation, streaming, and
formatting delegation to sdmxlib — over an abstract catalog. Consumers
supply how the catalog is opened, what gets spliced into responses, and any
catalog-specific speedups.

It MUST NOT import a consumer or ``pinax`` — that separation is the whole point of the boundary, and
``tests/test_library_import_boundary.py`` fails the build if it slips.
"""

from __future__ import annotations

__all__: list[str] = []
