# Design

These pages record why sdmxlib-rest is built the way it is. The guide pages
(Serving, Seams, HTTP behaviour, Writing a data route) say what the library
does; a design page says what was decided, the measurements behind it, and
what would reopen it.

| Page | Decides |
| --- | --- |
| [Scope and seams](scope.md) | What the library holds, what a consumer holds, and when a seam is added |
| [URL grammar](url-grammar.md) | Why every route serves SDMX-REST 2.2.2 keys and a 1.5 surface is a forwarder |
| [Availability](availability.md) | How `/availability` reads observations, which query shape each request gets, and where precompute lives |
| [Tokens and cache keys](tokens.md) | What the ETag, the structure cache and the availability values key on |
| [Unwritable content](content-validity.md) | Why content sdmxlib refuses to write is a 500 with no switch to turn validation off |

The middleware order is a design decision too. [HTTP behaviour](../http.md#middleware-order)
lists it, and the docstring of `_install_middleware` in `app.py` gives the
reason for each position.

## How a design is written

New designs follow the Elephant-Goldfish Model, as in the other StatCat Suite
repositories:

1. `/egm:egm-brainstorm` settles the idea into a concept brief.
2. `/egm:egm-draft` turns the brief into a page in this directory.
3. `/egm:egm-review` reviews the page with zero-context reviewers. The reports
   go under `docs/design/egm/<page>/`, with a `README.md` saying what came of
   them.
4. A pull request merges the page; the implementation follows it.

`/egm:egm-drift-report` compares these pages and the `EGM.md` contracts with
the code. Its reports go in `docs/design/egm/`, named by date.

A page describes the code as it is. When the code changes a decision, the
same pull request updates the page. A page the code no longer follows at all
moves to `history/design/`, headed with a line naming what replaced it, as in
sdmxlib. The directory is created when the first page moves.

## Where the records are

Decisions made before October 2026 were argued in the
[archived GitLab tracker](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues);
these pages state their outcomes and link the issue. Open proposals live in
the [GitHub tracker](https://github.com/StatCat-Suite/sdmxlib-rest/issues).
