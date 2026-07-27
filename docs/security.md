# Security architecture

[README](../README.md) · [简体中文](../README.zh-CN.md) ·
[Getting started](getting-started.md) · [Architecture](architecture.md) ·
[Operations](operations.md) · [Security policy](../SECURITY.md)

Project Memory Hub handles local project paths and structured development experience. Its security
model minimizes collection, isolates behavior before retrieval, treats every imported or direct
claim as untrusted until the relevant boundary verifies it, and fails closed when file identity or
authorization changes.

## Threat model

The supported deployment is one macOS user running one local Project Memory Hub runtime. The design
assumes that user intentionally grants access to selected project roots and local Codex sessions.

The product defends against:

- malformed, truncated, oversized, or unexpectedly nested JSON;
- malicious or malformed ChatGPT export archives;
- path traversal, unsafe archive members, symlinks, hardlinks, and special files;
- project, Git worktree, ref, or file identity changing between validation and use;
- accidental behavior-memory disclosure across projects, sources, or exact model IDs;
- untrusted direct capture claiming a different Codex namespace;
- non-loopback Web exposure, hostile Host/Origin values, CSRF, session misuse, and oversized forms;
- partial stage failures, checkpoint drift, duplicate imports, and unsafe retry replay;
- diagnostics accidentally reflecting exception text, request bodies, tokens, or captured content.

Project Memory Hub is not an operating-system sandbox. A malicious process already able to read the
same macOS user's files can read the runtime database or access token. The product also cannot erase
copies already made by filesystem snapshots, backup tools, disk recovery, or another process.

## Data minimization

The memory database stores bounded project facts and redacted structured behavior fields. It does
not copy raw Codex or ChatGPT conversation bodies into behavior memory. It does not store browser
cookies, account credentials, full environment variables, command stdout/stderr, exception
representations, or the local Web token as memory content.

Project scanning intentionally excludes common secret names and unbounded content, including `.env`
variants, private keys, credentials, dependency directories, and build output. The scanner observes
only configured roots and returns explicit permission diagnostics for paths it cannot read.

Codex and ChatGPT input is parsed locally. Runtime behavior does not require a remote vector store,
embedding API, additional model API key, browser scraping, or a continuous account connection.

## Private runtime and file permissions

The default runtime is:

```text
~/Library/Application Support/Project Memory Hub
```

The runtime root and its private subdirectories use mode `0700`. Ordinary files such as
`config.toml`, `memory.db`, `access-token`, and SQLite backups use mode `0600`. Existing unsafe owner,
mode, symlink, hardlink, or file-type conditions are rejected rather than repaired by broadening
access.

A custom global `--config` makes the configuration parent the runtime root. It does not weaken path
validation. Operators should use a dedicated private directory and must not place a runtime in a
shared checkout, cloud-synchronized public folder, or another user's directory.

Configuration rewrites are anchored to an opened parent directory, require one owner-controlled
regular target with no hard links, serialize cooperating writers with an advisory lock on that
directory, compare the caller's revision before replacement, revalidate the target and parent around
the atomic replace, and fsync both file and directory. Identical private Setup or Settings
submissions are zero-write; a concurrent managed change returns a conflict instead of silently using
last-write-wins. A matching legacy-readable file is tightened to `0600`. If replacement succeeds but
the final parent check or directory fsync fails, the write is reported as an uncertain commit rather
than falsely reported as an untouched configuration; reload and diagnose the filesystem before
retrying.

