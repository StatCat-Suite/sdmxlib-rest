"""The library never imports the catalog library a consumer opens bundles with.

``sdmxlib_rest`` reaches a catalog only through the ``CatalogProvider`` seam.
statcatlib (formerly pinax, published as pinaxlib) is one way a consumer opens
a bundle, so it is installed next to this package in practice, and an import
of it would work everywhere it is tested. That is exactly why it needs a test:
nothing else would notice.

Checked in a fresh interpreter, because this test session's own ``sys.modules``
says nothing about what importing the library pulls in.
"""

import pkgutil
import subprocess
import sys

import pytest

import sdmxlib_rest

pytestmark = pytest.mark.unit

_FORBIDDEN = ("statcatlib", "pinax", "pinaxlib")


def test_importing_every_module_loads_no_catalog_library() -> None:
    modules = [m.name for m in pkgutil.walk_packages(sdmxlib_rest.__path__, "sdmxlib_rest.")]
    script = (
        "import importlib, sys\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        f"print(','.join(sorted(m for m in sys.modules if m.split('.')[0] in {_FORBIDDEN!r})))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)  # noqa: S603

    assert modules
    assert result.stdout.strip() == ""
