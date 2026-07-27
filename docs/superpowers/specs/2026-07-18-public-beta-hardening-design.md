# Project Memory Hub 0.2.1 Public Beta Hardening Design

**Status:** Approved for implementation planning

**Date:** 2026-07-18

**Release posture:** Public Beta

**License:** Apache-2.0

## 1. Summary

This work prepares Project Memory Hub for a credible public GitHub Beta without changing
the memory engine's behavior. It improves first-run usability, documentation, packaging,
compatibility evidence, repository governance, and release safety around the existing
local-first product.

The release presentation uses English as the primary GitHub entry point and provides a
complete Simplified Chinese edition. The local web console keeps its existing English and
Chinese switch.

## 2. Locked decisions

The following decisions are part of the approved design:

- Use the balanced Public Beta approach rather than a documentation-only release or a
  cross-platform rewrite.
- Keep macOS as the only officially supported operating system.
- Treat Linux as experimental and only claim the checks that actually pass in CI.
- State that Windows is unsupported while the runtime depends on POSIX-only facilities.
- Publish under Apache-2.0.
- Release package version `0.2.1` with a Beta maturity classifier and label, not a
  PEP 440 prerelease suffix, and require no database migration.
- Use an English primary README with a prominent link to a complete Chinese README.
- Generate screenshots from an isolated synthetic runtime, never from a user's runtime.
- Preserve the private local Git history and prepare a separate sanitized public snapshot.
- Do not automatically publish to PyPI, create a remote, push, merge, or expose credentials.

## 3. Goals

### 3.1 Usability

- Let a new user understand the product, its boundaries, and its supported sources before
  encountering implementation protocol details.
- Provide a five-minute installation path with explicit success criteria.
- Make empty states, common failures, source status, and exact-model selection actionable.
- Reduce information density on Sources and large-project views without changing their
  underlying actions or data.

### 3.2 Ecosystem compatibility

- Align public compatibility claims with executable evidence.
- Verify supported Python versions through built artifacts, not source-tree imports alone.
- Make contributor and CI dependency resolution reproducible.
- Separate official macOS support from experimental Linux signals and unsupported Windows.

### 3.3 GitHub release readiness

- Add a valid open-source license, complete package metadata, governance documents, CI,
  release checks, and contribution templates.
- Add deterministic, privacy-safe product visuals and architecture diagrams.
- Prevent private paths, identifiers, tokens, user projects, or personal Git metadata from
  entering the public release snapshot.

## 4. Non-goals and frozen behavior

This project does not change:

- database schema, migrations, memory kinds, lifecycle rules, or compaction semantics;
- recall ranking, token accounting, or the 800-token product ceiling;
- the `project_id + source_agent + model_id` behavior-memory namespace;
- SQL pre-filtering of private behavior memories;
- Codex or ChatGPT ingestion semantics;
- the status of Trae, WorkBuddy, Zcode, QoderWork, or Claude Code as read-only probes;
- source enablement, path allowlists, probe boundaries, or model-verification rules;
- CLI JSON fields, stable error codes, exit codes, or command meanings;
- Web routes, HTTP status codes, CSRF, loopback binding, token bootstrap, or session rules;
- approval requirements for promotions and self-improvement proposals;
- the prohibition on automatic merge, push, external upload, or unapproved mutation.

No new ingestion adapter, cloud service, account login, browser scraping, model API, or
cross-model memory sharing is introduced.

## 5. Architecture

The existing engine remains the center of the system. Four release-facing layers are added
around it:

1. **Usability presentation** — onboarding guidance, clearer text-mode errors, bilingual
   static error pages, scannable source/project views, and exact-model instructions.
2. **Documentation and demo assets** — public README editions, operator/developer docs,
   synthetic screenshots, architecture diagrams, and a social preview.
3. **Packaging and compatibility evidence** — package metadata, reproducible locks, build
   checks, artifact installation smoke tests, and an honest platform matrix.
