"""A lazy codelist is stubbed from its wrapper, never materialised to stub it (#9).

`_stub_lazy` builds a stub straight from a ``LazyCodelist`` — the fast path that
exists so ``references=children`` can list thirteen codelists without reading a
single code. Eleven call sites used it as ``_stub_lazy(x) or _stub(_materialize(x))``.
sdmxlib 0.62 gave ``Codelist`` a ``__len__`` (sdmxlib#398), so the stub — whose
codes are emptied by design — became falsy, and every one of those sites threw it
away and materialised the whole codelist to stub it again.

Measured at ~5.6 ms per thousand codes: 12 ms for 619 codes, 565 ms for 100k,
roughly 3 s per stub on a codelist of half a million codes. On every request, and
inside the route where no consumer cache can reach it.

The guard at the bottom matters as much as the behavioural tests: this was one
bug copied eleven times, and the cheapest way to keep it fixed is to make the
expression impossible to reintroduce.
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sdmxlib as sl
from sdmxlib.model.annotations import Annotation, Annotations

import sdmxlib_rest
from sdmxlib_rest.routes import structure
from sdmxlib_rest.routes.structure import _materialize, _stub, _stub_lazy, _stub_of

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.conftest import World

pytestmark = pytest.mark.unit

GEO = sl.Codelist.urn_for(agency="AG", id="CL_GEO", version="1.0")


@pytest.fixture
def materialised(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the type of everything `_materialize` is asked to hydrate."""
    seen: list[str] = []
    real = structure._materialize

    def spy(artefact: Any) -> Any:
        seen.append(type(artefact).__name__)
        return real(artefact)

    monkeypatch.setattr(structure, "_materialize", spy)
    return seen


def test_an_empty_codelist_stub_is_falsy(world: World) -> None:
    """The root cause, pinned so a change in sdmxlib shows up here first.

    If this ever stops holding, the bug's trigger is gone — but `_stub_of` is
    still right, because it never asks the question.
    """
    stub = _stub_lazy(world.registry.get(GEO))
    assert isinstance(stub, sl.Codelist)
    assert len(stub) == 0
    assert not stub, "an emptied Codelist stub is falsy under sdmxlib 0.62+"


def test_stub_of_builds_a_lazy_codelist_stub_without_materialising(world: World, materialised: list[str]) -> None:
    lazy = world.registry.get(GEO)
    assert type(lazy).__name__ == "LazyCodelist", "the fixture must hand back the lazy wrapper"

    stub = _stub_of(lazy)

    assert materialised == []
    assert type(stub).__name__ == "Codelist"
    assert isinstance(stub, sl.Codelist)
    assert len(stub) == 0


def test_stub_of_still_stubs_an_artefact_that_has_no_lazy_wrapper(world: World) -> None:
    """The other branch: a non-lazy artefact falls through to `_stub`."""
    dataflow = world.registry.get(world.flow_urn)
    assert _stub_lazy(dataflow) is None, "a dataflow has no lazy wrapper"
    assert _stub_of(dataflow) is not None


def test_references_children_lists_codelists_without_materialising_one(
    client: TestClient, materialised: list[str]
) -> None:
    """The issue's own case: a dataflow with ``references=children``, stubs asked for.

    It used to ask at the default ``detail=full``, when references were stubbed
    whatever ``detail`` said. Since GH-32 ``full`` returns them in full, so the
    no-codes promise belongs to the stub details: ``referencestubs`` here, and
    ``allcompletestubs`` in the shape .Stat sends.
    """
    resp = client.get(
        "/structure/dataflow/AG/DF_POP/1.0", params={"references": "children", "detail": "referencestubs"}
    )

    assert resp.status_code == 200
    codelists = resp.json()["data"].get("codelists", [])
    assert codelists, "the discrimination check: codelists really were inlined"
    assert "LazyCodelist" not in materialised


def test_availability_references_codelist_does_not_materialise_one(client: TestClient, materialised: list[str]) -> None:
    """The same expression lived in the availability renderer, so the same bug did."""
    resp = client.get("/availability/dataflow/AG/DF_POP/1.0/all/all", params={"references": "codelist"})

    assert resp.status_code == 200
    assert resp.json()["data"].get("codelists"), "the discrimination check: codelists were inlined"
    assert "LazyCodelist" not in materialised


