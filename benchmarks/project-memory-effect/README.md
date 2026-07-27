# Project Memory Effect Benchmark

This repository benchmark measures whole-task outcomes from fresh Codex runs with and without a
bounded Project Memory Hub recall brief. It contains eight preregistered tasks across two synthetic
Git fixtures.

## Conditions

- **baseline:** Codex receives the task and the files exported from the preregistered fixture
  commit, with no project-memory brief.
- **PMH:** Codex receives the identical task and commit-tree export plus the bounded brief produced
  from a frozen, trusted synthetic Project Memory Hub fixture.

Both conditions use the same fixture commit, task prompt, validator, timeout, exact requested model,
reasoning effort, Codex CLI version, sandbox, and tool permissions. The brief itself counts toward
the PMH condition's complete task input.

The current Codex exec event contract does not emit an observed model ID. Public rows therefore bind
and report the exact requested model, while `observed_model_id` remains `null`; the report must not
claim that the event stream independently verified the provider-side model.

The preregistered manifest pins all eight recall-brief SHA-256 values and one root-independent
logical memory-snapshot SHA-256 in `recall_contract`. Preparation must reproduce those values; a
private state file cannot substitute different recall text by merely recomputing its self-reported
hash.

## Pilot and public run

Every JSONL row carries an explicit `artifact_scope`; the CLI never infers scope from a filename:

- `single-pair-pilot` is exactly T01, replicate 1, baseline plus PMH: 2 rows.
- `full-pilot` is exactly T01–T08, replicate 1, both conditions: 16 rows.
- `public-v1` is exactly T01–T08, replicates 1–3, both conditions: 48 rows.

A full one-replicate pilot validates the harness. It is not the headline product-effect result.
`verify-public` and `summarize` reject missing, duplicate, or extra protocol keys for the declared
scope, even when every individual row is structurally valid.

The preregistered public run uses three repetitions for every task and condition: 48 fresh Codex runs.
Repetitions improve stability but do not turn the benchmark into 48 independent tasks; the
statistical unit remains eight tasks.

## Outcome rules

Task success is the primary outcome; success is the primary outcome before efficiency. Every
failure, timeout, invalid run, validator failure, and
changed-path violation stays in the public aggregate.

Token and elapsed-time changes are compared only for matching task/replicate pairs where both
conditions succeeded. The report includes the number of comparable pairs and never treats an early
failure as a token saving.

Elapsed time starts immediately before the sandboxed Codex subprocess launch and stops after its
streams and process group are contained. Fixture/auth/private-launcher preparation and the
parent-owned validator run are outside that interval.

The token event is a model-processing measurement, not a billing or subscription-quota measurement.
Input, cached input, cache-write input, uncached input, output, reasoning output, and total are
reported separately. Token counts, durations, tool counts, contamination/write-guard observations,
post-change validation-workspace and transcript hashes, and raw-event hashes remain self-reported
runtime evidence: the public JSONL does not contain the private source evidence needed to
independently recompute them. Every row therefore carries
`"runtime_evidence_class":"self-reported"`, and summaries list the self-reported fields separately
from the manifest-checked contract fields. A successful row must report the manifest's exact
expected test count as both total and passed. Test counts on failed rows describe the observed
failure path and remain self-reported; summaries label those two cases separately.

## Isolation and write safety

The benchmark uses conflicting foreign-project, foreign-source, and foreign-model canaries. Those
negative isolation probes are reported separately from the token comparison and require zero
foreign hits.

All recall briefs are created from a temporary runtime before Codex execution. Child tasks cannot
capture, reconcile, promote, or mutate it. Runtime fingerprints and trusted/pending counts must
remain unchanged.

Each child Codex process receives a temporary auth-only home with no global `AGENTS.md`, config,
plugins, memories, Chronicle, or project rules. The runner removes the temporary auth copy when it
observes `thread.started`.

The frozen sandbox mode is `macos-external-seatbelt-v2`. The runner starts Codex inside one strict
macOS Seatbelt profile and passes `--dangerously-bypass-approvals-and-sandbox` solely to prevent
Codex from attempting an unsupported second Seatbelt application. This is not an unsandboxed run:
the runner copies Codex to a unique per-run `0700` launcher path, grants provider network and the
auth-only home only to that exact parent path, and removes the launcher when it observes
`thread.started`. Descendants cannot read or execute that launcher even before removal, so deletion
is defense in depth rather than a timing assumption.