4. **GitHub governance and release automation** — license, contribution/security policy,
   issue templates, CI, dependency updates, and draft-only release automation.

These layers may read existing public service/view models but must not introduce a second
memory path or bypass the existing container and security boundaries.

## 6. User experience design

### 6.1 README information architecture

The primary `README.md` is English and follows this order:

1. language switch and Beta label;
2. one-sentence product value and a synthetic Overview hero image;
3. four core benefits: local-first, strict isolation, verified memory, approval-gated change;
4. five-minute quick start with success criteria;
5. supported-source and platform matrices;
6. concise privacy and non-goal summary;
7. architecture and model-isolation diagrams;
8. links to detailed usage, operations, security, contributing, and design documentation.

`README.zh-CN.md` mirrors the same structure and facts in Simplified Chinese. Detailed
capture markers, locator limits, resolution state machines, backup/restore procedures, and
probe internals move to focused documents instead of occupying the README first screen.

End-user installation uses a normal built/local package path. Editable force installation
is documented only for contributors.

### 6.2 Overview onboarding

The Overview page adds a presentation-only "Next safe step" panel derived only from state
already present in the existing Overview view model. It shows one command, the reason for
it, and a success condition without adding a new runtime probe or backend action.

Examples include:

- no registered project: preview discovery with `discover --dry-run`;
- project without facts: scan a specific project in dry-run mode first;
- healthy runtime: show the most recent reconcile and health-check path.

The static first-run checklist may document the AGENTS integration command, but the Overview
request does not run doctor or inspect additional host files to personalize that checklist.

The Web process never executes those terminal commands.

### 6.3 Empty states and navigation

- Empty states explain why no content is shown and provide one bounded next step.
- The current navigation item receives `aria-current="page"` and a visible selected state.
- Exact identifiers remain available in details but are not the dominant visual label.
- Timestamps may gain a readable presentation while preserving the exact value in details.

### 6.4 Sources presentation

The twelve-column source table is replaced or supplemented by two honest groups:

- **Ingestion sources:** Codex and user-provided ChatGPT official exports.
- **Read-only probes:** Trae, WorkBuddy, Zcode, QoderWork, and Claude Code.

Each summary shows availability, access, model verification, behavior-import capability,
and the allowed action. Technical warning codes remain accessible in an expanded detail.
No locked source gains an Enable or Import action.

### 6.5 Projects presentation

Large project collections gain client-side search, status filters, a result count, and
client-side pagination or progressive disclosure over the already-rendered safe view model.
Full paths are collapsed until requested. No project API, route, query, or mutation is added.
All server-side project operations, identifiers, confirmation rules, and status values remain
unchanged.

### 6.6 Memories exact-model guidance

The Memories page explains how to obtain the exact current model ID with
`memory-hub codex-context`. It does not enumerate every stored model namespace, infer model
names, prefill a broader namespace, or expose cross-model metadata.

### 6.7 CLI and Web error presentation

Text-mode CLI errors retain the stable code and add a redacted message plus a safe hint.
JSON output, exit codes, exception mapping, and redaction remain unchanged.

Successful text-mode initialization adds a short ordered next-step checklist. Its JSON output
and initialization behavior remain unchanged.

Generic Web errors use a same-origin bilingual template with allowlisted static copy, the
existing HTTP status, and a link back to Overview. The template never renders exception
details, request bodies, paths, tokens, or user input.

## 7. Synthetic demo and visual assets

### 7.1 Isolation

The demo builder uses an explicitly supplied temporary configuration and runtime directory.
It must fail closed if the target resolves to the default runtime, an existing user database,
a symlink, or a non-empty unapproved directory.

The demo contains fictional projects, model IDs, memories, source states, and proposals. A
visible `DEMO DATA` label appears in screenshots.

### 7.2 Assets

The public asset set includes:

- Overview hero screenshot;
- Sources screenshot;
- Memories screenshot;
- local data-flow diagram;
- strict model-isolation diagram;
- approval-gated improvement diagram;
- 1280 by 640 social-preview PNG.

