"""What the structure routes cost on a bundle with one very large codelist, as ratios.

The availability tier measures the lake. This one serves the bundle through the
library's own app and times requests for its largest codelist, the case a
geography dimension of hundreds of thousands of codes makes expensive: a full
codelist is a body of about 100 MB, and a stub of it must not read a single
code. ``just bench-synthetic census`` builds such a bundle, modelled on the
public 2021 Census Profile: a ~580k-code six-level geography, a ~2.6k-code
nine-level characteristic list, names in English and French.

Ratios, never wall times, for the reason `test_availability_sql` gives.
Observed on the ``census`` shape, seed 1, macOS:

==============================================  ===========================
measurement                                     observed
==============================================  ===========================
codelist ``allstubs`` vs full                   3 ms vs 5.4 s
dataflow children, ``referencestubs`` vs full   32 ms vs 3.8 s (GH-32)
codelist in French vs English                   5.4 s vs 5.4 s
codelist in SDMX-ML 3.0 vs SDMX-JSON 2.0        5.9 s vs 5.4 s
==============================================  ===========================

``LocalRegistry.bundle`` opens the publication read-only, so this tier can run
beside a service holding the same bundle.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sdmxlib as sl
from attrs import frozen
from fastapi.testclient import TestClient

from sdmxlib_rest.app import RestSettings, create_app, library_routers
from sdmxlib_rest.provider import OpenedCatalog
from sdmxlib_rest.runtime import CatalogRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.bundle

_REPEATS = 2
_JSON = {"Accept": "application/vnd.sdmx.structure+json;version=2.0.0"}
_XML = {"Accept": "application/vnd.sdmx.structure+xml;version=3.0"}
#: Below this a full codelist answers in milliseconds, and a ratio against a
#: stub measures request overhead rather than whether codes were read.
_LARGE = 50_000


@frozen
class Served:
    """The bundle behind the library app, and the largest codelist it holds.

    Attributes:
        client: A `TestClient` over the library app, catalog open.
        codelist: The path of the bundle's largest codelist.
        codes: How many codes it has.
        dataflow: The path of a dataflow whose structure references it.
    """

    client: TestClient
    codelist: str
    codes: int
    dataflow: str


class _Provider:
    def __init__(self, root: Path) -> None:
        self._root = root

    def open(self) -> OpenedCatalog:
        registry = sl.LocalRegistry.bundle(self._root)
        return OpenedCatalog(registry=registry, meta_conn=registry.connection, on_close=registry.close)


def _path(kind: str, artefact: sl.MaintainableArtefact) -> str:
    return f"/structure/{kind}/{artefact.agency_id}/{artefact.id}/{artefact.version}"


@pytest.fixture(scope="module")
def served() -> Iterator[Served]:
    path = os.environ.get("SDMXLIB_REST_BENCH_BUNDLE")
    if not path:
        pytest.skip("set SDMXLIB_REST_BENCH_BUNDLE to a publication directory to run the bundle tier")
    root = Path(path)
    if not (root / "meta.duckdb").is_file():
        pytest.skip(f"{root} has no meta.duckdb")
    runtime = CatalogRuntime(_Provider(root))
    app = create_app(runtime, RestSettings(title="bench", version="0"), routers=list(library_routers()))
    with TestClient(app) as client:
        registry = runtime.registry
        # `len` on a lazy codelist counts without loading its codes.
        largest = max(registry.get_all(sl.Codelist), key=len)
        urn = str(largest.urn)
        dataflow = next(
            (
                flow
                for flow in registry.get_all(sl.Dataflow)
                if flow.structure is not None
                and (dsd := registry.get(flow.structure.urn)) is not None
                and isinstance(dsd, sl.DataStructure)
                and any(str(cl.urn) == urn for cl in dsd.all_codelists.values())
            ),
            None,
        )
        if dataflow is None:
            pytest.skip(f"no dataflow references {largest.id}")
        yield Served(
            client=client,
            codelist=_path("codelist", largest),
            codes=len(largest),
            dataflow=_path("dataflow", dataflow),
        )
    runtime.close()


def _best(
    served: Served, path: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None
) -> float:
    best = float("inf")
    for _ in range(_REPEATS):
        start = time.perf_counter()
        response = served.client.get(path, params=params or {}, headers=headers or _JSON)
        best = min(best, time.perf_counter() - start)
        assert response.status_code == 200, f"{path} {params}: {response.status_code}"
    return best


@pytest.fixture(scope="module")
def large(served: Served) -> Served:
    if served.codes < _LARGE:
        pytest.skip(f"the largest codelist has {served.codes} codes; this tier needs {_LARGE}+")
    return served


def test_a_stub_of_the_large_codelist_reads_no_codes(large: Served) -> None:
    """#9: a stub built by loading every code and dropping them."""
    full = _best(large, large.codelist)
    stub = _best(large, large.codelist, {"detail": "allstubs"})

    assert stub < full * 0.05, f"stub={stub:.3f}s full={full:.3f}s ({stub / full:.2f}x)"


def test_stubbed_references_read_no_codes(large: Served) -> None:
    """``references=children`` reads every referenced code at ``detail=full`` (GH-32), and none otherwise."""
    full = _best(large, large.dataflow, {"references": "children"})
    stubs = _best(large, large.dataflow, {"references": "children", "detail": "referencestubs"})

    assert stubs < full * 0.05, f"referencestubs={stubs:.3f}s full={full:.3f}s ({stubs / full:.2f}x)"


def test_names_in_french_cost_what_english_does(large: Served) -> None:
    """Localising to the second language must not re-walk or re-encode the codes."""
    body = large.client.get(large.codelist, headers=_JSON).json()
    languages = set(body["data"]["codelists"][0]["codes"][0].get("names", {}))
    if not {"en", "fr"} <= languages:
        pytest.skip(f"the codelist's names are in {sorted(languages)}, not en and fr")

    english = _best(large, large.codelist, headers={**_JSON, "Accept-Language": "en"})
    french = _best(large, large.codelist, headers={**_JSON, "Accept-Language": "fr"})

    assert french < english * 1.3, f"fr={french:.2f}s en={english:.2f}s ({french / english:.2f}x)"


def test_sdmx_ml_costs_within_twice_sdmx_json(large: Served) -> None:
    json = _best(large, large.codelist, headers=_JSON)
    xml = _best(large, large.codelist, headers=_XML)

    assert xml < json * 2, f"xml={xml:.2f}s json={json:.2f}s ({xml / json:.2f}x)"
