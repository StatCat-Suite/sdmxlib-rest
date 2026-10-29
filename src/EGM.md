# src

## Overview

`src/` holds the `sdmxlib_rest` package, laid out as a src-layout
distribution and built from `pyproject.toml`. It contains no source files of
its own.

## Subdirectories & Boundaries

- sdmxlib_rest: [`./sdmxlib_rest/EGM.md`](./sdmxlib_rest/EGM.md). The generic
  SDMX-REST HTTP surface over an sdmxlib `BundleRegistry`: the app factory,
  the runtime and its seams, the structure, metadata and availability routes,
  the middleware stack, response helpers, and the data-plane helpers a
  consumer's `/data` route is built from.
