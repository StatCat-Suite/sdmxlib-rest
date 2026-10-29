"""Build a synthetic bundle for the bench tier, or profile a real bundle's shape.

`just bench` needs a bundle directory, and a real one holds data that cannot
leave the machine it is on. sdmxlib 0.72 can stand one in:
`sdmxlib.testing.synthetic_bundle` builds a conforming, published bundle of a
given `Shape` from a seed, and `profile_bundle` reads that shape off a real
bundle — counts and fan-outs, never codes, names or values (GH-37).

A shape is a small JSON file. `tests/bench/shapes/` holds the committed ones,
chosen by hand. A profiled shape goes in `tests/bench/shapes/local/`, which is
gitignored: a real bundle's codelist sizes and observation count identify it,
so they stay on the machine that holds it.

## Usage

    uv run python tools/synthetic_bench.py build large .bench/synthetic-large-1
    uv run python tools/synthetic_bench.py build path/to/shape.json DIR --seed 7
    uv run python tools/synthetic_bench.py profile BUNDLE FLOW_URN NAME

`build` reuses a directory it built before from the same shape and seed, and
refuses any other non-empty one. `profile` writes
`tests/bench/shapes/local/NAME.json`.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import cast

import sdmxlib as sl
from sdmxlib.testing import Dim, Shape, profile_bundle, synthetic_bundle

_ROOT = Path(__file__).resolve().parent.parent
SHAPES = _ROOT / "tests" / "bench" / "shapes"
LOCAL_SHAPES = SHAPES / "local"
#: Written beside a built bundle, so a rerun can tell its own output from
#: anything else in the directory.
_STAMP = "synthetic-bench.json"


def shape_to_json(shape: Shape) -> dict[str, object]:
    """A `Shape` as the JSON this tool reads back."""
    return {
        "dimensions": [{"codes": d.codes, "depth": d.depth, "fanout": list(d.fanout)} for d in shape.dimensions],
        "observations": shape.observations,
        "periods": shape.periods,
        "languages": list(shape.languages),
        "attributes": shape.attributes,
        "annotated": shape.annotated,
    }


class ShapeFileError(ValueError):
    """A shape file that does not describe a `Shape`."""

    def __init__(self, problem: str) -> None:
        super().__init__(f"not a shape file: {problem}")


def _field(data: dict[str, object], key: str) -> object:
    if key not in data:
        raise ShapeFileError(f"{key!r} is missing")
    return data[key]


def _int(data: dict[str, object], key: str) -> int:
    value = _field(data, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShapeFileError(f"{key!r} must be an integer")
    return value


def _list(data: dict[str, object], key: str) -> list[object]:
    value = _field(data, key)
    if not isinstance(value, list):
        raise ShapeFileError(f"{key!r} must be a list")
    return value


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ShapeFileError(f"{what} must be an object")
    return {str(k): v for k, v in cast("dict[object, object]", value).items()}


def shape_from_json(data: object) -> Shape:
    """The `Shape` a shape file describes.

    Raises:
        ShapeFileError: If a key is missing or has the wrong type.
    """
    shape = _object(data, "the shape")
    dimensions: list[Dim] = []
    for raw in _list(shape, "dimensions"):
        dim = _object(raw, "a dimension")
        fanout = [v for v in _list(dim, "fanout") if isinstance(v, int)]
        if len(fanout) != 2:
            raise ShapeFileError("'fanout' must be two integers")
        dimensions.append(Dim(codes=_int(dim, "codes"), depth=_int(dim, "depth"), fanout=(fanout[0], fanout[1])))
    annotated = _field(shape, "annotated")
    if not isinstance(annotated, int | float):
        raise ShapeFileError("'annotated' must be a number")
    return Shape(
        dimensions=tuple(dimensions),
        observations=_int(shape, "observations"),
        periods=_int(shape, "periods"),
        languages=tuple(str(lang) for lang in _list(shape, "languages")),
        attributes=_int(shape, "attributes"),
        annotated=float(annotated),
    )


def resolve_shape(name: str) -> Path:
    """A shape file named by path, or by stem under the committed or local shapes.

    Raises:
        FileNotFoundError: If no such shape exists.
    """
    path = Path(name)
    if path.suffix == ".json" and path.is_file():
        return path
    for folder in (SHAPES, LOCAL_SHAPES):
        candidate = folder / f"{name}.json"
        if candidate.is_file():
            return candidate
    msg = f"no shape {name!r} in {SHAPES} or {LOCAL_SHAPES}"
    raise FileNotFoundError(msg)


def build(shape_file: Path, out: Path, seed: int) -> tuple[Path, bool]:
    """Build the synthetic bundle into `out`, or reuse the one already there.

    Args:
        shape_file: The shape to build.
        out: The bundle directory.
        seed: The seed `synthetic_bundle` draws from.

    Returns:
        The publication directory, which `just bench` opens, and whether this
        call built it.

    Raises:
        FileExistsError: If `out` holds anything but this shape and seed's bundle.
    """
    wanted = {"shape": json.loads(shape_file.read_text(encoding="utf-8")), "seed": seed}
    stamp_path = out / _STAMP
    if stamp_path.is_file():
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        token = stamp.pop("token", None)
        if stamp == wanted and token is not None and (out / token).is_dir():
            return out / token, False
        shutil.rmtree(out)
    elif out.exists() and any(out.iterdir()):
        msg = f"{out} is not empty and was not built by this tool"
        raise FileExistsError(msg)
    manifest = synthetic_bundle(out, shape_from_json(wanted["shape"]), seed=seed)
    stamp_path.write_text(json.dumps({**wanted, "token": manifest.token}, indent=2) + "\n", encoding="utf-8")
    return out / manifest.token, True


def profile(bundle: Path, flow: str, name: str) -> Path:
    """Profile `flow` in `bundle` into the gitignored local shapes; return the file."""
    shape = profile_bundle(bundle, sl.Dataflow.parse_urn(flow))
    LOCAL_SHAPES.mkdir(parents=True, exist_ok=True)
    path = LOCAL_SHAPES / f"{name}.json"
    path.write_text(json.dumps(shape_to_json(shape), indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    """Run `build` or `profile`, per the arguments."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    b = commands.add_parser("build", help="build a synthetic bundle from a shape")
    b.add_argument("shape", help="a shape name under tests/bench/shapes[/local], or a .json path")
    b.add_argument("out", type=Path, help="the bundle directory to build")
    b.add_argument("--seed", type=int, default=1)
    p = commands.add_parser("profile", help="write a real bundle's shape to tests/bench/shapes/local/")
    p.add_argument("bundle", type=Path)
    p.add_argument("flow", help="the dataflow URN to profile")
    p.add_argument("name", help="the shape file's stem")
    args = parser.parse_args()

    if args.command == "build":
        shape_file = resolve_shape(args.shape)
        started = time.perf_counter()
        publication, built = build(shape_file, args.out, args.seed)
        # The publication path alone on stdout, for `just bench-synthetic` to pass on.
        if built:
            elapsed = time.perf_counter() - started
            print(f"built {args.out} from {shape_file.name}, seed {args.seed}, in {elapsed:.1f} s", file=sys.stderr)
        else:
            print(f"reusing {args.out} ({shape_file.name}, seed {args.seed})", file=sys.stderr)
        print(publication)
        return 0
    path = profile(args.bundle, args.flow, args.name)
    print(f"wrote {path} (gitignored: it describes a real bundle)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
