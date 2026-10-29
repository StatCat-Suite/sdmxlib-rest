# EGM reports

Reports from Elephant-Goldfish Model reviews and drift reports, kept as
written. The docs site builds them without listing them in its navigation.

## Drift report on the EGM contracts

[`2026-10-28_DRIFT_REPORT.md`](2026-10-28_DRIFT_REPORT.md) compares the eight
`EGM.md` contracts the EGM bootstrap wrote, and the five design pages, with the
code before their first commit. Five reviewers found about 60 differences, and
the bootstrap found 22 more against `CLAUDE.md`, the guide pages and the
docstrings. Findings tagged `[doc]` and `[both]` were corrected in the same
change. Findings tagged `[code]` were filed as
[GH-72](https://github.com/StatCat-Suite/sdmxlib-rest/issues/72) to
[GH-76](https://github.com/StatCat-Suite/sdmxlib-rest/issues/76), and the
contracts describe the current behaviour and cite them.
