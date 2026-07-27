# Release preparation

[README](../README.md) · [简体中文](../README.zh-CN.md) ·
[Getting started](getting-started.md) · [Architecture](architecture.md) ·
[Security](security.md) · [Operations](operations.md)

This document separates local release preparation from remote publication. Project Memory Hub
`0.2.1` is a Public Beta candidate until a maintainer explicitly creates a repository, verifies the
public snapshot, runs hosted checks, and approves a draft release.

No repository URL, hosted CI result, tag, GitHub Release, package-index publication, or public
visibility change is implied by the local steps below.

## Release invariants

A Public Beta release must preserve these product contracts:

- schema migrations remain exactly `0001` through `0013`; static migration and backup evidence is
  accepted only when the WAL/journal gate passes, `quick_check` is `ok`, and `foreign_key_check` is
  empty;
- recall retains the 800-token product ceiling;
- behavior memory remains isolated by `project_id + source_agent + model_id` before retrieval;
- Codex and user-provided ChatGPT exports remain the only ingestion sources;
- Trae, WorkBuddy, Zcode, QoderWork, and Claude Code remain read-only probes;
- CLI JSON values/exit codes and Web routes/status/security boundaries do not drift accidentally;
- proposal application stops at a reviewable local commit and never merges or pushes;
- the default runtime and every live user project remain unchanged by build, smoke, or demo steps.

Any intended change to one of these contracts requires a separate design, migration/compatibility
plan, focused tests, and explicit release notes.

## Prepare the checkout

Use a clean, stable checkout, not a temporary proposal worktree. Confirm the branch and review every
tracked/untracked path before building:

```bash
git status --short --branch
git diff --check
uv lock --check
uv sync --locked --extra test
```

The lockfile must be tracked and agree with `pyproject.toml`. Release metadata must use a neutral
contributor identity, Apache-2.0, the English README, Beta classifiers, and only compatibility claims
that have real artifact evidence.

## Synchronize version and changelog

Before building, confirm that these versions agree:

```bash
uv run python - <<'PY'
import tomllib
from pathlib import Path

metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
package = Path("src/project_memory_hub/__init__.py").read_text(encoding="utf-8")
print(metadata["project"]["version"])
print(package.strip())
PY
```

For this candidate both must identify `0.2.1`. During development, `CHANGELOG.md` remains
`## [0.2.1] - Unreleased`. Before creating the immutable release tag, the maintainer must freeze the
real release date in that heading, rebuild from the resulting commit, and rerun every release gate.
The tag, source tree, changelog, wheel, and sdist must all describe the same version and date. Do not
invent compare links or attach private-history commit hashes.

## Run local quality gates

Run focused tests while developing, then run the complete local gate from the release candidate:

```bash
RELEASE_COMMIT="$(git rev-parse --verify HEAD^{commit})"
uv run playwright install chromium
uv run ruff format --check .
uv run ruff check .
uv run mypy src/project_memory_hub
uv run pytest --cov=project_memory_hub --cov-branch --cov-fail-under=85
node --check src/project_memory_hub/web/static/i18n.js
node --check src/project_memory_hub/web/static/projects.js
node --check src/project_memory_hub/web/static/sources.js
uv run pytest tests/e2e -q
uv run python scripts/verify_wheel.py
uv run python scripts/verify_public_assets.py docs/assets
uv run python scripts/verify_document_links.py
uv run python scripts/verify_workflows.py .github/workflows \
  --secret-scanning .github/secret_scanning.yml
git diff --check
git diff --quiet --exit-code HEAD --
uv run python scripts/verify_release_checkout.py --expected-head "$RELEASE_COMMIT"
```

Record exact commands and outcomes. A check that was not run must be listed as not run; never infer a
hosted result from a local equivalent command.

The browser installation is explicit because neither local nor hosted checks may rely on a runner's
preinstalled Chromium cache. The macOS workflow is release-blocking and owns the complete 85%
branch-coverage gate. Linux uses four independent experimental jobs for portable Python tests,
JavaScript syntax, Chromium E2E, and public-asset privacy, so one failure cannot suppress the other
checks.
Each Linux job uses `continue-on-error` and remains non-blocking. The portable tests use private
home-directory temporary roots (including an explicit E2E `TMPDIR`) and skip only tests that require
macOS `sandbox-exec` or
`fcntl.F_GETPATH`. These skips do not change production runtime logic; the blocking macOS CI still
owns complete validation of the macOS security boundaries. There is no Windows job.

## Build and inspect artifacts

Build into a new temporary directory so stale artifacts cannot satisfy the verifier:

