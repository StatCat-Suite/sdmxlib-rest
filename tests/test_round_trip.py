"""What a route sends must read back as what the registry holds (#9).

Request a response, parse it back with sdmxlib's own reader, and compare every
field of every codelist against the source — minus the one documented
difference, that a stub has no codes. Nothing is listed field by field, so a
field dropped anywhere in the pipeline — stub builder, serialiser, splice —
fails here whichever field it is.

This is the guard #9's first fix lacked. That fix made lazy codelist stubs fast
and, unnoticed, dropped their annotations, validity dates and ``uri``: the speed
tests passed, and only comparing the routes' output end to end showed it. Run
against that version, this file fails in all three formats.

**A round trip only checks what the fixture populates.** The default test
fixture's codelists carry no annotations, so a round trip over it would have
passed the bug. Hence the rich fixture here, and
`test_the_rich_fixture_populates_every_codelist_field`, which fails when sdmxlib
grows a codelist field this fixture leaves at its default. Without that, the
guard would quietly stop covering each new field.

It has already paid for itself once: the first run failed in SDMX-ML 2.1 before
reaching any codelist, because the shared fixture built concept references with
a malformed item URN that JSON readers accept and the 2.1 reader rejects.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import attrs
import pytest
import sdmxlib as sl
from fastapi.testclient import TestClient
from sdmxlib.formats import read
from sdmxlib.model.annotations import Annotation, Annotations

import tests.conftest as cf
from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.runtime import CatalogRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit

FORMATS = {
    "sdmx-json-2.0": "application/vnd.sdmx.structure+json;version=2.0.0",
    "sdmx-ml-2.1": "application/vnd.sdmx.structure+xml;version=2.1",
    "sdmx-ml-3.0": "application/vnd.sdmx.structure+xml;version=3.0",
}

#: (path, params, whether codelists come back as stubs). The two stub routes are
#: the ones #9 touched; the full codelist is the control that codes survive too.
ROUTES = {
    "structure-references-children": ("/structure/dataflow/AG/DF_POP/1.0", {"references": "children"}, True),
    "availability-references-codelist": (
        "/availability/dataflow/AG/DF_POP/1.0/all/all",
        {"references": "codelist"},
        True,
    ),
    "codelist-full": ("/structure/codelist/AG/CL_GEO/1.0", {}, False),
    # The queried artefact itself as a stub — one, a listing, and the shape .Stat
    # sends for structure listings. These used to load every code of every
    # codelist only to discard them.
    "codelist-allstubs": ("/structure/codelist/AG/CL_GEO/1.0", {"detail": "allstubs"}, True),
    "codelist-listing-allstubs": ("/structure/codelist/all/all/all", {"detail": "allstubs"}, True),
    "codelist-listing-stat-shape": (
        "/structure/codelist/all/all/all",
        {"detail": "allcompletestubs", "references": "children"},
        True,
    ),
}

ITEMS = "codes"

#: Fields a format cannot carry by its own schema — not losses, and so not
#: failures. ``isFinal`` is defined only in the SDMX-ML 2.1 XSDs
#: (``SDMXStructureBase.xsd``); SDMX 3.0 replaced it with semantic versioning, and
#: neither the 3.0/3.1 XSDs nor the SDMX-JSON schemas have it. Verified against
#: the schemas sdmxlib vendors under ``tests/schemas``, not recalled.
#:
#: Each entry is asserted to *really* be lost (see the test), so if a format
#: starts carrying one of these the exclusion fails instead of hiding it.
UNREPRESENTABLE: dict[str, frozenset[str]] = {
    "sdmx-json-2.0": frozenset({"is_final"}),
    "sdmx-ml-2.1": frozenset(),
    "sdmx-ml-3.0": frozenset({"is_final"}),
}


def _rich_codelist(codelist_id: str, codes: list[tuple[str, str]]) -> sl.Codelist:
    """A codelist with every optional field set to something non-default."""
    return sl.Codelist(
        id=codelist_id,
        maintainer=cf.AGENCY,
        version="1.0",
        name=cf.istr(codelist_id),
        description=cf.istr(f"{codelist_id}, described"),
        is_final=True,
        annotations=Annotations([Annotation(id="NOTE", title="kept", type="X")]),
        valid_from=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        valid_to=dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
        uri=f"https://example.test/{codelist_id}",
        extensions=(),
        codes=sl.ItemList([sl.Code(id=code, name=cf.istr(name)) for code, name in codes]),
    )


@pytest.fixture(scope="module")
def rich_world() -> Iterator[cf.World]:
    """The shared world, with codelists that exercise every field."""

    def rich_codelists(registry: sl.LocalRegistry) -> None:
        registry.add(_rich_codelist("CL_GEO", [("CA", "Canada"), ("US", "United States")]))
        registry.add(_rich_codelist("CL_SEX", [("M", "Male"), ("F", "Female")]))

    world = cf.build_world(codelists=rich_codelists)
    yield world
    world.registry.close()


@pytest.fixture(scope="module")
def rich_client(rich_world: cf.World) -> Iterator[TestClient]:
    app = create_app(
        CatalogRuntime(cf._WorldProvider(rich_world)),
        RestSettings(title="round-trip", version="0.1.0"),
        routers=list(library_routers()),
    )
    with TestClient(app) as client:
        yield client


def _lost_fields(sent: Any, source: Any, *, stub: bool, skip: frozenset[str]) -> dict[str, tuple[object, object]]:
    """Fields where what came back differs from the source."""
    lost: dict[str, tuple[object, object]] = {}
    for field in attrs.fields(type(source)):
        if not field.init or field.name in skip:
            continue
        expected = getattr(source, field.name)
        if field.name == ITEMS and stub:
            expected = type(expected)() if not isinstance(expected, list) else []
            if len(getattr(sent, ITEMS)) != 0:
                lost[ITEMS] = (getattr(sent, ITEMS), "no items, because a stub")
            continue
        received = getattr(sent, field.name)
        if field.name == ITEMS:
            received, expected = list(received), list(expected)
        if received != expected:
            lost[field.name] = (received, expected)
    return lost


@pytest.mark.parametrize("fmt", list(FORMATS))
@pytest.mark.parametrize("route", list(ROUTES))
def test_every_codelist_field_survives_the_round_trip(
    rich_client: TestClient, rich_world: cf.World, route: str, fmt: str
) -> None:
    path, params, stub = ROUTES[route]
    resp = rich_client.get(path, params=params, headers={"Accept": FORMATS[fmt]})
    assert resp.status_code == 200, resp.text[:300]

    message = read(resp.content)
    assert message.codelists, "the discrimination check: codelists really were in the response"

    unrepresentable = UNREPRESENTABLE[fmt]
    lost = {}
    for sent in message.codelists:
        urn = sl.Codelist.urn_for(agency=sent.agency_id, id=sent.id, version=sent.version)
        source = rich_world.registry.get(urn, eager=True)
        if diff := _lost_fields(sent, source, stub=stub, skip=unrepresentable):
            lost[sent.id] = diff
        # Keep each exclusion able to fail: the field must really be lost here.
        for name in unrepresentable:
            assert getattr(sent, name) != getattr(source, name), (
                f"{fmt} now carries {name!r}; remove it from UNREPRESENTABLE so it is checked again"
            )
    assert lost == {}, f"{route} in {fmt} did not round-trip: {lost}"


def test_the_rich_fixture_populates_every_codelist_field() -> None:
    """The guard on the guard: a round trip only checks fields the fixture sets.

    Fails when sdmxlib adds a ``Codelist`` field that `_rich_codelist` leaves at
    its default — at which point the round trip above would silently stop
    covering it. Two exemptions, each because "equals the default" does not mean
    "unset" for it: ``extensions`` references another codelist, which would need a
    second artefact to resolve against; and ``version`` is set deliberately to
    ``1.0``, which happens to be the default — it is also part of every route URL
    here, so the round trip checks it on every request regardless.
    """
    codelist = _rich_codelist("CL_X", [("A", "a")])
    exempt = {"extensions", "version"}
    unset = []
    for field in attrs.fields(sl.Codelist):
        if not field.init or field.name in exempt or field.default is attrs.NOTHING:
            continue
        default = field.default.factory() if isinstance(field.default, attrs.Factory) else field.default  # pyright: ignore[reportAttributeAccessIssue]
        if getattr(codelist, field.name) == default:
            unset.append(field.name)
    assert unset == [], f"set these in _rich_codelist so the round trip covers them: {unset}"
