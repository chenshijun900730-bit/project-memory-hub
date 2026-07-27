# Project Memory Hub Public Effect Benchmark Design

**Status:** Approved for implementation

**Date:** 2026-07-26

**Approved direction:** The user selected the public, reproducible A/B benchmark option.

## 1. Purpose

The benchmark measures whether a bounded Project Memory Hub recall brief helps a fresh Codex task
complete fixed engineering work with fewer model tokens, less elapsed time, or fewer wrong turns,
without reducing task success.

It is deliberately separate from the existing component tests:

- component tests prove that candidate memory is filtered to a brief of at most 800 estimated
  tokens and that project, source, and exact-model namespaces are isolated;
- the public benchmark measures whole-task outcomes from fresh Codex runs;
- neither result may be presented as a billing, subscription-quota, or universal productivity
  guarantee.

## 2. Claim boundary

The first public dataset is a paired benchmark over eight preregistered synthetic engineering tasks.
It may support a claim only in this form:

> On this fixed benchmark, under the recorded Codex version, exact model, reasoning effort,
> permissions, prompts, fixtures, and date, the PMH condition produced the reported paired task
> outcomes.

It must not support claims that PMH saves a fixed percentage on every project, outperforms every
Markdown or memory system, lowers a user's bill, or proves real use of a second model that was not
actually executed.

The runner supports repeated trials. A one-replicate run is a pilot that validates the harness and
may be published only as a labelled pilot. The preregistered public result uses three repetitions per
condition: eight tasks by two conditions by three repetitions, for 48 fresh Codex runs. Repetitions
do not become independent task samples; the statistical unit remains the eight paired tasks.
Artifacts declare one of three scopes in both the CLI and every JSONL row:
`single-pair-pilot` is exactly T01/r1/two conditions, `full-pilot` is exactly T01–T08/r1/two
conditions, and `public-v1` is exactly T01–T08/r1–r3/two conditions. Verification rejects any
missing, duplicate, or extra protocol key and any mixed harness commit/content identity.

## 3. Conditions

Both conditions receive:

- the same commit-tree export from the same generated Git fixture and deterministic baseline commit;
- the same base task prompt, task version, visible exported repository evidence, validation command, timeout,
  exact requested model, reasoning effort, Codex CLI version, sandbox, and tool permissions;
- an independent disposable workspace created from the same fixture snapshot;
- a fresh non-persisted Codex execution with user configuration and project-specific integrations
  disabled.

The runner uses a temporary auth-only Codex home. It does not copy global `AGENTS.md`, config,
plugins, memories, Chronicle, or project rules, and it clears inherited task/PMH environment
variables. The temporary auth copy is deleted when the runner observes `thread.started`.

The frozen execution sandbox is `macos-external-seatbelt-v2`. Codex is launched inside a strict
outer Seatbelt profile. The runner supplies `--dangerously-bypass-approvals-and-sandbox` only because
current macOS rejects Codex's attempt to apply a second Seatbelt profile from inside the first one.
For every run, Codex is copied to a unique `0700` private launcher. Only `/usr/bin/sandbox-exec` may
read and execute that path; descendants cannot read it or start another `sandbox-exec`. The exact
private Codex parent alone may read/write its auth-only home and process temp or use the network for
provider access. The runner removes the launcher after observing `thread.started`; executable-path
denial is the primary re-entry boundary, so unlink timing is defense in depth.

Current macOS resolves public hostnames through `/var/run/mDNSResponder`. The same exact
private-parent process filter therefore grants only `file-read-metadata` and
`file-test-existence` for that socket's path ancestors; the already parent-filtered network rule
authorizes the connection itself. No additional DNS Mach service, file-content permission, or
global `/var` read is granted. The DNS canary first requires successful host resolution, then runs
compiled `getaddrinfo("chatgpt.com")` as the private parent. It also starts an allowlisted zsh
descendant that reaches its own `getaddrinfo` path through `ztcp chatgpt.com 443`. A separate
numeric-loopback `ztcp` probe, against a listener already proven reachable outside Seatbelt,
verifies that the descendant cannot establish a connection even without DNS, while direct and
child reads of a protected file remain denied.