Raster screenshots use PNG or WebP as appropriate. Diagrams use SVG. The visual language
reuses the existing paper, ink, safe-green, warning-red, straight-border, and editorial
ledger system.

### 7.3 Privacy scan

Generation fails if text or image-adjacent metadata contains a real home-directory prefix,
known local project names, access-token material, UUIDs copied from the live runtime, private
conversation content, or configured forbidden terms. The scan runs locally and in CI.

The generated HTML and image metadata must also be inspected. Public screenshots never use
the live Projects page.

## 8. Packaging and compatibility

### 8.1 Package metadata

`pyproject.toml` gains:

- description and README metadata;
- Apache-2.0 SPDX license expression;
- neutral contributor attribution;
- keywords and classifiers;
- an operating-system classifier that does not imply Windows support;
- Python-version classifiers that match the verified CI matrix.

Repository URLs are added only after a real remote exists. The wheel verifier derives the
expected version from the package metadata instead of hard-coding it.

### 8.2 Reproducible dependencies

The application lockfile is committed. CI verifies the lock before syncing. Release artifacts
continue to declare supported dependency ranges; the lock controls contributor and CI
reproducibility rather than pinning downstream users to one environment.

### 8.3 Platform policy

- **macOS:** supported and release-blocking.
- **Linux:** experimental. Only checks that execute successfully may be advertised.
- **Windows:** unsupported while POSIX-only imports and behavior remain required.
- **Browser:** Chromium is the verified browser until another browser has equivalent E2E
  evidence.

The Python requirement range is finalized from the clean artifact-install matrix. An upper
bound is added if the next interpreter release is not verified.

## 9. GitHub governance

The repository gains:

- `LICENSE` with the Apache-2.0 text;
- `SECURITY.md` using GitHub private vulnerability reporting;
- `CONTRIBUTING.md` with setup, tests, privacy rules, and commit expectations;
- `CODE_OF_CONDUCT.md`;
- `CHANGELOG.md` following a consistent release format;
- bug and feature issue forms;
- a pull-request template;
- Dependabot configuration;
- CI, security-analysis, and draft-release workflows.

Templates must ask contributors to remove paths, tokens, session text, database contents,
and screenshots containing private data.

## 10. CI and release workflow

### 10.1 Required checks

The blocking macOS workflow runs:

- lock validation and dependency sync;
- Ruff format and lint checks;
- strict mypy;
- the full branch-aware pytest suite with an 85% gate;
- JavaScript syntax/static contract checks;
- Chromium E2E;
- wheel and sdist build;
- metadata validation;
- clean artifact-install and CLI smoke tests;
- demo-asset privacy verification;
- repository link and packaging contract checks.

Python versions are included only after the setup and artifact smoke test are proven. Linux
runs in a clearly named experimental job and does not create a misleading support badge.

### 10.2 Security automation

CodeQL and Dependabot are enabled. Secret-scanning configuration uses narrow exact-fixture
allowances where test vectors resemble credentials; it must not ignore entire test folders.

### 10.3 Draft release

A version tag builds wheel, sdist, and SHA-256 checksum files. The workflow validates and
attaches them to a draft GitHub Release. It does not upload to PyPI. Any build, metadata,
installation, privacy, or checksum failure prevents release creation.

## 11. Public-history strategy

The existing local history is preserved because it contains useful private development
context. It is not pushed directly.

After the tracked tree is sanitized and verified, a separate worktree prepares a flattened
public snapshot on a `codex/` release branch. The snapshot contains the reviewed public tree
and public-safe authorship metadata, without the private historical commits.

Creating a GitHub remote, filling exact project URLs, pushing the snapshot, and changing
repository visibility are separate external actions. They require the real repository
identity and a final privacy check; no workflow performs them implicitly.

## 12. Failure handling

- A core-contract regression removes or revises the presentation change; tests are not
  rewritten to bless an unintended behavior change.
