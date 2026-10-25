# Writing a data route

The library serves no `/data` route. A data handler decides how responses are
cached, which partitions a query prunes and which vendor parameters exist, and
the services that run this library each decide those differently. The library
supplies the parts of a handler that every service writes the same way, in
`sdmxlib_rest.data`, and the consumer writes the handler around them.

## The helpers

Every helper takes the request's parameters and the flow's
`sdmxlib.DataflowBinding`, which maps SDMX components to physical columns.

**Parsing the request** (`data.params`):

| Helper | Does |
| --- | --- |
| `key_dimension_ids` | The flow's key dimensions in data structure order, which is the order a dotted key is read in |
| `parse_c_params` | Parses `c[<id>]=<expr>` filters, with the operators `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `co`, `nc`, `sw` and `ew` |
| `build_non_eq_where` | Turns the non-equality filters into SQL fragments and parameters |
| `parse_sort` | Parses `sort=` into physical columns and directions |
| `validate_dimension_at_observation` | Checks `dimensionAtObservation` against the binding |
| `reject_unsupported_standard_params` | Answers 501 for standard parameters a single-bundle service cannot honour |

**Choosing columns** (`data.projection`):

| Helper | Does |
| --- | --- |
| `project_binding` | Trims the binding to the `attributes=` and `measures=` the request names |
| `project_arrow_batches` | Applies the same projection to Arrow batches, for Parquet output |
| `label_maps_for_dims` | Code-to-label maps for a CSV with label columns |

**Shaping the query** (`data.sql`):

| Helper | Does |
| --- | --- |
| `decimals_format_replace_clause` | Formats numeric columns to each row's `DECIMALS` attribute |
| `qualify_is_active` | Whether `firstNObservations` or `lastNObservations` can drop any row |

**Paging** (`data.paging`): `parse_items_range` reads a row-count `Range`
header, as the .Stat Data Explorer sends; `with_range` and `range_status`
build the `Content-Range` headers and the 206.

**Encoding** (`data.encoding`): each encoder takes an iterable of Arrow batches
or CSV chunks and yields bytes, so memory stays bounded by the batch size
whatever the result size.

| Helper | Writes |
| --- | --- |
| `emit_csv_with_labels` | SDMX-CSV with a `<dim>_label` column after each coded dimension |
| `prepend_utf8_bom` | A UTF-8 byte-order mark first, for spreadsheet clients |
| `stream_csv_zip` | A single-entry zip, streamed |
| `encode_parquet` | A Parquet file, streamed |

## A handler's outline

```python
@router.get("/data/{context}/{agencyID}/{resourceID}/{version}/{key}")
def data(..., registry=Depends(get_registry)):
    binding = registry.binding_for(flow_urn)
    dims = key_dimension_ids(registry, flow_urn, binding)
    # parse the key against `dims`, then c[], sort, projection
    # shape the filter with runtime.filter_shaper
    # build and run the query; stream batches through an encoder
    ...
```

`binding_for` returns the binding with its `database_role` naming the catalog
its table is attached as in this registry (`registry.lake_alias` for the lake,
an alias prefixed with the bundle id and token), so SQL built as
`<database_role>.<table_location>` reads the right lake. A binding read some other way, with `get_all`, goes through
`registry.locate(binding)` first.

`get_registry`, `get_meta_conn`, `get_token` and the other request
dependencies in `sdmxlib_rest.runtime` give a handler the same runtime the
library routes use. Include the router in `create_app`'s `routers` list.
