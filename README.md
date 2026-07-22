[简体中文](README.zh-CN.md)

**0.2.1 · Public Beta candidate · Unreleased**

# Project Memory Hub

Local-first, model-isolated project memory for Codex sessions and user-provided ChatGPT exports.

Project Memory Hub turns observable development outcomes into a compact brief for the next task. It
runs on your Mac, keeps behavior memory separated by project, source, and exact model, and never
pretends to recover a model's hidden chain of thought.

![Synthetic Project Memory Hub Overview with a visible DEMO DATA label](docs/assets/screenshots/overview.png)

## Why Project Memory Hub

- **Local-first.** Configuration, SQLite data, access credentials, and backups stay in a private
  local runtime. The application does not require an embedding service, vector database, or extra
  model API key.
- **Strict isolation.** Behavior memory is selected by
  `project_id + source_agent + model_id` before retrieval, so one source or model does not silently
  inherit another one's working habits.
- **Verified memory.** It records explicit outcomes, decisions, failed attempts, verification
  commands, risks, open issues, and reusable lessons. Direct Codex capture remains pending until the
  local session adapter verifies its provenance.
- **Approval-gated change.** Shared rules and improvement proposals require local review. An approved
  code proposal is applied only in an isolated branch and private temporary worktree; the tool never
  merges or pushes it for you.

## Five-minute quick start

### Requirements

