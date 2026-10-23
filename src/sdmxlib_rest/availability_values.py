"""The precomputed-availability seam, which supplies value lists for the library to render.

``/availability`` answers in two steps — compute the per-component distinct
value sets and the observation count, then render them. This seam sits between
the two. A consumer that already has the values, because its producer baked them
into the bundle or because it computed them once and kept them, hands them back
and the scan never runs. The library renders the response either way, through
`~sdmxlib_rest.responses.availability.render_availability`.

**The seam is consulted only for unfiltered requests, which keeps its key small.**
Precompute can only ever cover the unfiltered answer: the filter space is
unbounded, so nobody can bake every combination of ``c[]`` values, partial keys
and period bounds. Restricting the seam to that case means its key is just the
build, the flow and the component — nothing to normalise, so no way for two
spellings of one filter to land in different slots, or two different filters to
share one. A filtered request always scans.

"Unfiltered" is judged **after** the `~sdmxlib_rest.filter_shaper.ComponentFilterShaper`
runs. A shaper may add a constraint the request never named, and the answer is
then filtered whatever the URL said — so a precomputed unfiltered answer would be
wrong for it. A shaper may only narrow requests that were already filtered, but
the library cannot assume every shaper does.

**The key omits mode, format, language and references.** With no constraints,
``mode=available`` relaxes nothing and returns exactly what ``mode=exact`` does.
``Accept``, ``Accept-Language`` and ``references=`` change only the
*rendering*. So one entry per (build, flow, component) serves every client
spelling — where the response-level cache this replaced needed a slot per
accept variant x query suffix x language, and still missed ``?lang=fr``.

**Repeat-caching a filtered answer is memoisation and belongs above the route.**
A consumer can install an HTTP response cache through
``create_app(outer_middleware=...)``. It sits outside compression there, so it
must key on ``Accept-Encoding`` as well as ``Accept`` and
``Accept-Language``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from attrs import field, frozen

if TYPE_CHECKING:
    import sdmxlib as sl
    from sdmxlib.model.constraint import KeyValue
    from sdmxlib.model.dataflow import Dataflow


@frozen
class AvailabilityKey:
    """Identifies one precomputable availability answer.

    Hashable and canonical — `SdmxUrn` compares on agency, id and version — so a
    consumer can key a plain ``dict`` on it, or rebuild one from a baked
    filename with ``Dataflow.urn_for(...)`` and get a matching key.

    Attributes:
        token: Pins the entry to one publication of the bundle, so a
            re-publish invalidates every slot without any bookkeeping.
        flow_urn: The resolved dataflow. Keying on the flow rather than the
            request path is what makes the 1.5 and 2.2.2 surfaces share an entry,
            and what makes a forwarder's path shape unable to collide two flows.
        component_id: ``all`` or one component id, verbatim from the path.
            ``all`` is every key dimension; a specific id may also name an
            attribute.
    """

    token: str
    flow_urn: sl.SdmxUrn[Dataflow]
    component_id: str


@frozen
class ResolvedAvailability:
    """The two things a scan produces, and everything rendering needs from it.

    Attributes:
        key_values: One entry per enumerated component, in the order the route
            would scan them — the cube region preserves it.
        obs_count: Rows in the flow. With no constraints this is the total, and
            it is the figure .Stat shows in its "(N observations)" badge. Zero
            renders as a 204.
    """

    key_values: tuple[KeyValue, ...] = field(converter=tuple)
    obs_count: int


@runtime_checkable
class AvailabilityValues(Protocol):
    """Consumer-owned source of precomputed availability values.

    ``lookup`` is asked before scanning and returns the values, or ``None`` to
    fall through to the scan — so a partial bake is a normal state rather than a
    gap. ``store`` is offered the scanned values afterwards, which lets a consumer
    with no producer-side bake still pay the scan once per build instead of once
    per request. Both are called only for unfiltered requests.
    """

    def lookup(self, key: AvailabilityKey) -> ResolvedAvailability | None:
        """Return the values for ``key``, or ``None`` to scan."""
        ...

    def store(self, key: AvailabilityKey, resolved: ResolvedAvailability) -> None:
        """Offer freshly scanned values for keeping (consumer decides)."""


class NullAvailabilityValues:
    """Default: knows nothing, keeps nothing — every request scans."""

    def lookup(self, key: AvailabilityKey) -> ResolvedAvailability | None:  # noqa: ARG002
        """Never a hit."""
        return None

    def store(self, key: AvailabilityKey, resolved: ResolvedAvailability) -> None:
        """No-op."""


__all__ = ["AvailabilityKey", "AvailabilityValues", "NullAvailabilityValues", "ResolvedAvailability"]
