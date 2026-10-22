"""Generic SDMX-REST route handlers over an abstract catalog.

Each module here is a FastAPI ``APIRouter`` plus its handlers, depending only
on the library's runtime / splice / cache seams — never on a specific catalog.
Consumers include these routers (or wrap the handler functions) from their own
app factory.
"""