- macOS
- Python 3.11 or Python 3.12
- [uv](https://docs.astral.sh/uv/)
- A normal, stable local clone of this repository, not a temporary `.worktrees` checkout

Install the local package and initialize its private runtime:

```bash
uv tool install .
memory-hub version
memory-hub init --format json
```

`memory-hub version` should print `0.2.1`; initialization should return
`{"status":"initialized"}`.

Configure the first run without editing TOML. The example keeps only one reviewed root while leaving
Codex and ChatGPT enabled:

```bash
memory-hub setup --project-root "$HOME/Documents" --source codex --source chatgpt --complete --format json
```

Running `memory-hub setup --format json` without configuration options is a zero-write status check.
Setup preserves behavior isolation by project + source + exact model ID. It does not edit Codex
automation TOML; creating or repairing the daily task remains an explicit action in the authorized
Codex host.

Preview project discovery before writing the registry, then register the reviewed candidates:

```bash
memory-hub discover --dry-run --format json
memory-hub discover --format json
memory-hub doctor --format json
```

A successful discovery reports `status: "ok"`. Doctor may report a non-blocking `warn` when an
optional host integration is absent; investigate any `fail` before continuing.

The non-dry-run discovery command registers every candidate returned by that discovery run; it is
not an interactive per-project picker. Adjust the configured roots until the dry-run inventory is
acceptable before applying it.

Connect Codex only after reviewing the managed `AGENTS.md` change:

```bash
PMH_LAUNCHER="$(realpath "$(command -v memory-hub)")"
PMH_PYTHON="$(dirname "$PMH_LAUNCHER")/python"
test -x "$PMH_PYTHON"
codex mcp add project-memory-hub -- \
  "$PMH_PYTHON" -m project_memory_hub.integration.mcp_broker
codex mcp get project-memory-hub
memory-hub integrate agents install --dry-run --format json
memory-hub integrate agents install --format json
```

In the generated `[mcp_servers.project-memory-hub]` table in `~/.codex/config.toml`, allow and
pre-approve only the broker's two bounded tools while keeping the global approval policy unchanged:

```toml
enabled_tools = ["capture_pending_v1", "reconcile_if_due_v1"]
default_tools_approval_mode = "prompt"

[mcp_servers.project-memory-hub.tools.capture_pending_v1]
approval_mode = "approve"

[mcp_servers.project-memory-hub.tools.reconcile_if_due_v1]
approval_mode = "approve"
```

Restart Codex after first registering the broker. The managed block asks Codex to reconcile through
the narrow MCP broker and recall before substantial Git-project work, then capture through the
broker after a verified work unit. It does not run for simple questions or non-project chat. Keep
the normal `workspace-write` sandbox; full filesystem access is not required.

Start the loopback-only control panel when you want to review local state:

```bash
memory-hub serve --host 127.0.0.1 --port 8765
```

The authenticated browser bootstrap, ChatGPT import flow, success checks, and safe removal steps are
in [Getting started](docs/getting-started.md).

## Compatibility

### Sources

| Source | Capability | Public Beta status |
| --- | --- | --- |
| Codex | Incremental local JSONL ingestion and capture verification | `Supported` |
| ChatGPT | User-selected official export ZIP import | `Supported`; manual import only |
| Trae | Bounded installation/access and optional structure inspection | `Read-only probe`; behavior ingestion locked |
| WorkBuddy | Bounded installation and directory-access inspection | `Read-only probe`; behavior ingestion locked |
| Zcode | Bounded installation and directory-access inspection | `Read-only probe`; behavior ingestion locked |
| QoderWork | Bounded installation and directory-access inspection | `Read-only probe`; behavior ingestion locked |
| Claude Code | Bounded installation and directory-access inspection | `Read-only probe`; behavior ingestion locked |

Only Codex and ChatGPT are registered ingestion sources. Optional-source probes run only when the
owner explicitly requests them; reconcile, doctor, and daily automation do not invoke them.

When a ChatGPT export omits a usable model slug, the record stays in
`source_agent=chatgpt + model_id=unknown`. All such records share that fallback namespace, so treat
its recalled behavior as source-isolated but not precisely model-isolated; review it before reuse.

### Platforms and runtimes

| Component | Status | Notes |
| --- | --- | --- |
| macOS | `Supported` | Primary local runtime and release-blocking environment |
| Linux | `Experimental` | Non-blocking compatibility target; not a supported release platform |
| Windows | `Unsupported` | No supported runtime or release artifact |
| Python 3.11 | `Supported` | Declared package runtime |
| Python 3.12 | `Supported` | Declared package runtime |
| Chromium | `Verified` | Browser behavior is covered by Playwright Chromium tests |
| Safari and Firefox | Unverified | No compatibility claim in this Beta |

The Python 3.11 and 3.12 support claims require a clean built-wheel smoke on macOS with isolated
user directories. Hosted CI is not claimed until the real repository run exists; Linux remains an
explicitly non-blocking experimental target.

## What it remembers

Shared project facts may include bounded Git state, root manifests, package scripts, test
configuration, README and AGENTS indexes, and file-tree statistics. Behavior memory contains only
structured, redacted fields such as outcomes, explicit decisions, failed attempts, verified methods,
preferences, risks, open issues, and reusable lessons.

Before a task, recall builds a relevance-ranked brief capped at 800 tokens. It favors current state,
directly relevant verified commands, and unresolved work instead of replaying the entire project
history.

## Privacy and non-goals

Project Memory Hub does not:

- copy raw Codex or ChatGPT conversation bodies into its memory database;
- infer personality, private reasoning, or hidden chain of thought;
- scan the entire home directory or intentionally read `.env`, private keys, credentials,
  dependencies, or build outputs;
- scrape a browser, reuse account cookies, or continuously synchronize a ChatGPT account;
- upload runtime data to an external memory, embedding, or model service;
- turn optional-source probes into behavior ingestion;
- modify a scanned project without an approved proposal, or decide to merge or push a proposal;
- provide a multi-user cloud knowledge base or a sandbox against a malicious process already able to
  read the same macOS account.

The local Web server accepts loopback binds only. Its bootstrap token is stored separately with
private permissions, removed from the URL after authentication, and replaced by an HttpOnly local
session with CSRF protection.

See [Security architecture](docs/security.md) for the threat model, deletion semantics, and known
limits. Report suspected vulnerabilities through the private process in [SECURITY.md](SECURITY.md),
never through a public issue containing sensitive data.

## How it works

### Local data flow

![Local data flow from bounded sources to isolated recall](docs/assets/diagrams/local-data-flow.svg)

Project discovery and fact scanning are separate from behavior ingestion. Reconcile processes safe
retries, Codex increments, user-provided ChatGPT imports, pending verification, compaction, and
health-derived proposals under a single-instance lock and idempotent checkpoints.

### Strict model isolation

![Strict project, source, and exact-model isolation](docs/assets/diagrams/strict-model-isolation.svg)

Shared facts must be observable project facts. Private behavior memory is never searched across a
different project, source agent, or exact model ID and then filtered afterward; the namespace is part
of candidate selection itself.

### Approval-gated improvement

![Approval-gated proposal, isolated branch, and human merge boundary](docs/assets/diagrams/approval-gated-improvement.svg)

Health-based analysis may produce a proposal, but proposal creation, approval, apply, rollback,
merge, and publication remain distinct steps. Project Memory Hub stops at a reviewable local commit
on an isolated `codex/` branch.

The detailed capture marker, explicit issue-resolution state machine, retry behavior, and component
boundaries are documented in [Architecture](docs/architecture.md).

## Synthetic screenshot gallery

Every public screenshot is generated from a separate synthetic runtime and displays a visible
`DEMO DATA` label. Never use a live Projects page or a personal runtime to create public assets.

- [Sources: ingestion versus locked read-only probes](docs/assets/screenshots/sources.png)
- [Memories: exact project/source/model selection](docs/assets/screenshots/memories.png)
- [Repository social preview](docs/assets/social-preview.png)

## Documentation

- [Getting started](docs/getting-started.md)
- [Architecture](docs/architecture.md)
- [Security architecture](docs/security.md)
- [Operations and recovery](docs/operations.md)
- [Release preparation](docs/releasing.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)

## License

Project Memory Hub is licensed under the [Apache License 2.0](LICENSE).
