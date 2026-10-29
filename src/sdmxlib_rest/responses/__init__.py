"""Generic SDMX-REST response post-processing helpers.

Spec-level response shaping that any catalog needs:

- `empty` maps an empty JSON structure or metadata response to 204.
- `asof` declares ``asOf`` and answers a non-null value with 501.
- `availability` renders an `/availability` answer from per-component value
  lists, with a 204 when no observation matches.
- `unwritable` turns an sdmxlib ``ValidationError`` into a 500 naming the
  artefact and the rule it broke.
"""
