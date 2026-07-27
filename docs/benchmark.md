# Public Project-Memory Benchmark

Project Memory Hub was evaluated on a fixed synthetic benchmark that compares fresh Codex tasks
with and without a frozen, project-scoped PMH recall brief. The public-v1 batch contains eight tasks,
three repetitions, and two conditions: 48 runs in total.

This benchmark measures a narrow question: whether the supplied project memory helped on these
synthetic maintenance tasks under this fixed harness. It does not establish that every project will
save tokens or finish faster.

## Headline result

| Metric | Baseline | PMH | Formal per-task result |
| --- | ---: | ---: | ---: |
| Successful runs | 24 / 24 | 24 / 24 | Both conditions: 100% |
| Total model-processing tokens | Paired, three repetitions | Paired, three repetitions | Median change: **-7.83%** |
| First-pass elapsed time | Paired, three repetitions | Paired, three repetitions | Median change: **-11.18%** |
| Cross-project/source/model canary hits | 0 | 0 | No observed namespace contamination |

The 95% bootstrap interval over task medians is **-29.05% to +23.51%** for total model-processing tokens
and **-25.96% to +14.23%** for elapsed time. Both intervals include zero. The measured direction is
encouraging, but this batch does not support a general claim that PMH always reduces tokens or time.

The older pair-level diagnostic medians are -10.32% for tokens and -9.85% for time across 24
replicate pairs. They are retained for transparency, but they are not the formal headline because
the preregistered statistical unit is the task, not each repeated run.

## Per-task results

Each row first takes the median of the three paired replicate changes for that task. Negative values
mean the PMH condition used fewer model-processing tokens or less elapsed time.

| Task | Comparable pairs | Token change | Time change |
| --- | ---: | ---: | ---: |
| T01 | 3 | -27.66% | -13.99% |
| T02 | 3 | +68.96% | +21.36% |
| T03 | 3 | -7.57% | -31.36% |
| T04 | 3 | +23.51% | +14.23% |
| T05 | 3 | -5.42% | -8.36% |
| T06 | 3 | -8.09% | -2.23% |
| T07 | 3 | -32.51% | -25.96% |
| T08 | 3 | -29.05% | -16.12% |

Six of eight tasks used fewer tokens and finished faster in the PMH condition; two used more. This
variation is why the report includes per-task intervals and does not present one percentage as a
universal product effect.

## Frozen protocol