The parent may also probe the host-managed `/etc/codex/requirements.toml`. Seatbelt allows only
that literal path, the canonical `/private/etc/codex/requirements.toml`, and ancestor metadata
needed to resolve the `/etc` symlink; neither `/etc` subtree is readable. The rule is present even
when the file is absent so the normalized logical profile remains cross-host stable. On execution,
an absent file is recorded as absent in the private envelope. A present file must resolve to the
expected canonical path and pass bounded ownership, mode, type, and stable-content checks before
its real path and SHA-256 are privately sealed; otherwise execution fails closed.

Model-generated descendants may read the synthetic workspace and allowlisted system runtimes, but
may write data only to the preregistered existing implementation file. They cannot create sibling
files, unlink or change the mode of that file, establish new network connections, read the
auth-only home, or write `.git`/`.codex`. Only the exact private Codex parent may fork. Descendants
may execute only a fixed repository-inspection/editing tool set; Python, other interpreters,
compilers, arbitrary workspace executables, and a second `sandbox-exec` are denied. Inherited CPU,
file-size, open-file, and dynamically headroom-bounded process limits are installed before Codex
starts, and the runner cleans the full process group on every exit path. The model phase does not
run tests; validation is deliberately owned by the trusted parent runner.

The fixture source repository is verified byte-for-byte against the recorded commit before each
launch, including ignored and untracked files. The model workspace is then materialized from the
verified commit blobs and contains no `.git` directory, local Git configuration, hooks, ignored
caches, or untracked files. The model can inspect the exported code and documentation but cannot
depend on mutable local Git metadata or history.

After the model process ends, the runner moves its workspace away from the granted path, takes
streaming bounded snapshots, copies a stable result to a validation-only path never granted to the
model sandbox, and removes the original after validation. The validator sees this copy read-only;
it has a separate writable temporary directory, cannot fork or execute another program, cannot
read the user home, and cannot use the network. The trusted parent starts one fresh worker for each
frozen task-specific case, treats its output as an untrusted observation, and performs the final
comparison itself. Workers receive no trusted success-report descriptor or protocol, so monkeypatching
unittest, writing a forged frame, printing a success claim, or exiting zero cannot independently
create a passing result.

Executable macOS canaries cover initial private launch, same-path re-entry, launcher removal while
the parent remains active, parent DNS with descendant DNS/network denial, auth/network separation,
protected-path metadata/content, allowed-file data writes, denied sibling/unlink/mode changes, Git
executable access with the expected non-repository status, `.git`/`.codex` creation or write denial,
denied child fork and detach-capable interpreter execution, and a real T05 oracle run.
These checks define this fixed synthetic benchmark; they are not a general arbitrary-code sandbox
or a claim about file descriptors deliberately passed by a compromised parent.

The only treatment difference is:

- **baseline:** no project-memory brief is appended; Codex may inspect all code, tests, and
  documentation exported from the fixture commit;
- **pmh:** the base prompt is followed by the real bounded brief returned by Project Memory Hub from
  a frozen synthetic verified-memory fixture.

The PMH condition includes the recall brief's input-token cost in the whole-task token result. The
baseline is not denied normal repository evidence.

This design compares PMH recall with the same workflow without PMH. It does not compare PMH with a
third-party or broad Markdown memory pool; that would require a preregistered third condition.

## 4. Eight tasks

Two generated fixture repositories provide four tasks each so the result is not limited to the PMH
codebase itself. Every task has a bounded implementation surface, visible project history, an
automated validator, and repository evidence from which the baseline can recover the same decision
more slowly.

| ID | Fixture | Engineering memory being exercised |
|---|---|---|
| T01 | `ledger-tools` | Locate the correct verification command and preserve trailing spaces |
| T02 | `ledger-tools` | Continue a partially completed configuration-merge change |
| T03 | `ledger-tools` | Avoid a previously failed broad retry strategy |
| T04 | `ledger-tools` | Follow the approved UTC timestamp contract |
| T05 | `release-kit` | Preserve a symlink/path safety invariant |
| T06 | `release-kit` | Handle macOS/Linux path normalization consistently |
| T07 | `release-kit` | Follow the verified release-check order |
| T08 | `release-kit` | Distinguish a resolved issue from the remaining open issue |

