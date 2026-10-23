"""A `MessageAnnotator` that applies sdmxlib annotation overlays at serve time (GH-10).

An overlay (``sdmxlib.bundle.Overlay``) is a versioned document of entries that
set, add or remove annotations of one type on the artefacts and items a
selector picks: annotations only, never ids, codes, structure or data. It lets
a deployment change display hints without rebuilding its bundle.

`OverlayAnnotator` applies its overlays to every structure message before it
is serialised, so the change reaches every wire format. Its fingerprint is the
overlays' digests, so `CatalogRuntime.effective_token` comes out equal to
sdmxlib's ``effective_token(manifest, overlays)``: the structure cache, the
splices and the ETag move whenever an overlay is added, removed, reordered or
edited, and `AvailabilityKey` does not, since annotations cannot change
availability.

`replace` swaps the overlays on a running deployment, for example from an
admin endpoint or a file watcher the consumer owns. A request in flight when
they are swapped may pair the new body with the old ETag; the next request
carries the new ETag, so a client never holds a stale body for longer than one
revalidation.

Applying overlays copies every artefact an entry selects, on every request the
structure cache does not answer. A ``*`` or class-name selector that reaches a
very large codelist's codes pays that on each such request; keep selectors
specific, or settle a change into the bundle with ``sdmxlib.bundle.fold_overlays``.

Entries' ``scope`` labels are not interpreted: every entry applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from attrs import define, field
from sdmxlib.bundle import apply_overlays, overlay_digest, read_overlay

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    import duckdb
    import sdmxlib as sl
    from sdmxlib.bundle import Merge, Overlay
    from sdmxlib.model.message import StructureMessage


def _fingerprint(overlays: Sequence[Overlay]) -> str:
    """The overlays' digests, one per line, in apply order; ``""`` for none.

    One per line because that is how sdmxlib's ``effective_token`` folds them
    after the manifest token, and the runtime folds a fingerprint after the
    token the same way, so the two tokens are equal.
    """
    return "\n".join(overlay_digest(o) for o in overlays)


@define
class OverlayAnnotator:
    """Applies annotation overlays, in order, to every structure message.

    Attributes:
        merge: Per annotation type, how a ``set`` combines with the annotations
            already there; passed to ``apply_overlays``. Replace by default.
    """

    _overlays: tuple[Overlay, ...] = field(default=(), alias="overlays", converter=tuple)
    merge: Mapping[str, Merge] | None = None
    _fingerprint: str = field(init=False, default="")

    def __attrs_post_init__(self) -> None:
        self._fingerprint = _fingerprint(self._overlays)

    @classmethod
    def from_files(cls, paths: Iterable[str | Path], *, merge: Mapping[str, Merge] | None = None) -> Self:
        """An annotator applying the overlay documents at ``paths``, in order.

        Raises:
            sdmxlib.bundle.OverlayError: If a file is not an overlay document
                this sdmxlib can read.
        """
        return cls(overlays=tuple(read_overlay(p) for p in paths), merge=merge)

    @property
    def overlays(self) -> tuple[Overlay, ...]:
        """The overlays applied, in order."""
        return self._overlays

    def replace(self, overlays: Iterable[Overlay]) -> None:
        """Apply ``overlays`` from the next request on, in place of the current ones."""
        new = tuple(overlays)
        fingerprint = _fingerprint(new)
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
        registry: sl.LocalRegistry,  # noqa: ARG002
        conn: duckdb.DuckDBPyConnection,  # noqa: ARG002
        token: str,  # noqa: ARG002
    ) -> StructureMessage:
        """``message`` with the overlays applied: changed copies, never in place."""
        overlays = self._overlays
        if not overlays:
            return message
        return apply_overlays(message, overlays, merge=self.merge)

    def fingerprint(self) -> str:
        """The overlays' digests, computed when they were set; ``""`` for none."""
        return self._fingerprint


__all__ = ["OverlayAnnotator"]
