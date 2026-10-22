"""``Range`` request / ``Content-Range`` response handling for observation slices.

SDMX-REST has no pagination of its own for the data plane; clients that need
it (the .Stat Data Explorer in particular) reuse HTTP Range with a row-count
unit rather than bytes. This module parses the request side and builds the
matching response headers.
"""

from __future__ import annotations

import re

# Two unit tokens are accepted. ``items`` is the .Stat / SDMX-REST-conventional
# token; ``values`` is what dotstatsuite-data-explorer hardcodes on every
# request (``getRangeHeaderValue`` in its ``lib/utils.js``). Both mean the same
# 0-indexed, inclusive-end row range.
RANGE_RE = re.compile(r"^\s*(items|values)\s*=\s*(\d+)\s*-\s*(\d+)\s*$")


def parse_items_range(header: str | None) -> tuple[int, int, str] | None:
    """Parse a ``Range: <unit>=<start>-<end>`` header into ``(start, end, unit)``.

    The unit is echoed back on the response so ``Range: values=0-9`` yields
    ``Content-Range: values 0-9/<total>``.

    Returns ``None`` for an absent header or any other unit (e.g. ``bytes``) —
    callers fall through to the unranged stream. Validation of the resulting
    bounds belongs to the caller, which can raise 400 with route context.
    """
    if not header:
        return None
    m = RANGE_RE.match(header)
    if m is None:
        return None
    unit = m.group(1)
    start = int(m.group(2))
    end = int(m.group(3))
    return start, end, unit


def build_range_headers(start: int, end: int, total: int, unit: str) -> dict[str, str]:
    """Build the ``Content-Range`` + ``Accept-Ranges`` headers for a 206 response."""
    return {
        "Content-Range": f"{unit} {start}-{end}/{total}",
        "Accept-Ranges": unit,
    }


def with_range(
    base_headers: dict[str, str],
    range_info: tuple[int, int, int, str] | None,
    *,
    sort_skipped: bool = False,
) -> dict[str, str]:
    """Layer ``Content-Range`` / ``Accept-Ranges`` onto a headers dict when paginating.

    ``sort_skipped=True`` adds ``X-Sort-Skipped: true`` — for callers that
    enforce a ceiling on sorted pagination and served an unordered first page
    instead of an expensive global sort.
    """
    out = dict(base_headers)
    if range_info is not None:
        start, end, total, unit = range_info
        out.update(build_range_headers(start, end, total, unit))
    if sort_skipped:
        out["X-Sort-Skipped"] = "true"
    return out


def range_status(range_info: tuple[int, int, int, str] | None) -> int:
    """``206 Partial Content`` when the response is a Range slice, ``200 OK`` otherwise."""
    return 206 if range_info is not None else 200