Task prompts never contain the expected patch or hidden validation answer. PMH memory contains
structured current state, decision, failed attempt, verified command, changed path, risk, or open
issue—not a full patch or raw prior conversation.

## 5. Frozen memory and write guard

The benchmark creates a private temporary PMH runtime containing:

- the two target projects;
- the target `source_agent=codex` and exact requested model namespace;
- conflicting foreign-project, foreign-source, and foreign-model canaries;
- synthetic verified behavior records used only for the benchmark.

All treatment briefs are generated before Codex runs. The runtime is then closed and never exposed
to the child Codex process. No reconcile, capture, promotion, proposal, or Web service is available
during a run.

The runner records a bounded runtime fingerprint and trusted/pending row counts before and after
brief generation. The public result is invalid if:

- the fingerprint changes after the frozen point;
- a pending row exists;
- trusted record counts change;
- target recall contains any foreign project, source, or exact-model canary.

The private runtime, database, local paths, canary text, and raw recall body are never published.

## 6. Execution and contamination controls

- Runs use the published `task-id-parity-balanced-v1` AB/BA policy with equal first-position counts;
  no unused random seed is claimed.
- Dependencies are prepared outside the timed interval.
- The timer starts immediately before the sandboxed Codex subprocess launch and ends after stream
  capture and process containment; private-launcher copying and parent-owned validation are excluded.
- Each run receives a fresh commit-tree export without Git metadata or ignored/untracked caches,
  plus an isolated raw evidence directory.
- Runs are sequential to avoid local CPU and disk contention.
- The execution window is kept short and records the date, but the report still acknowledges that a
  provider can change backend behavior under the same model ID.
- Raw Codex JSON events remain private. The parser receives one explicit file and never scans the
  user's Codex sessions directory.
- Trial outputs are not captured into PMH.
- Before the first valid pilot, the execution manifest was amended from Codex's nested
  `workspace-write` mode to `macos-external-seatbelt-v1` after the nested `sandbox_apply` failed
  closed on macOS. No result from the failed harness attempts is eligible for publication.
- Before the first valid pilot, the model-side validation command was replaced with parent-owned
  post-run validation so the child executable set could exclude interpreters and compilers and
  close the detached-process path. This tightened profile is recorded as
  `macos-external-seatbelt-v2`; both A/B conditions use the amended prompt and oracle.
- The external validator treats model-modified code as untrusted. On macOS it runs under a separate
  minimal Seatbelt profile with the validation workspace read-only, a private temporary directory
  as its only normal write surface, no fork/exec/network access, parent-owned per-case oracle
  comparisons, bounded output/resources, and cleanup for the full process group.
- Before execution, a conservative AST policy admits only the expected pure implementation surface
  for T01–T08. Each worker resolves the target before generating a fresh 128-bit challenge; the
  parent accepts only canonical, duplicate-key-free, non-reused challenge/observation responses.
  This is a fail-closed policy for the fixed synthetic benchmark, not a cryptographic proof for
  arbitrary hostile Python, and it may reject an unusual but otherwise valid implementation.
- Preparation reads the official manifest through one bounded, no-follow, metadata-stable snapshot
  and checks it against the fixed preregistered SHA-256 constant; it never authenticates a mutable
  manifest by comparing that path with itself. The private state and every public row bind the
  manifest digest, Git harness commit, content-derived harness identity, and normalized digest of
  the actual model Seatbelt profile. Development preparation may record a dirty content identity,
  but `--execute` additionally requires Python bytecode suppression and the complete regular-file
  sets under `scripts/` and `src/`, plus the manifest, result schema, `pyproject.toml`, and
  `uv.lock`, to equal their blobs at the recorded `HEAD`; extra relevant files, caches, symlinks,
  or dirty contents fail closed. Publishable runs start with `python3 -B` from a fresh reviewed
  clone or clean detached worktree, and a dirty harness must be committed and prepared again.
  This self-check detects accidental checkout drift; it does not claim to attest launcher code
  that was already malicious before Python imported it.