- Demo generation refuses the default runtime and cleans incomplete output.
- Privacy-scan failure blocks screenshots, commits, and release artifacts.
- An experimental Linux failure remains visible and does not become a support claim.
- A release failure leaves no published release and never falls through to PyPI.
- The current local `main` remains recoverable regardless of public-snapshot preparation.

## 13. Test strategy

### 13.1 Frozen contracts

Tests assert the schema version, source capabilities, strict namespace filtering, recall
ceiling, CLI JSON and exit-code contracts, Web route/status contracts, and security headers.

### 13.2 New focused tests

New tests cover:

- text-mode CLI code, message, and hint output with redaction;
- bilingual allowlisted Web error pages;
- Overview next-step state selection;
- Sources grouping without new actions;
- Projects presentation filters and path disclosure;
- Memories exact-model instructions;
- demo-runtime refusal and isolation;
- deterministic screenshot seed data;
- privacy scanning of assets and tracked public files;
- package metadata, lockfile, workflows, templates, and documentation links.

Production presentation changes follow red-green-refactor. Documentation, configuration,
workflow, and generated-asset changes use contract tests plus real build/runtime checks.

### 13.3 Full validation

The final validation includes:

- full pytest with branch coverage;
- Ruff format and lint;
- strict mypy;
- JavaScript checks;
- Chromium E2E;
- wheel verification and wheel/sdist build;
- metadata validation and clean artifact install;
- demo generation and privacy scan;
- `memory-hub doctor` on the stable local installation;
- Graphify update and hook verification;
- clean Git status and diff checks.

## 14. Acceptance criteria

The work is complete when:

- both README editions independently explain and complete installation;
- a clean environment can install the built artifact, initialize, run doctor, and start the
  loopback console;
- every empty state explains cause, next step, and success condition;
- Sources visibly distinguishes ingestion from read-only probes;
- exact-model guidance does not weaken namespace isolation;
- all committed screenshots come from synthetic data and pass privacy checks;
- existing tests and all new tests pass with branch coverage at or above 85%;
- doctor remains fully healthy on the supported local installation;
- package metadata and compatibility claims exactly match verified evidence;
- the repository contains the approved license, governance, CI, templates, changelog, and
  reproducible dependency baseline;
- package version `0.2.1`, labeled Beta through project metadata and documentation, requires
  no data migration;
- no remote, push, merge, PyPI upload, or public release occurs implicitly.

## 15. Risks and mitigations

### Presentation changes accidentally alter behavior

Mitigation: freeze JSON, exit-code, route, HTTP-status, namespace, source-capability, and
schema contracts before implementation.

### Demo tooling touches a real runtime

Mitigation: require an explicit isolated target, reject default/existing/symlinked targets,
seed only fictional records, and verify post-run inventory.

### Public artifacts leak local information

Mitigation: sanitize tracked documents, build from synthetic data, scan text/metadata, and
prepare a flattened public snapshot instead of publishing private history.

### Compatibility marketing exceeds evidence

Mitigation: generate the public matrix from executed CI results and keep unsupported or
experimental labels explicit.

### Release automation publishes an irreversible artifact

Mitigation: draft-only GitHub releases and no PyPI credentials or upload step.

## 16. Implementation waves

This design is intentionally delivered as separate, independently verifiable work units:

1. **Public foundation:** license, package metadata, lockfile, README editions, focused docs,
   governance files, and static release-contract tests.
2. **Synthetic demo pipeline:** isolated seed data, privacy scanner, diagrams, screenshots,
   and asset-contract tests.
3. **Usability presentation:** text-mode hints, static error shell, Overview guidance,
   Sources grouping, Projects client-side controls, exact-model guidance, and focused tests.
4. **CI and release automation:** compatibility matrix, build/install smoke tests, security
   automation, and draft-release workflow.
5. **Public snapshot preparation:** tracked-tree sanitization, final privacy audit, and an
   isolated flattened branch prepared without creating or pushing a remote.

Each wave must pass its focused checks before the next wave starts. Full validation and the
public snapshot occur only after all preceding waves are green.
