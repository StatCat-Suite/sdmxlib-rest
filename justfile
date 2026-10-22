# sdmxlib-rest development tasks

default:
    @just --list

# ── Dev ───────────────────────────────────────────────────────────────────────

[group('dev')]
lint:
    uv run ruff check src tests

[group('dev')]
typecheck:
    uv run basedpyright src

# Independent second-opinion pass: pyrefly at `preset = "all"` minus a
# documented deny-list (see pyrefly.toml). basedpyright stays the source of
# truth; pyrefly adds the implicit-Any family, override enforcement, and the
# warn-tier checks a default run hides. Part of `check` — this target is for
# running it alone.
#
# Second-opinion type check: pyrefly at preset = "all"
[group('dev')]
pyrefly:
    uv run pyrefly check

[group('dev')]
checkfmt:
    uv run ruff format --check src tests

# `pyrefly` joined this list once its inherited backlog hit 0 (see pyrefly.toml's
# closing note). It runs LAST: basedpyright is still the source of truth, and
# when both fire on one change its message is the one to read first.
#
# Lint + typecheck (inner dev loop)
[group('dev')]
check: lint checkfmt typecheck pyrefly

# Format code
[group('dev')]
fmt:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# ── Test ──────────────────────────────────────────────────────────────────────

[group('test')]
test:
    uv run pytest -m unit

[group('test')]
test-all:
    uv run pytest

# The ratios behind milestone 1. Needs a real bundle: the effects that motivate
# that work do not exist at fixture size, so without SDMXLIB_REST_BENCH_BUNDLE
# every test skips rather than measuring something meaningless.
[group('test')]
bench bundle="":
    SDMXLIB_REST_BENCH_BUNDLE="{{bundle}}" uv run pytest -m bundle -s -v

[group('test')]
ci: check test

# ── Release ───────────────────────────────────────────────────────────────────
# The library versions independently of any consumer: a consumer pins a range
# and upgrades deliberately. Breaking a seam Protocol is a minor bump while
# we're pre-1.0, and will be a major bump after.

[group('release')]
release-1-preview:
    @echo "Next version: $(git-cliff --bumped-version)"
    @echo ""
    @git-cliff --bump --unreleased

# Inserts the new section above the newest existing one. Deliberately NOT
# `git-cliff --bump -o CHANGELOG.md`, which regenerates the whole file from
# commit subjects: that replaces the curated entries (0.1.0's "Extracted from
# its first consumer" prose and its Added list, 0.2.0's Breaking section) with three
# generated bullets, and rewrites the Keep a Changelog header with cliff.toml's.
# Generated output is the *starting point* for an entry here, not the entry.
#
# Insert a generated entry above the newest one, then edit it
[group('release')]
release-2-changelog VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Writing changelog for v{{VERSION}}..."
    git-cliff --unreleased --tag "v{{VERSION}}" > /tmp/cliff-section.md
    python3 - "{{VERSION}}" <<'EOF'
    import pathlib, re, sys
    version = sys.argv[1]
    generated = pathlib.Path("/tmp/cliff-section.md").read_text()
    # cliff.toml's header is emitted ahead of the section; the file has its own.
    match = re.search(r"^## \[", generated, re.M)
    if match is None:
        raise SystemExit("git-cliff produced no version section — are there unreleased commits?")
    section = generated[match.start() :].strip()
    path = pathlib.Path("CHANGELOG.md")
    text = path.read_text()
    if f"## [{version}]" in text:
        raise SystemExit(f"CHANGELOG.md already has a [{version}] section")
    existing = re.search(r"^## \[", text, re.M)
    if existing is None:
        raise SystemExit("CHANGELOG.md has no existing version section to insert above")
    path.write_text(text[: existing.start()] + section + "\n\n" + text[existing.start() :])
    EOF
    rm -f /tmp/cliff-section.md
    echo ""
    echo "Generated entry inserted. EDIT IT — group the breaking changes and say"
    echo "what a consumer has to do — then run: just release-3-tag {{VERSION}}"

# Takes the version explicitly rather than re-deriving it, so the tag cannot
# disagree with the changelog entry just written. git-cliff bumps a breaking
# change to the next MAJOR; pre-1.0 this project treats a seam break as a minor
# (see the Versioning section of CLAUDE.md), so the two would differ.
#
# Bump pyproject, commit and tag the release
[group('release')]
release-3-tag VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    grep -q "^## \[{{VERSION}}\]" CHANGELOG.md || {
      echo "CHANGELOG.md has no [{{VERSION}}] section — run: just release-2-changelog {{VERSION}}" >&2
      exit 1
    }
    uv version "{{VERSION}}"
    # Not uv.lock — it is gitignored on purpose (see .gitignore: consumers of a
    # published library resolve from pyproject.toml). `git add` on an ignored
    # path errors, which under `set -e` aborted the release half-done.
    git add CHANGELOG.md pyproject.toml
    git commit -m "chore(release): v{{VERSION}}"
    git tag -a "v{{VERSION}}" -m "v{{VERSION}}"
    echo "Tagged v{{VERSION}} — run: just release-4-push"

# Notes come from CHANGELOG.md, NOT `git-cliff --latest`. Generated output is
# the starting point for an entry, not the entry (see release-2-changelog), so
# publishing it raw contradicts the section a human just curated. v0.2.1 is the
# worked example: cliff listed a uv_build pin that lost a merge and never
# shipped, because the superseded commit is still unreleased-since-the-last-tag.
#
# Push the release commit + tag, then publish the GitLab release
[group('release')]
release-4-push:
    #!/usr/bin/env bash
    set -euo pipefail
    VER=$(git describe --tags --abbrev=0)
    NOTES=$(mktemp)
    trap 'rm -f "${NOTES}"' EXIT
    # Extracted BEFORE the push on purpose: a missing or empty section should
    # abort while it is still a local problem, rather than leave a pushed tag
    # with no release attached to it.
    python3 - "${VER#v}" <<'EOF' > "${NOTES}"
    import pathlib, re, sys
    version = sys.argv[1]
    text = pathlib.Path("CHANGELOG.md").read_text()
    start = re.search(rf"^## \[{re.escape(version)}\]", text, re.M)
    if start is None:
        raise SystemExit(f"CHANGELOG.md has no [{version}] section — run: just release-2-changelog {version}")
    rest = text[start.end() :]
    following = re.search(r"^## \[", rest, re.M)
    body = rest if following is None else rest[: following.start()]
    # Drop the remainder of the heading line — GitLab titles the release itself.
    body = body.split("\n", 1)[1].strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md's [{version}] section is empty — write it before releasing")
    print(body)
    EOF
    git push
    git push --tags
    glab release create "${VER}" --name "${VER}" --notes-file "${NOTES}"
    echo "Released ${VER} on GitLab"

[group('release')]
build:
    uv build

# Publish to PyPI. Not wired into CI on purpose — the first publish claims the
# name permanently, so it stays a deliberate manual act.
[group('release')]
publish:
    uv publish
