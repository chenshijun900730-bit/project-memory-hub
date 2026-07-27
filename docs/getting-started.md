# Getting started

[README](../README.md) · [简体中文](../README.zh-CN.md) ·
[Architecture](architecture.md) · [Security](security.md) ·
[Operations](operations.md)

This guide installs Project Memory Hub from a normal local clone, creates a private runtime, previews
every write that has a dry-run boundary, and connects Codex only after review.

## Requirements

- macOS
- Python 3.11 or Python 3.12
- [uv](https://docs.astral.sh/uv/)
- A stable local clone outside temporary `.worktrees` directories
- Read access to the project roots you intentionally configure

The Public Beta does not provide a supported Windows runtime. Linux is experimental and should not
be used as evidence that a macOS installation is healthy.

## Install the local package

From the root of the local clone:

```bash
uv tool install .
command -v memory-hub
memory-hub version
```

The executable should resolve from the `uv` tool environment and `memory-hub version` should print
`0.2.1`. Editable installation is reserved for contributor development; see
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Initialize private storage

```bash
memory-hub init --format json
```

Expected result:

```json
{"status":"initialized"}
```

The default runtime is:

```text
~/Library/Application Support/Project Memory Hub
```

The runtime directory is restricted to mode `0700`; ordinary runtime files, including the SQLite
database, configuration, access token, and backups, are restricted to `0600`. A global
`--config /path/to/config.toml` selects a different runtime whose root is the configuration file's
parent directory.

## Configure the first run

Inspect the current setup without changing an existing configuration:

```bash
memory-hub setup --format json
```

To apply one reviewed root, keep the two registered sources, and mark local setup complete:

```bash
memory-hub setup --project-root "$HOME/Documents" --source codex --source chatgpt --complete --format json
```

The same fields are available from the authenticated Web `/setup` page after the control panel is
started. New configurations resume as incomplete; legacy configurations without the completion field
remain complete, so upgrading never forces the wizard. Repeating an identical completion is a
zero-write operation.

Setup only saves local roots, registered sources, retention and recall limits, completion state, and
the desired daily time. It does not run discovery, import, or reconcile, does not rotate the local
access token, and does not inspect optional source probes. Only Codex and ChatGPT can be enabled.
Behavior memory remains isolated by project + source + exact model ID.

While a new configuration remains incomplete, `memory-hub serve` reports `setup_required` and skips
its startup reconcile. Completing Setup only unlocks that normal startup behavior; discovery and
imports still remain explicit operations.

The command and Web page never edit Codex automation TOML. They report whether the desired daily
task is current, drifted, unavailable, or requires authorization; creation and repair remain explicit
actions in the authorized Codex host.

## Preview and register projects

The default discovery roots are `~/Documents`, `~/Code x`, and `~/Workbuddy`. Discovery is bounded
and reports inaccessible roots instead of claiming they were scanned.

Run the no-write preview first:

```bash
memory-hub discover --dry-run --format json
```

Review candidate paths and permission issues. Register only after the preview is acceptable:

```bash
memory-hub discover --format json
```

The non-dry-run command registers every candidate returned by that run. It is not a per-project
selector. If any candidate should remain unregistered, change the configured discovery roots and
repeat the dry-run before applying the inventory.

To scan facts for one registered project, use an explicit path and preview it first:

```bash
PROJECT_ROOT="/path/to/project"
memory-hub scan --cwd "$PROJECT_ROOT" --dry-run --format json
memory-hub scan --cwd "$PROJECT_ROOT" --format json
```

Fact scanning reads bounded Git and root-level metadata. It does not turn the whole repository into a
prompt and does not intentionally read secret files, dependency trees, or build output.

## Connect Codex

Project Memory Hub uses a narrow stdio MCP broker for its two write-capable Codex operations, while
recall remains a strict read-only CLI operation. Register the broker from the stable tool installation
before installing the managed guidance:

```bash
PMH_LAUNCHER="$(realpath "$(command -v memory-hub)")"
PMH_PYTHON="$(dirname "$PMH_LAUNCHER")/python"
test -x "$PMH_PYTHON"
codex mcp add project-memory-hub -- \
  "$PMH_PYTHON" -m project_memory_hub.integration.mcp_broker
codex mcp get project-memory-hub
```

Keep the global approval policy unchanged. In the generated `[mcp_servers.project-memory-hub]`
table in `~/.codex/config.toml`, allow and pre-approve only the two bounded PMH tools:

```toml
enabled_tools = ["capture_pending_v1", "reconcile_if_due_v1"]
default_tools_approval_mode = "prompt"

[mcp_servers.project-memory-hub.tools.capture_pending_v1]
approval_mode = "approve"

[mcp_servers.project-memory-hub.tools.reconcile_if_due_v1]
approval_mode = "approve"
```

Restart Codex after first registration so new tasks load `capture_pending_v1` and
`reconcile_if_due_v1`. Keep the normal `workspace-write` sandbox; do not grant full filesystem access
or add the private PMH runtime directory as a writable root.

Project Memory Hub manages only one marked block in the user's Codex `AGENTS.md`. Existing bytes
outside that block are preserved and the first real write creates a private backup.

Preview the exact structural change:

```bash
memory-hub integrate agents install --dry-run --format json
```

If the preview is expected, install the managed block:

```bash
memory-hub integrate agents install --format json
memory-hub doctor --format json
```

For substantial work in a Git-backed project, the managed workflow reconciles through MCP, resolves
the current Codex namespace, recalls a bounded brief through the read-only CLI, and submits a pending
work unit through MCP afterward. It never falls back to direct CLI capture or reconcile. Simple
questions and non-project conversations skip the workflow. Recall or capture failure never withholds
the user's original task result.

To inspect the exact namespace resolved for the current Codex task:

```bash
memory-hub codex-context --cwd "$PROJECT_ROOT" --format json
```

Do not guess, abbreviate, or copy a model ID from another task. The returned correlation ID is not a
trust credential.

## Run the first reconcile

```bash
memory-hub reconcile --if-due --format json
```

`--if-due` runs only when at least 24 hours have passed since the last successful reconcile or a
bounded catch-up marker exists. Repeated runs use checkpoints, receipts, and content fingerprints to
avoid duplicating successful records.

Project Memory Hub does not create a scheduler during installation or setup. A Codex desktop
automation is an optional, separately authorized host action. If a scheduled run is missed because
the Mac is asleep or Codex is closed, the next managed project task or control-panel startup can run
the due-only catch-up path.

## Import a ChatGPT official export

ChatGPT ingestion is manual. Project Memory Hub does not log in, reuse cookies, scan Downloads, or
scrape a browser.

Keep the original export unchanged and preview the import:

```bash
EXPORT_ZIP="/path/to/chatgpt-export.zip"
memory-hub import chatgpt "$EXPORT_ZIP" --dry-run --format json
```

The preview validates archive limits and reports matches and confirmation requirements without
writing imported conversations. If the preview is acceptable:

```bash
memory-hub import chatgpt "$EXPORT_ZIP" --format json
```

Ambiguous project or model matches remain pending for local review; they are never guessed into a
namespace. The source export is not deleted, moved, or modified.

## Open the local control panel

Start the loopback-only server:

```bash
memory-hub serve --host 127.0.0.1 --port 8765
```

In a second terminal, bootstrap one browser session:

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
TOKEN="$(<"$RUNTIME/access-token")"
open "http://127.0.0.1:8765/?token=$TOKEN"
unset TOKEN
```

Treat the bootstrap URL as a credential and never paste it into an issue, log, or screenshot. After
validation, the server redirects to a token-free URL and uses an HttpOnly, SameSite session. Never
bind the server to `0.0.0.0` or a LAN address; non-loopback binds are rejected.

The control panel can review projects, exact namespaces, memories, imports, promotions, proposals,
and settings. Archive preserves a record but removes it from recall. Delete changes its lifecycle and
hides it from the UI and recall; it is not an immediate byte-level erasure from SQLite pages or
backups.

## Success criteria

A first installation is ready for use when:

- `memory-hub version` reports `0.2.1`;
- initialization reports `status: "initialized"`;
- discovery preview completes without an unexplained permission failure;
- registered projects and fact scans match the paths the owner reviewed;
- `codex mcp get project-memory-hub` reports the expected enabled stdio broker;
- managed Codex guidance is `current` or intentionally absent;
- SQLite quick-check, schema, enabled adapters, and runtime permissions pass doctor;
- doctor is `pass`, or every `warn` is understood and non-blocking;
- the control panel is reachable only through loopback and bootstrap removes the token from the URL.

Do not broaden filesystem permissions simply to turn an optional warning green. Follow the exact
remediation in [Operations](operations.md).

## Pause or remove the integration

Preview and remove only the managed Codex block:

```bash
memory-hub integrate agents remove --dry-run --format json
memory-hub integrate agents remove --format json
```

This does not remove project files or runtime data. For backup, recovery, full uninstall, and stale
automation cleanup, follow [Operations](operations.md). For contributor setup, use
[CONTRIBUTING.md](../CONTRIBUTING.md).
