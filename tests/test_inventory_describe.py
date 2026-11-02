"""`LiveInventory` reads an Inventory root with ``kind: describe`` (GH-84).

statcatlib writes a ``describe`` root for a version whose only change is the
catalog description in ``extensions.statcat``. sdmxlib 0.80.0 and earlier
refuse that kind and skip the root, so a provider that resolves its live
edition through the Inventory would stay on the version before it. sdmxlib
0.81.0 reads it; this test holds the pin to a release that does.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sdmxlib.edition import Producer
from sdmxlib.inventory import (
    CATALOG_FORMAT,
    CATALOG_NAME,
    CURRENT_NAME,
    CatalogDocument,
    Current,
    Event,
    EventKind,
    LiveInventory,
    Root,
    encode,
    leaf_ref,
    local_fetch,
    next_root,
    record,
    root_path,
    sha256_hex,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
_PRODUCT = "14100287"
_EDITION = "01a18e00-0000-7000-8000-000000000100"
_SCHEMA_VERSION = "2.52"
_EXTENSIONS = {"statcat": {"title": "Census catalog"}}


def _write_root(root: Path, new: Root) -> None:
    data = encode(new)
    path = root / root_path(new.seq)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    current = Current(seq=new.seq, root_sha256=sha256_hex(data), restore_epoch=new.restore_epoch)
    (root / CURRENT_NAME).write_bytes(encode(current))


def _next(
    root: Path, previous: Root | None, kind: EventKind, extensions: Mapping[str, Mapping[str, str]] | None = None
) -> Root:
    leaf = record(
        None,
        product_id=_PRODUCT,
        kind="data",
        lifecycle="revisable",
        edition_id=_EDITION,
        previous=None,
        manifest_sha256="0" * 64,
        schema_version=_SCHEMA_VERSION,
        embargo=False,
        now=_NOW,
    )
    data, ref = leaf_ref(leaf)
    (root / ref.leaf).parent.mkdir(parents=True, exist_ok=True)
    (root / ref.leaf).write_bytes(data)
    new = next_root(
        previous,
        products={_PRODUCT: ref},
        event=Event(actor="tests", reason="test", kind=kind),
        producer=Producer(name="tests", version="0"),
        now=_NOW,
        extensions=extensions,
    )
    _write_root(root, new)
    return new


def test_live_inventory_reads_a_describe_root_without_skipping_it(tmp_path: Path) -> None:
    catalog = CatalogDocument(format_version=CATALOG_FORMAT, catalog_id="01a18e00-0000-7000-8000-000000000000")
    (tmp_path / CATALOG_NAME).write_bytes(encode(catalog))
    sealed = _next(tmp_path, None, "seal")
    described = _next(tmp_path, sealed, "describe", extensions=_EXTENSIONS)

    live = LiveInventory(local_fetch(tmp_path), schema_version=_SCHEMA_VERSION).refresh()

    assert (described.seq, described.event.kind) == (2, "describe")
    assert (live.seq, live.skipped) == (2, [])
    assert live.head(_PRODUCT).edition_id == _EDITION