- Official verification and summarization resolve each public row's `harness_commit` locally and
  recompute the content-derived harness identity from that commit tree. The execution envelope
  separately binds the fixed validated `/usr/bin/git` executable by path and content hash.
- Checkpoint reuse additionally re-hashes the stable validation-workspace tree, validator
  transcript, and raw event stream. Result and receipt files are fsynced in publication order.
  Missing or partial receipts cause a private quarantine-and-rerun; complete receipt/evidence
  mismatches fail closed.

## 7. Measurements

Success is the primary outcome. A run is successful only when:

- Codex exits normally within the timeout;
- the task-specific validator passes;
- tests or fixture contracts were not modified;
- changed paths stay inside the task allowlist.

Every run retains:

- success, timeout, invalid reason, validator counts, and elapsed milliseconds;
- requested and observed model identity when the event stream provides it;
- tool-call count and a deterministic wrong-route count derived from explicit task canaries;
- recall status, estimated tokens, selected count, omitted count, and brief hash for PMH;
- final cumulative input, cached input, cache-write input, uncached input, output, reasoning-output,
  and total tokens from the fresh Codex run;
- workspace, prompt, task, validator, and private raw-evidence hashes;
- zero/positive contamination counts and the PMH write-guard result.

Missing token fields remain `null`; they are never converted to zero. Cumulative token snapshots are
not summed. `reasoning_output_tokens` is reported as an output subtype and is not added to
`total_tokens` again.

The pinned Codex CLI currently emits the requested model only through the executed command, not as
an observed field in `thread.started`. Until that event contract changes, successful public rows
record the exact requested model and `observed_model_id=null`; reports must not call that an
independent provider-side model observation.

## 8. Analysis

The report presents:

- success rate by condition before any efficiency comparison;
- all failed, timed-out, or invalid runs;
- paired token and elapsed-time differences only where both conditions succeeded for the same task
  and repetition;
- per-task paired values plus median paired percentage change;
- the number of comparable pairs instead of silently shrinking the denominator;
- bootstrap intervals when the available successful pairs support them;
- isolation probes separately from token savings.

For each comparable pair:

```text
token_change = (pmh_total_tokens - baseline_total_tokens) / baseline_total_tokens
time_change  = (pmh_duration_ms - baseline_duration_ms) / baseline_duration_ms
```

Negative values mean the PMH condition used fewer tokens or less time.

## 9. Public and private artifacts

Tracked public artifacts:

- benchmark protocol and fixture/task manifest;
- runner and deterministic analysis code;
- sanitized per-run JSONL containing allowlisted numeric/status/hash fields only;
- aggregate JSON and English/Chinese human-readable report;
- README links and exact reproduction commands.

The manifest-bound public contract pins the reconstructable fixture commit, initial workspace,
condition-specific prompt, validator, normalized model-sandbox profile, and expected test count.
Host paths are normalized only for that logical profile digest; the private execution envelope
still binds the actual executable/auth paths and file hashes. It also binds the private
managed-requirements presence state and, when present, the canonical path and content hash. The
public artifact does not attest to that file's contents or to the host policy it may impose.
Runtime token/time/tool measurements,
contamination and write-guard observations, changed-workspace/validator transcript hashes, and raw
event hashes are labelled self-reported because their private source evidence is not published.
Successful rows must match the manifest's expected test count; failed-row test counts remain
self-reported observations of the failure path and are classified separately in summaries.

Private ignored artifacts:

- raw Codex event JSONL and stderr;
- prompts with the actual recall brief;
- generated workspaces and PMH runtime/database;
- task final responses and tool stdout/stderr;
- session/thread/source-record identifiers and local absolute paths.

The existing public-tree privacy audit must pass without adding an allowlist exemption.

## 10. Stop conditions

The full run stops safely and preserves completed evidence when:

- authentication or model access fails;
- a paid API key or new external credential would be required;
- quota exhaustion repeats;
- the PMH write guard or isolation canary fails;
- two consecutive harness failures have the same unresolved cause;
- the runner would need to weaken sandbox, privacy, or project/source/model isolation.

Pilot failure is a harness issue, not a product-effect result. No headline benchmark is generated
until the preregistered run is complete and the public privacy/quality gates pass.
