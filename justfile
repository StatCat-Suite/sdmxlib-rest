# sdmxlib-rest development tasks

default:
    @just --list

# ── Dev ───────────────────────────────────────────────────────────────────────

[group('dev')]
lint:
    uv run ruff check src tests

# pyrefly at `preset = "all"` minus a documented deny-list (see pyrefly.toml)
# is the only type checker. Keep it at 0 findings. It covers `src` and
# `tests`, and dead suppressions fail here too: `unused-ignore` and
# `unused-type-ignore` are both on.
#
# Type check: pyrefly at preset = "all", src + tests
[group('dev')]
typecheck:
    uv run pyrefly check

[group('dev')]
checkfmt:
    uv run ruff format --check src tests

# Lint + typecheck (inner dev loop)
[group('dev')]
check: lint checkfmt typecheck

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

# Starts release/vVERSION from origin/main, then inserts the new section above
# the newest one. Deliberately NOT `git-cliff --bump -o CHANGELOG.md`, which
# regenerates the whole file from commit subjects: that replaces the curated
# entries (0.1.0's "Extracted from its first consumer" prose and its Added list,
# 0.2.0's Breaking section) with three generated bullets, and rewrites the Keep
# a Changelog header with cliff.toml's. It would also drop every entry before
# 0.6.2, since GitHub's history starts at a single "sdmxlib-rest 0.6.2" commit.
# Generated output is the *starting point* for an entry here, not the entry.
#
# Start release/vX from origin/main and insert a generated entry, then edit it
[group('release')]
release-2-changelog VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -n "$(git status --porcelain)" ]]; then
      echo "error: the working tree has changes; commit or stash them first" >&2
      exit 1
    fi
    git fetch origin --tags
    git switch --detach origin/main
    git switch -c "release/v{{VERSION}}"
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
    echo "what a consumer has to do — then run: just release-3-pr"

# The version comes from the branch release-2-changelog started, so it cannot
# disagree with the changelog entry just written. git-cliff bumps a breaking
# change to the next MAJOR; pre-1.0 this project treats a seam break as a minor
# (see the Versioning section of CLAUDE.md), so the two would differ.
#
# Bump pyproject, commit, push release/vX and open its PR
[group('release')]
release-3-pr:
    #!/usr/bin/env bash
    set -euo pipefail
    BRANCH=$(git branch --show-current)
    if [[ "${BRANCH}" != release/v* ]]; then
      echo "error: not on a release/vX branch (run just release-2-changelog VERSION)" >&2
      exit 1
    fi
    VER="${BRANCH#release/}"
    # Extracted BEFORE anything is pushed: a missing or empty section should
    # abort while it is still a local problem.
    NOTES=$(just _release-notes "${VER#v}")
    uv version "${VER#v}"
    # Not uv.lock — it is gitignored on purpose (see .gitignore: consumers of a
    # published library resolve from pyproject.toml). `git add` on an ignored
    # path errors, which under `set -e` aborted the release half-done.
    git add CHANGELOG.md pyproject.toml
    git commit -m "chore(release): ${VER}"
    git push -u origin "${BRANCH}"
    gh pr create --base main --head "${BRANCH}" --title "chore(release): ${VER}" --body "${NOTES}"
    echo "When the PR's checks are green, run: just release-4-merge"

# Notes come from CHANGELOG.md, NOT `git-cliff --latest`. Generated output is
# the starting point for an entry, not the entry (see release-2-changelog), so
# publishing it raw contradicts the section a human just curated. v0.2.1 is the
# worked example: cliff listed a uv_build pin that lost a merge and never
# shipped, because the superseded commit is still unreleased-since-the-last-tag.
#
# Merge the release PR once CI is green, tag its merge commit, mint the GitHub release
[group('release')]
release-4-merge:
    #!/usr/bin/env bash
    set -euo pipefail
    BRANCH=$(git branch --show-current)
    if [[ "${BRANCH}" != release/v* ]]; then
      echo "error: not on a release/vX branch" >&2
      exit 1
    fi
    VER="${BRANCH#release/}"
    NOTES=$(just _release-notes "${VER#v}")
    if [[ "$(gh pr view "${BRANCH}" --json state --jq .state)" != MERGED ]]; then
      # Exits non-zero if any check fails, so a red CI stops the release here.
      gh pr checks "${BRANCH}" --watch --fail-fast
      gh pr merge "${BRANCH}" --merge
    fi
    # `gh pr merge` has exited 0 without merging; trust the state, not the exit.
    STATE=$(gh pr view "${BRANCH}" --json state --jq .state)
    if [[ "${STATE}" != MERGED ]]; then
      echo "error: the ${BRANCH} PR is ${STATE}, not merged" >&2
      exit 1
    fi
    MERGE=$(gh pr view "${BRANCH}" --json mergeCommit --jq .mergeCommit.oid)
    git fetch origin
    git switch main
    git merge --ff-only origin/main
    git tag -a "${VER}" -m "${VER}" "${MERGE}"
    # Only this tag: the local clone may still hold the GitLab-era tags, which
    # point into history GitHub does not have.
    git push origin "${VER}"
    gh release create "${VER}" --verify-tag --title "${VER}" --notes "${NOTES}"
    git branch -D "${BRANCH}"
    git push origin --delete "${BRANCH}"
    echo "Released ${VER} on GitHub. Next: just build && just publish"

# Print the curated CHANGELOG.md section for VERSION, without its heading line
# (GitHub titles the release itself).
_release-notes VERSION:
    #!/usr/bin/env python3
    import pathlib, re
    version = "{{VERSION}}"
    text = pathlib.Path("CHANGELOG.md").read_text()
    start = re.search(rf"^## \[{re.escape(version)}\]", text, re.M)
    if start is None:
        raise SystemExit(f"CHANGELOG.md has no [{version}] section — run: just release-2-changelog {version}")
    rest = text[start.end() :]
    following = re.search(r"^## \[", rest, re.M)
    body = rest if following is None else rest[: following.start()]
    body = body.split("\n", 1)[1].strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md's [{version}] section is empty — write it before releasing")
    print(body)

[group('release')]
build:
    uv build

# Publish to PyPI. Not wired into CI on purpose — the first publish claims the
# name permanently, so it stays a deliberate manual act.
[group('release')]
publish:
    uv publish
