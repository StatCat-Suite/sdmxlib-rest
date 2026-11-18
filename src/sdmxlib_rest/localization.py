"""Accept-Language → localised SDMX-JSON post-processing.

sdmxlib's ``to_json`` emits both ``name`` (a single string, default
language) and ``names`` (the full ``{lang: text}`` map). Clients that
read ``names`` directly get the bilingual data either way; clients that
read ``name`` get whichever language sdmxlib chose as default. This
lets clients drive that choice via the standard ``Accept-Language``
header — we rewrite ``name`` (and ``description`` mirrored against
``descriptions``) on every artefact that exposes both forms.

Scope notes:

- JSON only. SDMX-ML carries every language as separate
  ``<xml:lang>``-tagged children, so XML clients already pick a
  language themselves; no rewrite needed.
- We don't drop ``names`` from the response — clients that want the
  full multilingual struct still get it. We just promote one language
  to the primary ``name`` slot.
- Match strategy: case-insensitive prefix match on the BCP 47 tag.
  ``Accept-Language: fr-CA`` matches an artefact's ``names.fr`` (region
  tag stripped) and an artefact's ``names.fr-CA`` (exact).
"""

from __future__ import annotations

import json

import sdmxlib as sl
from fastapi import HTTPException
from fastapi.responses import Response

from sdmxlib_rest.negotiation import is_browser_request


def localized_json_response(
    body: bytes,
    *,
    media_type: str,
    accept_language: str | None,
    accept: str | None = None,
) -> Response:
    """Return a FastAPI Response carrying ``body`` (optionally localised) + ``Content-Language``.

    When ``accept`` indicates a browser (Accept includes ``text/html`` and
    no SDMX-specific media type), the response is served as
    ``application/json`` instead of the SDMX-versioned media type —
    browsers don't recognise ``application/vnd.sdmx.*+json`` and fall back
    to a file download. The body is identical JSON either way; only the
    Content-Type header changes. Strict SDMX clients (curl with explicit
    Accept, .Stat Explorer's JS fetch) get the spec-defined type unchanged.
    """
    out, content_language = localize_sdmx_json(body, accept_language)
    headers: dict[str, str] = {}
    if content_language is not None:
        headers["Content-Language"] = content_language
    chosen_media_type = "application/json" if is_browser_request(accept) else media_type
    return Response(content=out, media_type=chosen_media_type, headers=headers)


def parse_accept_language(header: str | None) -> list[str]:
    """Return preferred language tags in order, with q-value sort.

    ``Accept-Language: fr-CA, fr;q=0.9, en;q=0.5`` -> ``["fr-ca", "fr", "en"]``.
    Each tag passes through `sdmxlib.normalize_tag`, so the list is lowercase
    and every entry is safe to hand to an sdmxlib ``lang`` parameter. A bare
    ``*`` is treated as "no preference" -> empty list. Entries `normalize_tag`
    refuses (``en_US``, ``en US``) and other malformed entries are dropped,
    so a bad entry under-matches the request and never fails it.
    """
    if not header:
        return []
    items: list[tuple[float, str]] = []
    for raw in header.split(","):
        raw = raw.strip()  # noqa: PLW2901 — intentional rebind
        if not raw or raw.startswith("*"):
            continue
        tag, _, params = raw.partition(";")
        try:
            tag = sl.normalize_tag(tag.strip())
        except sl.InvalidTagError:
            continue
        q = 1.0
        for param in params.split(";"):
            param = param.strip()  # noqa: PLW2901
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    continue
        items.append((q, tag))
    # Stable sort by descending q, preserving the order of equal q.
    items.sort(key=lambda x: -x[0])
    return [tag for _, tag in items]


def normalize_request_tag(tag: str, *, param: str = "lang") -> str:
    """Normalise one request-supplied language tag, or raise a 400.

    Call it on a tag taken from a request before passing it to an sdmxlib
    ``lang`` parameter, which raises `sdmxlib.InvalidTagError` for a tag that
    is not well-formed by RFC 5646.

    Args:
        tag: The tag as the request spelled it, such as ``"fr-CA"``.
        param: The request parameter named in the error detail.

    Returns:
        `tag` in lowercase, as `sdmxlib.normalize_tag` returns it.

    Raises:
        HTTPException: 400, when `sdmxlib.normalize_tag` refuses `tag`. The
            detail is ``"<param>: <sdmxlib's message>"``, which quotes at most
            64 characters of the tag.
    """
    try:
        return sl.normalize_tag(tag)
    except sl.InvalidTagError as exc:
        raise HTTPException(status_code=400, detail=f"{param}: {exc}") from exc


def best_match(preferred: list[str], available: list[str]) -> str | None:
    """Pick the best ``available`` language tag for the client's ``preferred`` list.

    Case-insensitive matching with two-pass fallback: first an exact tag
    match (``fr-CA`` ↔ ``fr-CA``), then a base-language match (``fr-CA``
    matches ``fr``, and vice versa — either side may carry a region tag
    while the other doesn't).
    """
    if not preferred or not available:
        return None
    avail_lower = [tag.lower() for tag in available]
    for want in preferred:
        want_lower = want.lower()
        if want_lower in avail_lower:
            return available[avail_lower.index(want_lower)]
    for want in preferred:
        want_base = want.lower().partition("-")[0]
        for i, have in enumerate(avail_lower):
            if have.partition("-")[0] == want_base:
                return available[i]
    return None


def localize_sdmx_json(body: bytes, accept_language: str | None) -> tuple[bytes, str | None]:
    """Rewrite a SDMX-JSON body so the ``name`` / ``description`` of each artefact match Accept-Language.

    Returns ``(body_bytes, content_language)`` where ``content_language``
    is the language tag actually used (or ``None`` when the header is
    absent / no match was found — in which case the body is returned
    unchanged).
    """
    preferred = parse_accept_language(accept_language)
    if not preferred:
        return body, None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body, None

    chosen_languages: set[str] = set()
    _walk_and_localise(payload, preferred, chosen_languages)
    if not chosen_languages:
        return body, None

    # If multiple artefacts ended up with different languages (rare —
    # different artefacts carry different ``names`` maps), report the
    # most-preferred one that did get used.
    for want in preferred:
        if want in chosen_languages:
            return json.dumps(payload, ensure_ascii=False).encode("utf-8"), want
        for picked in chosen_languages:
            if picked.lower().partition("-")[0] == want.lower().partition("-")[0]:
                return json.dumps(payload, ensure_ascii=False).encode("utf-8"), picked
    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), next(iter(chosen_languages))


# Field pairs: (singular field, plural-multilingual field).
_LOCALISABLE_FIELDS = (("name", "names"), ("description", "descriptions"))

# A parsed JSON body (`json.loads` returns `Any`), typed so the walk below
# narrows to real types.
type _Json = dict[str, _Json] | list[_Json] | str | int | float | bool | None


def _walk_and_localise(node: _Json, preferred: list[str], chosen_out: set[str]) -> None:
    """Recursively rewrite ``name`` (against ``names``) on every dict node."""
    if isinstance(node, dict):
        for single, multi in _LOCALISABLE_FIELDS:
            names = node.get(multi)
            if isinstance(names, dict) and names:
                pick = best_match(preferred, list(names.keys()))
                if pick is not None and isinstance(names.get(pick), str):
                    node[single] = names[pick]
                    chosen_out.add(pick)
        for v in node.values():
            _walk_and_localise(v, preferred, chosen_out)
    elif isinstance(node, list):
        for item in node:
            _walk_and_localise(item, preferred, chosen_out)