On macOS, `getaddrinfo` reaches the local `/var/run/mDNSResponder` socket. The profile grants only
the exact private Codex parent the ancestor metadata needed to reach that socket; it does not add a
new DNS Mach service, file-content access, or a global `/var` read. The existing parent-only
network rule still owns the socket connection. An executable canary first confirms the host can
resolve `chatgpt.com`, then confirms the private parent can resolve it while an allowed zsh
descendant actually attempts the same name. A separate numeric-loopback probe confirms that the
descendant still cannot connect even without DNS, and protected files remain unreadable.

Model-generated child processes cannot establish new network connections or read the auth-only
home. They may change file data only in the preregistered existing implementation file; creating a
sibling, unlinking or changing its mode, and writing `.git`/`.codex` remain denied. Inherited
CPU, file-size, open-file, and dynamically headroom-bounded process limits apply before Codex
starts. Only the exact private Codex parent may fork. Model descendants may execute a fixed
inspection/editing tool set, but not Python, other interpreters, compilers, arbitrary workspace
programs, or another `sandbox-exec`; the task prompt leaves tests to the trusted parent runner.
The runner also cleans the complete process group on every exit path and uses streaming, bounded
before/after snapshots rather than mutable Git state.

Codex may probe the host-managed `/etc/codex/requirements.toml`. The outer profile grants read
access only to that exact logical file and its canonical
`/private/etc/codex/requirements.toml`, plus the minimum ancestor metadata needed to resolve
`/etc`; it does not grant either `/etc` tree. This logical permission is identical on hosts where
the file is absent. If a safe managed file is present, its canonical path and content hash are
sealed only in the private execution envelope; absence is recorded explicitly. Public artifacts
prove the stable logical permission, not the private contents or policy enforced by that host file.

Before every model launch, the runner verifies the fixture working tree byte-for-byte against the
recorded commit, including ignored and untracked files. It then materializes the model workspace
from verified commit blobs. The export contains no `.git` directory, local Git configuration,
hooks, ignored caches, or untracked files, so neither condition can depend on mutable repository
metadata.

After the model phase, the runner moves the model workspace away from its granted path, makes a
stable copy at a path the model sandbox never received, and removes the original after validation.
The external validator receives that copy read-only under a separate minimal Seatbelt profile. It
cannot fork, execute another program, read the user home, or use the network; only its separate
temporary directory is writable. The trusted parent launches one fresh worker per frozen
task-specific case and compares each ordinary observation with a parent-owned oracle. A worker
never receives a final success-report file descriptor or protocol, so model code cannot turn a
forged unittest result, stdout claim, or zero exit alone into benchmark success.

Before a worker runs, the parent applies a conservative AST policy to the one allowed implementation
file. The worker resolves the target function before generating a fresh 128-bit challenge, and the
parent accepts only one canonical, duplicate-key-free response with a new challenge and the expected
observation. The policy is intentionally limited to these eight synthetic tasks: it can reject an
unusual but otherwise valid Python solution, and it is not a cryptographic proof for arbitrary
hostile Python or interpreter vulnerabilities.

macOS integration canaries cover private launcher re-entry, deletion-after-start, protected-path
metadata/content access, child network and auth access, allowed-file data writes, denied
sibling/unlink/mode changes, Git executable access with the expected non-repository status,
`.git`/`.codex` creation or writes, the strict validator,
denied child fork and detach-capable interpreter execution, and the real T05
temporary-directory/symlink oracle. This is a fixed synthetic benchmark boundary, not a general
sandbox for arbitrary third-party repositories or a claim about already-open file descriptors
deliberately passed by a compromised parent process.

## Privacy

The raw Codex event streams remain private. Public results contain only synthetic task identifiers,
status values, numeric measurements, bounded model/tool metadata, and one-way hashes. Result schema
v3 binds every row to its explicit artifact scope, the fixed preregistered manifest digest, Git
harness commit, content-derived harness identity, and the normalized digest of the actual model
Seatbelt profile. It also records one-way hashes for the stable validation-workspace tree and
validator evidence transcript.

The fixed manifest/public harness contract reconstructs and pins the fixture commit, initial
workspace hash, condition-specific prompt hash, validator hash, normalized model-sandbox profile
hash, and expected test count for every task. Official verification compares those reconstructable
fields instead of trusting the row's own values, and requires the exact expected count for
successful rows. Failed-row test counts are explicitly classified as self-reported. One artifact
must also contain exactly one
`harness_commit`/`harness_sha256` identity. Host-specific sandbox-executable, protected-home, and
temporary paths are normalized to logical markers for the cross-machine profile digest; the private
execution envelope separately binds the actual executable/auth paths and file identities used by
that run.

The public tree must not contain local absolute paths, usernames, private project names, prompts
containing actual recall bodies, assistant responses, tool output, session identifiers, source
record identifiers, PMH databases, access credentials, or account/quota details.

