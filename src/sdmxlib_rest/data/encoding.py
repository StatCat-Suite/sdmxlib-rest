"""Wire-format encoders for observation streams.

Every encoder here takes an iterable of Arrow record batches (or of CSV byte
chunks) and yields bytes, so memory stays bounded by batch size. Bulk SDMX
downloads are routinely larger than a server process's memory budget.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

import polars as pl
import pyarrow.parquet as pq
import zipstream

if TYPE_CHECKING:
    import pyarrow as pa
    import sdmxlib as sl


def emit_csv_with_labels(
    batches: Iterable[pa.RecordBatch],
    binding: sl.DataflowBinding,
    *,
    structure_id: str,
    label_maps: dict[str, dict[str, str]],
    structure_kind: str = "dataflow",
    action: str = "I",
) -> Iterator[bytes]:
    """SDMX-CSV writer emitting an extra ``<dim>_label`` column after each coded dim.

    Mirrors the shape sdmxlib's ``write_sdmx_csv`` produces (STRUCTURE,
    STRUCTURE_ID, ACTION, dims, measures, attrs) but interleaves a
    ``<dim>_label`` column after each dim column present in ``label_maps``.
    Attributes and measures pass through unchanged — labels only make sense
    for enumerated key dims.

    Raises:
        KeyError: If a batch is missing a column the binding declares.
    """
    dim_cols = list(binding.dimension_column_map.values())
    measure_cols = list(binding.measure_column_map.values())
    attr_cols = list(binding.attribute_column_map.values())
    data_cols = [*dim_cols, *measure_cols, *attr_cols]

    out_cols: list[str] = ["STRUCTURE", "STRUCTURE_ID", "ACTION"]
    for col in dim_cols:
        out_cols.append(col)
        if col in label_maps:
            out_cols.append(f"{col}_label")
    out_cols.extend(measure_cols)
    out_cols.extend(attr_cols)

    header_bytes = (",".join(out_cols) + "\n").encode("utf-8")
    header_emitted = False

    for batch in batches:
        missing = [c for c in data_cols if c not in batch.schema.names]
        if missing:
            msg = f"Record batch is missing column(s) {missing!r}; available: {batch.schema.names}"
            raise KeyError(msg)
        df = pl.from_arrow(batch)
        assert isinstance(df, pl.DataFrame)  # noqa: S101 — pl.from_arrow of RecordBatch narrows
        df = df.select(data_cols).with_columns(
            pl.lit(structure_kind).alias("STRUCTURE"),
            pl.lit(structure_id).alias("STRUCTURE_ID"),
            pl.lit(action).alias("ACTION"),
        )
        for col, mapping in label_maps.items():
            if col not in df.columns:
                continue
            df = df.with_columns(pl.col(col).cast(pl.Utf8).replace_strict(mapping, default=None).alias(f"{col}_label"))
        df = df.select(out_cols)
        buf = io.BytesIO()
        df.write_csv(buf, include_header=False)
        body = buf.getvalue()
        if not header_emitted:
            yield header_bytes + body
            header_emitted = True
        else:
            yield body
    if not header_emitted:
        yield header_bytes


def prepend_utf8_bom(csv_chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Emit a leading UTF-8 BOM then forward every chunk from ``csv_chunks``.

    For the SDMX-REST 2.2.2 ``format=csvfile`` / ``csvfilewithlabels`` download
    flow: Excel opens a UTF-8 CSV cleanly with the BOM present and mangles
    diacritics without it. Upstream gates this on ``file=true``.
    """
    yield b"\xef\xbb\xbf"
    yield from csv_chunks


def stream_csv_zip(csv_chunks: Iterable[bytes], *, inner_filename: str) -> Iterator[bytes]:
    """Wrap a CSV byte iterator in a single-entry streaming zip.

    Uses ``zipstream-ng`` so the central directory is appended at the end of
    the body — no rewinding, no buffering of the whole archive. Compression
    level 1 matches the gzip path's CPU budget; the same DEFLATE runs
    underneath, so the speed/ratio trade-off is identical.
    """
    zs = zipstream.ZipStream(compress_type=zipstream.ZIP_DEFLATED, compress_level=1)
    zs.add(csv_chunks, inner_filename)
    yield from zs


def encode_parquet(batches: Iterable[pa.RecordBatch]) -> Iterator[bytes]:
    """Stream Arrow batches as a parquet file.

    The parquet wire format is sequential — magic header, row groups, then a
    footer ending in the file metadata. Readers consume in order and parse the
    footer last, so HTTP streaming is fine as long as no Content-Length is
    advertised. Each yielded chunk is whatever the ``ParquetWriter`` flushed
    since the previous yield; the final yield carries the closing footer.

    Memory is bounded by row-group size (one ``RecordBatch``), not by total
    result size, and TTFB is the first batch's scan + encode rather than the
    whole query.

    ``write_statistics`` and ``use_dictionary`` are off deliberately:
    SDMX-CSV-shaped data is string-heavy with high repetition, zstd compresses
    the raw bytes better than dictionary encoding represents them, and the
    page min/max scan costs CPU that bulk-download readers get nothing for
    (they don't do row-group predicate pushdown).
    """
    sink = io.BytesIO()
    writer: pq.ParquetWriter | None = None
    for batch in batches:
        if writer is None:
            writer = pq.ParquetWriter(
                sink,
                batch.schema,
                compression="zstd",
                write_statistics=False,
                use_dictionary=False,
            )
        writer.write_batch(batch)
        chunk = sink.getvalue()
        if chunk:
            yield chunk
            sink.seek(0)
            sink.truncate(0)
    if writer is not None:
        writer.close()
        chunk = sink.getvalue()
        if chunk:
            yield chunk