An installed `doctor` can recover a stable local source checkout only from the installed
distribution's PEP 610 `direct_url.json`. The adjacent `METADATA` and `RECORD` must bind the exact
loaded module and direct-URL bytes by name, version, SHA-256, and size; unsafe, duplicated, malformed,
unbounded, hard-linked, symlinked, or `.worktrees` provenance fails closed. A valid archive, VCS, or
ordinary index-wheel installation does not prove a local source checkout and therefore keeps
source-bound integrations optional. Codex automation metadata only checks an already trusted root:
automation cwd is never accepted as source provenance. Graphify hook inspection never launches the
Graphify executable. It pins the repository, parses only a bounded no-follow `.git/config`, permits
only repository-contained `core.hooksPath` values (including Husky's `_` convention), and holds both
hook files open while checking their exact markers and path identities again before accepting them.

## Project and namespace isolation

Behavior candidate retrieval always includes:

```text
project_id + source_agent + model_id
```

Filtering occurs in the storage query before ranking. An equal model label from Codex and ChatGPT
does not make the namespaces equal. A different exact model ID under Codex is also a different
namespace. If a ChatGPT export lacks a safe model slug, records use
`source_agent=chatgpt + model_id=unknown`. Multiple unknown slugs share that fallback namespace, so
it preserves source isolation but cannot prove exact-model separation. The importer does not infer
or synthesize a more precise identity.

The current Codex namespace is resolved from `CODEX_THREAD_ID`, task cwd, and bounded local session
metadata. Ordinary recall repeats that check before querying. Direct capture remains pending until a
later adapter pass validates the real task lifecycle and structured content fingerprint. A supplied
correlation ID is not a trust credential.

The pending correlation ID is stored separately from the adapter's trusted source record ID. It is
bound only when that trusted record actually verifies the pending row, or during an exact forensic
pending recovery. A conflicting correlation is rejected atomically; matching only project, model,
or content hash is never enough to deduplicate a different task.

Project lookup uses registered canonical path identity. A replaced path, changed generation, stale
relink, or inconsistent Git identity aborts the affected operation instead of falling back to an
outer or similarly named project. On macOS, persisted identity accepts device-number renumbering
only for the same canonical path and unchanged directory inode. Different inodes, symlink retargets,
non-macOS device changes, and any identity change during one operation remain fail-closed.

## Source boundaries

Codex ingestion incrementally reads local session JSONL. A partial trailing record is not trusted or
checkpointed until complete. A fully delimited malformed record invalidates that lifecycle with a
stable content-free warning.

Deferred Codex locators contain no transcript payload. macOS device-number renumbering is tolerated
only for the same fixed session scope and inode after the stored prefix SHA-256 matches. File
replacement, inode change, prefix change, or any open/reopen identity drift still rejects replay.

ChatGPT ingestion accepts only a user-selected official export ZIP. It checks archive path traversal,
member types, compression ratio, member and total size, JSON structure, conversation depth, node
count, and text limits. The original ZIP is not moved, modified, or deleted. Ambiguous project or
model matches require local confirmation.

Trae, WorkBuddy, Zcode, QoderWork, and Claude Code have no behavior-ingestion capability in this
Beta. Their optional probes run in a separate zero-write container, use fixed trusted anchors and
bounded metadata inspection, and do not open the Project Memory Hub runtime. Probe results are not
saved as behavior memory and never unlock an Enable or Import action.

## Local Web boundary

The control panel binds only to IPv4/IPv6 loopback aliases accepted by the security layer. Wildcard,
LAN, and public bind addresses are rejected. Host is validated before routing; unsafe requests also
require an allowed Origin and a session-bound CSRF value.

The initial access token is generated from cryptographic random bytes and stored in a separate
`0600` file. A valid bootstrap request redirects immediately to a URL without the token and sets an
HttpOnly, SameSite=Strict local session cookie. Do not paste the bootstrap URL into issues, logs, chat,
shell transcripts, or screenshots.

Request bodies, multipart forms, field counts, individual fields, and JSON nesting are bounded.
Authentication, validation, conflict, and internal failures return fixed allowlisted text and status
codes; exception details, request bodies, paths, tokens, and user content are not rendered.

The `/setup` POST routes add a 256 KiB route-specific limit, accept only URL-encoded forms, and use
an exact field allowlist. They do not accept files, JSON, redirect targets, tokens, `model_id`, or
optional probe sources. Only Codex and ChatGPT can enter `enabled_sources`; exact model namespaces
remain resolved later from trusted task metadata.

The Web process can update private application configuration and approved local lifecycle state. It
cannot edit Codex automation files directly, expose a non-loopback API, merge proposal branches, or
push a repository.

## Archive and structured-input safety

Archive extraction rejects absolute paths, parent traversal, repeated unsafe members, symlink-like
entries, non-regular content, excessive expansion, and size-budget violations. Paths are validated
before any extraction target is opened.

Structured JSON readers apply byte, item, depth, string, and key limits. Invalid UTF-8 and malformed
records fail with stable reason codes rather than echoing input. Where partial recovery is safe,
unrelated valid projects or later source lifecycles continue; transaction boundaries prevent a
checkpoint from claiming a write that did not commit.

## Git proposal safety

Improvement proposals do not grant repository authority. Creation, approval, apply, rollback, merge,
and push are independent actions.

An approved apply validates the repository identity, clean baseline, exact proposal state, target
paths, patch size, Git ref, and private worktree before executing allowlisted verification. Absolute
paths, traversal, `.git` targets, symlinks, hardlinks, special files, worktree/ref races, and cleanup
identity drift fail closed. A failed apply attempts bounded cleanup and records a stable outcome; it
does not reset the user's branch or delete an arbitrary directory.

A successful apply creates a local commit on an isolated `codex/` branch. Human review is required
before merge or publication.

## Safe diagnostics and retry

Public CLI and Web errors expose fixed codes, bounded counts, and remediation hints. They do not
include raw exception strings, capture payloads, archive contents, environment variables, or command
output. `doctor` is read-only, but its machine-readable result can include local operational paths;
sanitize it before sharing and prefer reporting only overall status and check codes.

Retry items contain canonical redacted capture fields under size and age limits. Corrupt queue files,
project identity drift, privacy rejection, and repeated failures are quarantined or reported without
leaking their payload. Never attach the retry directory or SQLite database to a public issue.

## Deletion and backups

Archive keeps a record locally but excludes it from recall. Delete moves a record to the deleted
lifecycle and hides it from normal UI and recall queries. Neither operation promises immediate
physical overwriting of SQLite pages, WAL files, filesystem snapshots, or existing backups.

To remove all local data, stop every writer, remove managed Codex integration and optional host
automation, then follow the exact uninstall procedure in [Operations](operations.md). Use SQLite's
online backup API for backups; copying an active WAL database is not a consistent backup.

## Public assets and issue reports

Public screenshots must be produced only by the isolated synthetic demo pipeline. The pipeline must
not access the default runtime or live Projects page and must scan the final DOM, SVG, manifest, and
raster metadata for real home prefixes, project names, tokens, live identifiers, or private phrases.

When reporting an ordinary bug, do not include absolute local paths, access tokens, session
transcripts, ChatGPT exports, database contents, private screenshots, model credentials, or raw logs.
Use fictional paths and synthetic reproduction data.

Security vulnerabilities must not be disclosed in a public issue. Follow the conditional private
reporting instructions in [SECURITY.md](../SECURITY.md). Private vulnerability reporting is a GitHub
repository setting, not something this local application can enable.

## Operational response

- Run `memory-hub doctor --format json` locally, but share only sanitized status/check codes.
- Stop `serve`, reconcile, and host automation before database restore or schema recovery.
- Preserve the original malformed export or session locally without uploading it.
- Do not use `sudo`, recursive permission broadening, checkpoint deletion, or manual SQL cleanup as a
  first response.
- Keep a private SQLite backup before migration or recovery; after the non-empty WAL/journal gate,
  accept it as verified only when `quick_check` is `ok` and `foreign_key_check` is empty.

See [Operations](operations.md) for exact commands and [Architecture](architecture.md) for trust and
transaction boundaries.