## Claim boundary

The report may describe results on this frozen dataset under the recorded environment. It must not
generalize a fixed token reduction beyond the dataset, translate model-processing counts into a
user's bill, compare untested memory systems, or imply use of a second model that was not executed.

## Reproduction lifecycle

The command is safe by default: `run` only prints the schedule unless `--execute` is present. The
private root contains prompts, recall text, raw events, temporary workspaces, and resumable
checkpoints; keep it outside the repository.

```bash
PRIVATE_ROOT="${TMPDIR:-/tmp}/pmh-project-memory-effect-public-v1"

python3 -B scripts/run_public_benchmark.py prepare \
  --private-root "$PRIVATE_ROOT" \
  --model gpt-5.6-sol

python3 -B scripts/run_public_benchmark.py run \
  --scope single-pair-pilot \
  --private-root "$PRIVATE_ROOT" \
  --replicates 1 \
  --task T01
```

After reviewing that two-run schedule, add `--execute` and an output path to run the single-pair
harness pilot:

```bash
python3 -B scripts/run_public_benchmark.py run \
  --scope single-pair-pilot \
  --private-root "$PRIVATE_ROOT" \
  --replicates 1 \
  --task T01 \
  --execute \
  --output benchmarks/project-memory-effect/results/pilot-t01.jsonl

python3 -B scripts/run_public_benchmark.py verify-public \
  --scope single-pair-pilot \
  --input benchmarks/project-memory-effect/results/pilot-t01.jsonl

python3 -B scripts/run_public_benchmark.py summarize \
  --scope single-pair-pilot \
  --input benchmarks/project-memory-effect/results/pilot-t01.jsonl \
  --output benchmarks/project-memory-effect/results/pilot-t01-summary.json
```

Run a publishable benchmark from a fresh, reviewed clone or clean detached worktree at the intended
commit. Use Python's `-B` option as shown above; real `--execute` rejects an interpreter that may
write bytecode caches into the checked harness tree. `prepare` and a dry run may still be used while
developing the harness because their state records the current content-derived identity. A real
`--execute` additionally requires the complete regular-file sets under `scripts/` and `src/`, plus
the manifest, public result schema, `pyproject.toml`, and `uv.lock`, to equal the blobs at the
recorded Git `HEAD`. Extra relevant files, bytecode caches, symlinks, and dirty contents fail
closed. Commit the harness changes and create a fresh private root before a publishable run.

This in-process check detects accidental checkout drift; it is not attestation of launcher code
that was already malicious before Python imported it. The fresh reviewed checkout is therefore an
explicit operational trust boundary, not a property the running process claims to prove.

Both `verify-public` and `summarize` require an explicit scope and its complete protocol-key set.
They validate the official task, version, model, CLI, reasoning, timeout, replicate, and order protocol
in addition to the structural public allowlist. They also
require the preregistered manifest, memory snapshot, and task-specific PMH brief hashes, so a
protocol-shaped lookalike row cannot be turned into an official effect summary. They also resolve
each row's `harness_commit` in the local repository and recompute its harness identity from that
commit tree; the claimed `harness_sha256` must match.

The full one-replicate pilot uses `--scope full-pilot`, omits `--task T01`, and produces sixteen
runs. The preregistered public batch uses `--scope public-v1 --replicates 3` and produces 48 runs.
Use a fresh private root when changing scopes because scope is part of every private checkpoint and
the sealed execution envelope. A resumed command reuses only complete,
allowlist-validated private checkpoints with matching task, model request, CLI version, fixture,
memory snapshot, timeout, condition, fixed manifest, harness commit/content identity, normalized
model sandbox profile, stable validation tree, raw events, and validator transcript. The result is
fsynced before its 65-byte SHA-256 receipt, and the containing directory is then fsynced. A missing
or partial receipt is treated as an interrupted publication: the entire run directory is moved to
a private `0700` quarantine and the same run ID starts again. A complete but mismatched receipt or
evidence hash fails closed instead of being silently reused.

Preparation similarly writes `benchmark-state.json` before `benchmark-state.sha256`. Every run
validates that receipt plus the manifest-bound recall contract, execution settings, repositories,
and harness identity before it reads or reuses private state.

The first real `--execute` also seals `execution-envelope.json` and its SHA-256 receipt. That private
envelope binds the actual Codex, fixed validated `/usr/bin/git`, validator, and auth-file identities
plus the managed-requirements present/absent state and, when present, its safe canonical path and
content hash. It also binds the normalized model Seatbelt profile for every preregistered task.
Later execution under the same private root rejects boundary drift before starting a new Codex run.