def _stub_lazy_or_sites(path: Path) -> list[int]:
    """Lines where ``_stub_lazy(...)`` is the left operand of an ``or``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            first = node.values[0]
            if isinstance(first, ast.Call) and isinstance(first.func, ast.Name) and first.func.id == "_stub_lazy":
                hits.append(node.lineno)
    return hits


def test_no_stub_site_tests_the_lazy_stub_for_truthiness() -> None:
    """The guard: ``_stub_lazy(x) or ...`` must not come back anywhere in the package.

    Read off the AST rather than grepped, so the docstrings that describe the bug
    do not trip it.
    """
    package = Path(sdmxlib_rest.__file__).parent
    offenders = {
        str(path.relative_to(package)): lines
        for path in sorted(package.rglob("*.py"))
        if (lines := _stub_lazy_or_sites(path))
    }
    assert offenders == {}, f"use `_stub_of` instead of `_stub_lazy(x) or ...`: {offenders}"


@pytest.mark.parametrize("kind", ["codelist", "hierarchy"])
def test_the_fast_stub_is_the_same_stub_the_slow_path_builds(kind: str) -> None:
    """The fix must change speed, not output.

    Since 0.2.0 the falsy-stub bug meant every lazy codelist was actually stubbed
    through ``_stub(_materialize(x))``, so that is what clients have been
    receiving. The lazy stub used to copy a hand-kept list of six fields and so
    dropped annotations, validity dates, ``uri`` and extensions — making the
    #9 fix a silent output change until the fields were derived from the class.
    Equality here is the whole contract: same object, without reading the items.
    """
    registry = sl.LocalRegistry(Path(tempfile.mkdtemp()) / "m.duckdb").open()
    annotations = Annotations([Annotation(id="NOTE", title="kept", type="X")])
    if kind == "codelist":
        registry.add(
            sl.Codelist(
                id="CL",
                maintainer="AG",
                version="1.0",
                name={"en": "x"},
                description={"en": "described"},
                annotations=annotations,
                codes=[sl.Code(id="A", name={"en": "a"})],
            )
        )
        urn: Any = sl.Codelist.urn_for(agency="AG", id="CL", version="1.0")
    else:
        registry.add(sl.Hierarchy(id="H", maintainer="AG", version="1.0", name={"en": "h"}, annotations=annotations))
        urn = sl.Hierarchy.urn_for(agency="AG", id="H", version="1.0")

    lazy = registry.get(urn)
    assert type(lazy).__name__.startswith("Lazy"), "the fast path only applies to a lazy wrapper"

    fast = _stub_lazy(lazy)
    assert fast == _stub(_materialize(lazy))
    assert isinstance(fast, (sl.Codelist, sl.Hierarchy))
    assert fast.annotations == annotations


# ── The queried artefact itself as a stub ────────────────────────────────────

STUB_REQUESTS = {
    "one codelist": ("/structure/codelist/AG/CL_GEO/1.0", {"detail": "allstubs"}),
    "every codelist": ("/structure/codelist/all/all/all", {"detail": "allstubs"}),
    # The shape .Stat sends for structure listings: a stub detail *with* references.
    "every codelist, .Stat shape": (
        "/structure/codelist/all/all/all",
        {"detail": "allcompletestubs", "references": "children"},
    ),
}


@pytest.mark.parametrize("request_name", list(STUB_REQUESTS))
def test_a_stub_request_for_a_codelist_never_loads_its_codes(
    client: TestClient, materialised: list[str], request_name: str
) -> None:
    """A stub carries no codes, so nothing should load them.

    Both routes that serve the queried artefact used to load it in full before
    looking at ``detail``, to hand the loaded object to the references walk —
    which never reads a codelist's items. In a listing that was every codelist in
    the catalogue: ~3.3 s for one half-million-code codelist alone, per request.
    """
    path, params = STUB_REQUESTS[request_name]
    resp = client.get(path, params=params)

    assert resp.status_code == 200
    assert resp.json()["data"].get("codelists"), "the discrimination check: codelists were returned"
    assert "LazyCodelist" not in materialised


def test_a_full_request_still_loads_the_codes(client: TestClient, materialised: list[str]) -> None:
    """The other side: ``detail=full`` returns the codes, so it must load them.

    Without this the test above would pass just as well against a route that had
    stopped returning codes altogether — or against a spy that recorded nothing.
    """
    resp = client.get("/structure/codelist/AG/CL_GEO/1.0")

    assert resp.status_code == 200
    assert resp.json()["data"]["codelists"][0]["codes"], "a full codelist carries its codes"
    assert "LazyCodelist" in materialised


#: Every value `_resolve_extras` dispatches on, plus two that fall through.
REFERENCES = ["none", "children", "descendants", "all", "parents", "parentsandsiblings", "dataflow", "codelist"]


@pytest.mark.parametrize("detail", ["allstubs", "full", "referencestubs"])
@pytest.mark.parametrize("references", REFERENCES)
def test_the_references_walk_answers_the_same_for_a_wrapper_and_a_loaded_artefact(
    world: World, references: str, detail: str
) -> None:
    """Why the routes can hand the walk an unloaded wrapper.

    `LazyCodelist` is not a subclass of `Codelist`, so a walker branching on
    ``isinstance(x, sl.Codelist)`` would take a different path for the wrapper
    and silently change the response. None does today — every walker acts only on
    dataflows, structures and category schemes — which is what makes skipping the
    load safe.

    This compares the two answers on the fixture's data, which has no
    extensions — so a walker that branched on ``sl.Codelist`` and read them would
    return ``[]`` either way and pass here. Measured, not assumed: such a walker
    did. `test_no_code_branches_on_a_class_that_has_a_lazy_twin` is the guard
    that catches it whatever the data; this one stays because it catches a
    divergent branch whose answer is not empty.
    """
    lazy = world.registry.get(GEO)
    assert type(lazy).__name__ == "LazyCodelist"

    via_wrapper = structure._resolve_extras(lazy, references, detail, world.registry)
    via_loaded = structure._resolve_extras(_materialize(lazy), references, detail, world.registry)

    assert via_wrapper == via_loaded


#: Concrete classes storage can hand back as a lazy wrapper instead. A wrapper is
#: not a subclass of its concrete class, so branching on one of these silently
#: sends the wrapper down the other path.
_HAS_LAZY_TWIN = {"Codelist", "Hierarchy"}


def _branches_on_a_lazy_twin(path: Path) -> list[int]:
    """Lines calling ``isinstance(x, ...)`` with one of `_HAS_LAZY_TWIN` among the types."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"):
            continue
        if len(node.args) != 2:
            continue
        kinds = node.args[1].elts if isinstance(node.args[1], ast.Tuple) else [node.args[1]]
        for kind in kinds:
            name = kind.attr if isinstance(kind, ast.Attribute) else kind.id if isinstance(kind, ast.Name) else None
            if name in _HAS_LAZY_TWIN:
                hits.append(node.lineno)
    return hits


def test_no_code_branches_on_a_class_that_has_a_lazy_twin() -> None:
    """The data-independent guard that makes skipping the load safe.

    The routes hand the references walk an unloaded wrapper. That is only right
    while nothing branches on ``sl.Codelist`` or ``sl.Hierarchy``: a
    ``LazyCodelist`` is not a ``Codelist``, so such a branch takes the other path
    for the wrapper and changes the response without failing anything. Comparing
    outputs cannot be trusted to notice — it needs data that exercises the branch
    — so this reads the source.

    For a codelist, branch on ``sl.CodelistLike``, which is runtime-checkable and
    matches the wrapper and the loaded codelist alike. sdmxlib has no
    ``HierarchyLike``, so a hierarchy branch has to name ``LazyHierarchy`` too.
    """
    package = Path(sdmxlib_rest.__file__).parent
    offenders = {
        str(path.relative_to(package)): lines
        for path in sorted(package.rglob("*.py"))
        if (lines := _branches_on_a_lazy_twin(path))
    }
    assert offenders == {}, (
        f"isinstance against a class with a lazy twin: {offenders}. For a codelist use "
        f"sl.CodelistLike; for a hierarchy also accept sdmxlib.storage.lazy.LazyHierarchy."
    )
