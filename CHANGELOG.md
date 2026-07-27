# Changelog

All notable changes to Project Memory Hub are recorded in this file. The project uses semantic
version numbers and keeps unreleased work clearly separate from an actual publication.

## [0.2.1] - Unreleased

**Public Beta candidate.** No GitHub Release or PyPI publication is claimed by this entry.

### Added

- A resumable, idempotent first-run setup flow shared by `memory-hub setup` and the bilingual local
  Web console, with explicit Codex/ChatGPT selection and honest automation handoff.
- English and Simplified Chinese public entry points with focused getting-started, architecture,
  security, operations, and release documentation.
- Repository governance, structured issue forms, and an honest pull-request verification template.
- Reproducible contributor and release-validation dependencies in the tracked lockfile.

### Changed

- Package metadata now identifies the project as a Beta licensed under Apache-2.0 and limits claims
  to verified Python and platform targets.
- Release verification derives the expected package version from project metadata.
- Schema v11 separates the active pending-verification queue from bounded, payload-free terminal
  history. Verified, expired, and rejected rows no longer consume active queue capacity.
- Schema v12 stores the local task correlation separately from the trusted adapter source record,
  preserving exact duplicate detection after bounded history eviction without conflating tasks
  that produced identical content.
- Schema v13 binds every deferred Codex locator to the exact parser-policy SHA-256. Locators created
  before that provenance existed are invalidated instead of being replayed under changed semantics;
  a dedicated Codex catch-up marker keeps reconcile due until the trusted adapter successfully
  rescans them from policy-invalidated checkpoints.
- Managed Codex writes use the narrow stdio MCP broker for `capture_pending_v1` and
  `reconcile_if_due_v1`; recall remains a strict read-only CLI operation.
- Persisted project lookup tolerates macOS device-number renumbering only when the canonical path
  and directory inode remain unchanged; discovery refreshes the stored device number.
- Deferred Codex replay applies the same macOS-only device-renumbering rule to a fixed session
  scope, and still requires the stored inode and exact prefix SHA-256 before recovery.
- Codex runtime and incremental parsing now accept fully parsed, known non-semantic records in a
  separate bounded window while keeping lifecycle records, malformed JSON, duplicate keys, and
  unknown record kinds fail-closed at the original semantic limit.
- Incremental Codex ingestion now accepts same-inode append-only growth while binding each batch to
  its prior checkpoint prefix, consumed snapshot bytes, and reopened prefix hash. A database
  snapshot or checkpoint race is retried once; truncation, replacement, and prefix rewrites remain
  fail-closed. An already receipted Codex record whose content, project, model, and source
  provenance still match is treated as an idempotent duplicate even when an earlier verified event
  timestamp is replayed; this exception is explicitly unavailable to pending-capture promotion.
- Managed Codex and daily-reconcile guidance now report the exact PMH stage, confirmed evidence or
  an explicitly unconfirmed cause, actual record state, code-commit impact, remediation, and any
  required user action instead of a generic unavailable message.
- Public snapshot commits now use a privacy-normalized UTC date from the audited source commit
  instead of the year 2000, so GitHub no longer labels newly published files as decades old.

### Security

- Setup uses configuration compare-and-swap, dirfd-anchored atomic writes, a route-specific 256 KiB
  URL-encoded form limit, existing authentication and CSRF boundaries, and never writes Codex
  automation files or accepts optional probe sources as ingestion adapters.
- Public contribution paths explicitly reject local paths, credentials, session material, database
  contents, and private images.
- Vulnerability reporting is conditional on an enabled GitHub private vulnerability report rather
  than a fictional public or personal contact channel.
- Terminal pending history stores provenance metadata without `structured_payload_json` and is
  deterministically bounded to 50,000 rows.
- A trusted source can bind only the correlation of the pending row it actually verified; ordinary
  source replay cannot attach itself to a later pending declaration, and conflicting forensic
  correlation bindings fail closed.
- Directory replacement, inode drift, symlink retargeting, and non-macOS device drift still fail
  closed; in-process identity snapshots always require the exact device/inode tuple.

## [0.2.0]

### Added

- Local-first capture, verified recall, strict project/source/model isolation, and bounded Codex and
  user-selected ChatGPT ingestion.
- Loopback-only review console, approval-gated proposals, backups, reconciliation, and local
  operational diagnostics.

This section documents the existing local Beta baseline; it does not assert a remote publication.