```bash
: "${RELEASE_COMMIT:?capture RELEASE_COMMIT before running the quality gates}"
RELEASE_DIST="$(mktemp -d)"
uv build --wheel --sdist --out-dir "$RELEASE_DIST"
uv run twine check "$RELEASE_DIST"/*
uv run python scripts/verify_wheel.py
PYTHON_311="$(uv python find --system 3.11)"
PYTHON_312="$(uv python find --system 3.12)"
uv run python scripts/verify_release_artifacts.py \
  --dist "$RELEASE_DIST" \
  --smoke-python "$PYTHON_311" \
  --smoke-python "$PYTHON_312"
```

The release verifier must accept exactly one wheel and one sdist, confirm package/version/entry-point
agreement, reject unsafe archive paths, and require all templates, static assets, migrations, and
runtime modules.

The final compatibility gate installs the built wheel—not the checkout—into clean Python 3.11 and
3.12 environments with isolated HOME/config/data/cache locations. It exercises CLI help, version,
init, doctor, and loopback server startup while proving that the user's default runtime did not
change. The verifier accepts only absolute interpreters returned by `uv python find --system`; it
rejects the repository `.venv`, ambient `python`, and any interpreter resolving inside the checkout.

## Verify documentation and synthetic assets

Every public relative link must resolve without following a symlink or escaping the repository. The
only temporary broken-link exceptions before final asset generation are the seven exact asset paths
defined by the public documentation contract. Final release validation permits no missing exception.

Public images are generated, never hand-edited from a live console. Run the isolated generator and
privacy verifier once the final UI is frozen:

```bash
DEMO_ROOT="$(mktemp -d)"
uv run python scripts/generate_demo_assets.py \
  --runtime-dir "$DEMO_ROOT/runtime" \
  --output-dir "$DEMO_ROOT/assets"
uv run python scripts/verify_public_assets.py "$DEMO_ROOT/assets"
```

The final committed set contains three synthetic screenshots, three SVG diagrams, the
[1280×640 social preview](assets/social-preview.png), and a manifest. Screenshots must visibly say
`DEMO DATA`; the route receipt must never include the live Projects page.

Privacy verification checks seed data, final DOM text, SVG, manifest, file limits, raster metadata,
dimensions, allowlisted synthetic identifiers, forbidden local terms, and default-runtime hashes. It
does not claim OCR. Real home paths, project names, tokens, session text, database values, private Git
metadata, or live identifiers block the release.

## Verify workflow policy

Run the repository policy verifier before relying on any workflow file:

```bash
uv run python scripts/verify_workflows.py .github/workflows \
  --secret-scanning .github/secret_scanning.yml
```

Blocking macOS CI must use the locked dependency graph, install Chromium explicitly, and run Ruff,
strict mypy, branch coverage, JavaScript syntax checks, Chromium E2E, package/metadata verification,
both clean built-wheel smokes, documentation checks, and demo privacy verification. CodeQL analyzes
Python and JavaScript. Every third-party action is pinned to a full commit SHA and workflows use
minimal permissions.

The policy rejects remote creation, push, package publication, OIDC publishing, `twine upload`, and
repository-visibility changes. The release workflow accepts only `v*` tags, rebuilds and revalidates
the artifacts, and may create only a draft release after every validation succeeds.

## Prepare a public snapshot

The private development history contains local implementation plans and machine-specific paths. It
must not be pushed as the public repository history.

Prepare an external UTF-8 private-term file before the audit. It must be outside the repository, not
tracked, owned by the current user, a single-link regular file with exact `0600` permissions, and no
larger than 64 KiB. Never print its contents. The tracked allowlist is not a directory exclusion: each
exception binds one exact relative path, full blob SHA-256, explicit rule codes, and a review reason.

Audit an immutable Git commit, then create the local snapshot from the same receipt:

```bash
test -s "${PMH_PRIVATE_TERMS_FILE:?set a non-empty external private terms file}"
SOURCE_COMMIT="$(git rev-parse --verify HEAD^{commit})"
AUDIT_ROOT="$(mktemp -d)"
SNAPSHOT_ROOT="$(mktemp -d)"
uv run python scripts/audit_public_tree.py \
  --mode tree \
  --ref "$SOURCE_COMMIT" \
  --forbidden-file "$PMH_PRIVATE_TERMS_FILE" \
  --allowlist config/public-release-allowlist.toml \
  --receipt "$AUDIT_ROOT/public-tree-receipt.json"
uv run python scripts/prepare_public_snapshot.py \
  --source "$SOURCE_COMMIT" \
  --receipt "$AUDIT_ROOT/public-tree-receipt.json" \
  --branch codex/public-beta-0.2.1 \
  --worktree "$SNAPSHOT_ROOT/worktree" \
  --forbidden-file "$PMH_PRIVATE_TERMS_FILE" \
  --allowlist config/public-release-allowlist.toml
```

