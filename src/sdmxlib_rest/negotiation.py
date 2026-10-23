"""HTTP content-type negotiation helpers.

Each SDMX route advertises a small set of media types it can serialise. If
the caller's ``Accept`` header excludes every one of them, the route answers
``406 Not Acceptable``, as the SDMX-REST 2.2.2 contract requires.

The matcher is liberal. SDMX-CSV / SDMX-JSON / SDMX-ML content types are
long and version-suffixed, and clients commonly send
``application/vnd.sdmx.data+csv`` (no version), ``text/csv``,
``application/xml``, etc. We accept the most common shorthand forms and
strip q-values.
"""

from __future__ import annotations

from fastapi import HTTPException


def is_browser_request(accept: str | None) -> bool:
    """True when the Accept header looks like a browser's, not a strict SDMX client.

    Browsers default to something like
    ``text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8`` —
    they don't recognise ``application/vnd.sdmx.structure+json`` and fall
    back to "save as" instead of rendering. Strict SDMX clients (curl with
    explicit Accept, .Stat Explorer's JS fetch) name the SDMX media type
    directly and do NOT include ``text/html``. The distinguishing token is
    ``text/html``; checking that keeps the heuristic narrow.
    """
    if not accept:
        return False
    raw = accept.lower()
    return "text/html" in raw and "application/vnd.sdmx" not in raw


def _normalize_version(version: str) -> str:
    """Strip trailing ``.0`` patch components from a media-type version token.

    SDMX-REST 2.2.2 uses two-part versions (``version=2.0``); sdmxlib's
    writers emit three-part (``version=2.0.0``). Both denote the same file
    format. Normalising both sides to the shortest form lets matching
    succeed regardless of which shape the client/server picked.

    ``2.0.0`` → ``2.0``, ``2.1`` → ``2.1``, ``1.0.0`` → ``1.0``, ``3.0`` → ``3.0``.
    """
    while version.count(".") >= 2 and version.endswith(".0"):
        version = version[:-2]
    return version


def _parse_media(media: str) -> tuple[str, str | None]:
    """Split a media-type token into ``(base_type, normalized_version)``.

    The base is the ``application/...`` portion (lower-cased, stripped of
    matrix params). The version is the value of ``;version=X.Y[.Z]`` after
    `_normalize_version`, or ``None`` if the token is unversioned.
    Non-version matrix params (``;urn=true``, ``;charset=utf-8``) are
    ignored — they don't change format identity.
    """
    parts = [p.strip() for p in media.lower().split(";")]
    base = parts[0]
    version: str | None = None
    for p in parts[1:]:
        if p.startswith("version="):
            version = _normalize_version(p[len("version=") :])
            break
    return base, version


def _media_matches(
    request_base: str,
    request_version: str | None,
    supported_base: str,
    supported_version: str | None,
) -> bool:
    """``application/...`` base must match exactly; version equal OR either side unversioned.

    An unversioned request token (``application/vnd.sdmx.structure+json``)
    is treated as "any version of this format" — SDMX-REST 2.2.2 §6.1
    permits the server to pick its default. An unversioned supported
    entry would similarly accept any version, but we don't currently
    register such entries.
    """
    if request_base != supported_base:
        return False
    if request_version is None or supported_version is None:
        return True
    return request_version == supported_version


def ensure_acceptable(accept: str | None, supported: list[str]) -> None:
    """Raise ``HTTPException(406)`` if ``accept`` excludes every entry in ``supported``.

    Returns silently otherwise — the actual content-type chosen for the
    response is the route's existing dispatch logic (e.g. "xml in accept →
    XML, else JSON"). This function only enforces "Accept must overlap our
    output set".

    Permissive cases (always considered acceptable):

    - ``accept`` is ``None`` / empty — no preference expressed.
    - The header contains ``*/*`` or ``application/*``.
    - The header contains a media type whose ``(base, normalized version)``
      equals one of the ``supported`` entries. SDMX two-part / three-part
      version tokens are equivalent (``version=2.0`` ≡ ``version=2.0.0``)
      via `_normalize_version`; an unversioned client token matches
      any supported version per SDMX-REST 2.2.2 §6.1.
    - The header contains one of the format-shorthand tokens
      ``xml`` / ``json`` / ``csv`` / ``parquet`` AND the request does NOT
      claim a specific SDMX media type. Lets generic clients sending
      ``text/csv`` / ``application/xml`` etc. still hit our SDMX-typed
      equivalents, while strict spec clients naming an SDMX type
      version we don't produce get the 406 they're owed (closes #33).
    """
    if not accept:
        return
    raw = accept.lower()
    if "*/*" in raw or "application/*" in raw:
        return
    # Version-aware matching: walk every comma-separated Accept token and
    # try to match against any supported entry on (base, normalized version).
    supported_parsed = [_parse_media(m) for m in supported]
    for token in raw.split(","):
        # Strip q-value suffix (``;q=0.8``) — it doesn't affect satisfiability,
        # only ranking; we don't implement preference, just acceptability.
        clean = token.split(";q=")[0].strip()
        if not clean:
            continue
        req_base, req_version = _parse_media(clean)
        for sup_base, sup_version in supported_parsed:
            if _media_matches(req_base, req_version, sup_base, sup_version):
                return
    if "vnd.sdmx" not in raw:
        # Generic-token shortcut. Only when the client didn't claim a
        # specific SDMX media type — otherwise `xml` / `json` substring
        # matches would let an unsupported SDMX version slip through to a
        # silently-wrong response (e.g. asking for SDMX-JSON 2.1.0 and
        # getting our 2.0.0).
        for token in ("xml", "json", "csv", "parquet"):
            if token in raw and any(token in m.lower() for m in supported):
                return
    raise HTTPException(
        status_code=406,
        detail=(f"Accept {accept!r} not satisfiable. This operation produces: {supported}"),
    )
