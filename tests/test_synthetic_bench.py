"""Tests for `tools/synthetic_bench.py` — the synthetic bundle tier (GH-37).

The bench itself only runs on demand, so these cover what would break it
quietly: a committed shape that no longer parses, and a `build` that rebuilds
for nothing or overwrites a directory it did not create.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sdmxlib.testing import Dim, Shape

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
# `tools/` is not a package, so the tool is loaded by path.
_TOOL = ROOT / "tools" / "synthetic_bench.py"
_spec = importlib.util.spec_from_file_location("synthetic_bench", _TOOL)
assert _spec is not None
assert _spec.loader is not None
synthetic_bench = importlib.util.module_from_spec(_spec)
sys.modules["synthetic_bench"] = synthetic_bench
_spec.loader.exec_module(synthetic_bench)

_TINY = {
    "dimensions": [{"codes": 4, "depth": 2, "fanout": [2, 3]}, {"codes": 3, "depth": 1, "fanout": [2, 10]}],
    "observations": 20,
    "periods": 2,
    "languages": ["en", "fr"],
    "attributes": 1,
    "annotated": 0.5,
}


def _shape_file(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "tiny.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("path", sorted((ROOT / "tests" / "bench" / "shapes").glob("*.json")), ids=lambda p: p.stem)
def test_every_committed_shape_parses(path: Path) -> None:
    shape = synthetic_bench.shape_from_json(json.loads(path.read_text(encoding="utf-8")))

    assert shape.dimensions
    assert shape.observations > 0


def test_a_shape_survives_the_json_round_trip() -> None:
    shape = Shape(
        dimensions=(Dim(codes=9, depth=2, fanout=(2, 4)), Dim(codes=3)),
        observations=40,
        periods=3,
        languages=("en", "fr"),
        attributes=2,
        annotated=0.25,
    )

    assert synthetic_bench.shape_from_json(synthetic_bench.shape_to_json(shape)) == shape


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"observations": None}, "'observations' must be an integer"),
        ({"dimensions": {}}, "'dimensions' must be a list"),
        ({"dimensions": [{"codes": 3, "depth": 1, "fanout": [2]}]}, "'fanout' must be two integers"),
        ({"annotated": "half"}, "'annotated' must be a number"),
    ],
)
def test_a_bad_shape_file_says_what_is_wrong(change: dict[str, object], problem: str) -> None:
    with pytest.raises(synthetic_bench.ShapeFileError, match=problem):
        synthetic_bench.shape_from_json({**_TINY, **change})


def test_a_missing_key_is_named() -> None:
    data = {k: v for k, v in _TINY.items() if k != "periods"}

    with pytest.raises(synthetic_bench.ShapeFileError, match="'periods' is missing"):
        synthetic_bench.shape_from_json(data)


def test_build_reuses_its_own_bundle_and_rebuilds_for_a_new_seed(tmp_path: Path) -> None:
    shape_file = _shape_file(tmp_path, _TINY)
    out = tmp_path / "bundle"

    publication, built = synthetic_bench.build(shape_file, out, seed=1)
    assert built
    assert publication.parent == out
    assert (publication / "bundle.manifest.json").is_file()
    assert synthetic_bench.build(shape_file, out, seed=1) == (publication, False)
    rebuilt, built = synthetic_bench.build(shape_file, out, seed=2)
    assert built
    assert rebuilt != publication


def test_build_refuses_a_directory_it_did_not_create(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "keep.txt").write_text("not ours", encoding="utf-8")

    with pytest.raises(FileExistsError, match="was not built by this tool"):
        synthetic_bench.build(_shape_file(tmp_path, _TINY), out, seed=1)
    assert (out / "keep.txt").read_text(encoding="utf-8") == "not ours"


def test_a_shape_resolves_by_name_or_path(tmp_path: Path) -> None:
    assert synthetic_bench.resolve_shape("large") == ROOT / "tests" / "bench" / "shapes" / "large.json"
    path = _shape_file(tmp_path, _TINY)
    assert synthetic_bench.resolve_shape(str(path)) == path
    with pytest.raises(FileNotFoundError, match="no shape 'nope'"):
        synthetic_bench.resolve_shape("nope")