- Benchmark: `project-memory-effect-public-v1`
- Public harness commit:
  [`f70ea19530d3032840a7bf3ccdb6065db13b3d81`](https://github.com/chenshijun900730-bit/project-memory-hub/commit/f70ea19530d3032840a7bf3ccdb6065db13b3d81)
- Harness content SHA-256:
  `bfa308853d24abb4a69f4eac99fd89a109e2c4e29b1c9f2e8a2517828380d9ae`
- Manifest SHA-256:
  `a8d2c7696ad3a3035291dfeba0cf2c4cc300cf3a90bb4f04e5f046cd011c8643`
- Requested model: `gpt-5.6-sol`
- Codex CLI: `codex-cli 0.146.0-alpha.3.1`
- Reasoning effort: `low`
- Timeout: 600 seconds per run
- Order: preregistered parity-balanced AB/BA by task ID
- Fixtures: two synthetic Git repositories, eight fixed tasks
- PMH recall: 21–23 selected records and 734–793 estimated tokens per PMH run

The pinned CLI did not emit a provider-observed model identifier, so every row records
`observed_model_id=null`. The report confirms the exact requested model; it does not claim an
independent provider-side model observation.

## Analysis method

Efficiency is compared only when baseline and PMH both succeeded for the same task and repetition.
For each comparable pair:

```text
token_change = (pmh_total_tokens - baseline_total_tokens) / baseline_total_tokens
time_change  = (pmh_duration_ms - baseline_duration_ms) / baseline_duration_ms
```

The formal estimator first takes the median of the three paired changes for each task, then takes
the median across the eight task values. The reported interval is a deterministic 10,000-resample
percentile bootstrap over those eight per-task medians. The result summary also exposes every
per-task value, the 24-pair diagnostic median, and the exact comparable denominators.

## Evidence and safety boundaries

- Fixed protocol identity, prompt hashes, fixture commits, workspace hashes, validator hashes, and
  successful test counts are contract-verified.
- Token, duration, tool-call, contamination, and write-guard fields come from bounded runtime
  evidence and are classified as self-reported.
- All 48 rows passed their fixed parent-owned validators.
- All project, source, and model contamination canaries remained zero.
- The frozen PMH snapshot stayed unchanged; synthetic trusted rows remained 208 and pending rows
  remained zero before and after every run.
- The benchmark used the approved strict outer macOS Seatbelt. The model child could edit only its
  preregistered synthetic implementation file and could not use network access.
- Runtime token counts are model-processing measurements, not billing usage, subscription usage, or
  a price estimate.
- Raw prompts, recall bodies, assistant responses, tool output, session identifiers, local paths,
  and the private SQLite database are not published.

## Public artifacts

- [48 allowlisted result rows](../benchmarks/project-memory-effect/results/public-v1.jsonl)
- [Schema-v2 per-task summary](../benchmarks/project-memory-effect/results/public-v1-summary.json)
- [Benchmark manifest](../benchmarks/project-memory-effect/manifest.json)
- [Public result schema](../benchmarks/project-memory-effect/result.schema.json)
- [Reproduction and sandbox details](../benchmarks/project-memory-effect/README.md)

The result JSONL SHA-256 is
`773fc8ca9056e8d8afb54fe8ccbfff68254dd4d2d390ddb3c140135e06314c35`. The schema-v2 summary
SHA-256 is `d6a4dfd5021f865e173f312146f6b83ea8f6e41e7dc7643ab6e239d41a83d15c`.

## Reproduce or verify

The execution commit is public and immutable. Start from a clean clone of that commit, use a new
private root outside the repository, and review the 48-run schedule before adding `--execute`:

```bash
git checkout f70ea19530d3032840a7bf3ccdb6065db13b3d81
PRIVATE_ROOT="${TMPDIR:-/tmp}/pmh-project-memory-effect-public-v1"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B \
  scripts/run_public_benchmark.py prepare \
  --private-root "$PRIVATE_ROOT" \
  --model gpt-5.6-sol

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B \
  scripts/run_public_benchmark.py run \
  --scope public-v1 \
  --private-root "$PRIVATE_ROOT" \
  --replicates 3
```

The `run` command is dry-run by default. Real execution requires the explicit `--execute` switch and
an output path. On macOS it also requires the fixed benchmark's strict outer Seatbelt boundary; the
Codex sandbox-bypass parameter is not approved for unrelated tasks.

Verify the published rows and regenerate the per-task summary from a repository revision that
contains summary schema v2:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B \
  scripts/run_public_benchmark.py verify-public \
  --scope public-v1 \
  --input benchmarks/project-memory-effect/results/public-v1.jsonl

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B \
  scripts/run_public_benchmark.py summarize \
  --scope public-v1 \
  --input benchmarks/project-memory-effect/results/public-v1.jsonl \
  --output public-v1-summary.json
```

## 中文结论

本次公开实验固定 8 个合成任务、每个条件重复 3 次，共 48 次运行。Baseline 与 PMH 均为
24/24 成功。以任务作为正式统计单位时，PMH 条件的总模型处理 token 中位变化为 **-7.83%**，
耗时中位变化为 **-11.18%**。

但 token 以任务为单位的 95% bootstrap 区间为 **-29.05% 到 +23.51%**，耗时区间为
**-25.96% 到 +14.23%**，两者都包含 0。因此，这组结果说明 PMH 在多数任务上呈现节省方向，
但不足以声称所有项目都会稳定节省 token 或时间。这里的 token 是模型处理量，不是账单、
订阅额度或价格。
