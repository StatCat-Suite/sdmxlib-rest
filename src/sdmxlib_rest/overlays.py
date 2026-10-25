"""A `MessageAnnotator` that applies sdmxlib annotation overlays at serve time (GH-10).

An overlay (``sdmxlib.bundle.Overlay``) is a versioned document of entries that
set, add or remove annotations of one type on the artefacts and items a
selector picks: annotations only, never ids, codes, structure or data. It lets
a deployment change display hints without rebuilding its bundle.

`OverlayAnnotator` applies its overlays to every structure message before it
is serialised, so the change reaches every wire format. Its fingerprint is the
hooks line and the overlays' digests as applied to its audience, so
`CatalogRuntime.effective_token` comes out equal to sdmxlib's
``effective_token(manifest, overlays, merge=..., audience=...)``: the
structure cache, the splices and the ETag move whenever an overlay is added,
removed, reordered or edited, or a hook or the audience changes, and
`AvailabilityKey` does not, since annotations cannot change availability.

`replace` swaps the overlays on a running deployment, for example from an
admin endpoint or a file watcher the consumer owns. A request in flight when
they are swapped may pair the new body with the old ETag; the next request
carries the new ETag, so a client never holds a stale body for longer than one
revalidation.

Applying overlays copies every artefact an entry selects, on every request the
structure cache does not answer. A ``*`` or class-name selector that reaches a
very large codelist's codes pays that on each such request; keep selectors
specific, or settle a change into the bundle with ``sdmxlib.bundle.fold_overlays``.

With an ``audience``, an entry applies when its ``scope`` is that audience or
``null``; without one, every entry applies. An overlay that changes a name or
description (``texts``) is refused when it is set, since sdmxlib applies those
only in ``fold_overlays``, into a new publication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from attrs import define, field
from sdmxlib.bundle import OverlayError, applied, apply_overlays, overlay_digest, read_overlay

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    import duckdb
    import sdmxlib as sl
    from sdmxlib.bundle import MergeHook, Overlay
    from sdmxlib.model.message import StructureMessage


def _fingerprint(overlays: Sequence[Overlay], merge: Mapping[str, MergeHook] | None, audience: str | None) -> str:
    """The hooks line, then each overlay's digest as applied to `audience`; ``""`` for no overlays.

    The lines sdmxlib's ``effective_token`` hashes after ``v2`` and the
    manifest token. The runtime hashes a fingerprint after the same two lines,
    so the two tokens are equal.
    """
    if not overlays:
        return ""
    hooks = ",".join(f"{kind}={hook.id}" for kind, hook in sorted((merge or {}).items())) or "default"
    return "\n".join([hooks, *(overlay_digest(applied(o, audience)) for o in overlays)])


def _refuse_texts(overlays: Sequence[Overlay]) -> None:
    for overlay in overlays:
        if overlay.texts:
            msg = (
                f"overlay {overlay.id!r} changes {len(overlay.texts)} name or description text(s); "
                "sdmxlib applies those only with fold_overlays, into a new publication"
            )
            raise OverlayError(msg)


@define
class OverlayAnnotator:
    """Applies annotation overlays, in order, to every structure message.

    Attributes:
        merge: Per annotation type, the `MergeHook` that decides how a ``set``
            combines with the annotations already there; passed to
            ``apply_overlays``. Replace by default.
        audience: The audience whose scoped entries apply, with the unscoped
            ones; ``None`` applies every entry.

    An overlay that changes a name or description raises `OverlayError` at
    construction.
    """

    _overlays: tuple[Overlay, ...] = field(default=(), alias="overlays", converter=tuple)
    merge: Mapping[str, MergeHook] | None = None
    audience: str | None = None
    _fingerprint: str = field(init=False, default="")

    def __attrs_post_init__(self) -> None:
        _refuse_texts(self._overlays)
        self._fingerprint = _fingerprint(self._overlays, self.merge, self.audience)

    @classmethod
    def from_files(
        cls,
        paths: Iterable[str | Path],
        *,
        merge: Mapping[str, MergeHook] | None = None,
        audience: str | None = None,
    ) -> Self:
        """Build an annotator applying the overlay documents at ``paths``, in order.

        A file that is not an overlay document this sdmxlib can read raises
        `sdmxlib.bundle.OverlayError` from `read_overlay`.

        Args:
            paths: Overlay documents, applied in this order.
            merge: Per annotation type, the `MergeHook` for a ``set``.
                Replace by default.
            audience: The audience whose scoped entries apply; ``None``
                applies every entry.

        Returns:
            The annotator.
        """
        return cls(overlays=tuple(read_overlay(p) for p in paths), merge=merge, audience=audience)

    @property
    def overlays(self) -> tuple[Overlay, ...]:
        """The overlays applied, in order."""
        return self._overlays

    def replace(self, overlays: Iterable[Overlay]) -> None:
        """Apply ``overlays`` from the next request on, in place of the current ones.

        An overlay that changes a name or description raises `OverlayError`,
        and the current overlays stay.

        Args:
            overlays: The overlays to apply, in order.
        """
        new = tuple(overlays)
        _refuse_texts(new)
        fingerprint = _fingerprint(new, self.merge, self.audience)
        # Overlays before fingerprint. A request reads the fingerprint (ETag,
        # cache key) before it annotates, so this order can only pair a new
        # body with the old key, which the next request replaces. The reverse
        # would file the old body under the new key, and clients would keep
        # getting 304s for it until the next change.
        self._overlays = new
        self._fingerprint = fingerprint

    def annotate(
        self,
        message: StructureMessage,
        *,
        registry: sl.BundleRegistry,  # noqa: ARG002
        conn: duckdb.DuckDBPyConnection,  # noqa: ARG002
        token: str,  # noqa: ARG002
    ) -> StructureMessage:
        """``message`` with the overlays applied: changed copies, never in place."""
        overlays = self._overlays
        if not overlays:
            return message
        return apply_overlays(message, overlays, merge=self.merge, audience=self.audience)

    def fingerprint(self) -> str:
        """The hooks line and the overlays' digests, computed when they were set; ``""`` for none."""
        return self._fingerprint


__all__ = ["OverlayAnnotator"]