The auditor reads paths and blob bytes from the commit object, rejects symlinks, gitlinks, unknown
binary data, unsafe paths, private identifiers, credentials, session/database material, and
unreviewed UUIDs, then reuses the public-asset verifier for PNG metadata and screenshot DOM receipts.
Its receipt contains hashes and counts, not matched private content. The snapshot builder does not
trust the receipt alone: it repeats the same audit before creating a ref.

The resulting repository has one neutral root commit. Compare its tree hash and file inventory to
the audited candidate before adding a remote. It must have no parent and must use
`Project Memory Hub Maintainers <noreply@project-memory-hub.invalid>` for both author and committer.
Its author and committer dates use the source committer's UTC calendar date at `00:00:00Z`. The
source identity, precise time, and original timezone are not copied. This keeps GitHub's file-age
display current while retaining deterministic snapshot identities and date-level privacy.
The private source repository, current branch, index, tags, remotes, and history remain unchanged.
The builder may create only the fixed local branch and its new external worktree; it never adds a
remote, pushes, tags, merges, invokes GitHub CLI, or creates a Release.

## Remote actions require a maintainer

The following actions are outside local build authority and require the repository owner to review
the final snapshot and approve them separately:

- create or select the real GitHub repository;
- add the verified remote identity;
- choose public/private visibility;
- enable Issues, private vulnerability reporting, secret scanning, push protection, and repository
  moderation settings;
- configure rulesets and required hosted checks;
- push the one-root snapshot branch or a version tag;
- inspect hosted macOS CI and the explicitly non-blocking Linux experimental job;
- upload the verified social preview;
- create, review, or publish a draft GitHub Release.

Do not fill README, package metadata, templates, or changelog with a repository URL until that
identity exists. Do not claim hosted CI passed before the actual remote run is visible.

After the repository exists, an administrator must enable GitHub secret scanning and push
protection, then confirm zero unresolved alerts before publication. The tracked
`.github/secret_scanning.yml` starts with `paths-ignore: []`; it does not itself enable those remote
settings. A synthetic false positive may be excluded only by one exact file path plus a reviewed
full SHA-256 and reason in the public allowlist—never by a directory or wildcard.

## Draft release and checksums

After the tag-triggered release workflow's own blocking verification job passes, it may build the
wheel and sdist again, verify them, create deterministic SHA-256 checksums, and attach only those
verified files to a draft GitHub Release. Creating that draft does not imply the independent hosted
macOS CI or CodeQL workflows have passed. The draft remains unpublished until a maintainer checks
their real hosted results plus version text, changelog, assets, checksums, compatibility evidence,
security settings, and the public tree.

The tag must be `v<pyproject version>` (the workflow trigger is `v*`). Reproduce the checksum file
locally from the already verified artifact pair:

```bash
uv run python scripts/create_checksums.py "$RELEASE_DIST" --tag "v0.2.1"
(cd "$RELEASE_DIST" && shasum -a 256 -c SHA256SUMS)
```

The generator writes a stable, sorted `SHA256SUMS` covering exactly one wheel and one sdist. The
release workflow uses `gh release create ... --draft`; it does not publish the draft automatically.

No PyPI upload is part of the Public Beta workflow. Do not configure an upload token, trusted
publisher, package-index credential, OIDC publication, or `twine upload` step.

## Failure and rollback

If a local gate fails before a tag exists:

1. stop before tag, push, visibility change, or draft publication;
2. preserve the failing artifact and sanitized reason locally;
3. fix the source and rerun the complete affected gate from a new build directory;
4. regenerate public assets after any presentation change;
5. rebuild the public snapshot after any tracked-tree change;
6. never replace a failed artifact in place while retaining an old checksum.

If a hosted tag workflow fails after its tag was pushed, do not move, reuse, or force-push that tag.
Fix the source, increment the version, freeze a new changelog entry, rerun the full gate, and create a
new tag. Preserve the failed tag as evidence unless the repository owner explicitly authorizes its
deletion before any release publication. If a draft release already exists, keep it draft or delete
the draft assets through an explicitly authorized maintainer action. Do not rewrite private
development history or modify the user's stable runtime as a release recovery shortcut.

## Release record

Record only evidence that actually exists:

- source tree/commit identity used for the audited snapshot;
- version and changelog state;
- exact local commands and outcomes;
- hosted workflow URLs after they exist;
- wheel, sdist, and SHA-256 values;
- supported and experimental compatibility results;
- synthetic asset manifest and privacy result;
- known risks, deferred work, and rollback decision.

Do not copy tokens, local absolute paths, user project names, raw doctor output, session content,
database contents, or private Git metadata into the release record.
