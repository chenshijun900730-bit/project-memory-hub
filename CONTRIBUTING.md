# Contributing to Project Memory Hub

Thank you for helping improve Project Memory Hub. Contributions should preserve its local-first
privacy model, strict source/model isolation, and honest verification record.

## License and conduct

By intentionally submitting a contribution for inclusion in this project, you agree that it may be
distributed under the repository's Apache-2.0 license. Participation is also governed by
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Development setup

Use macOS with Python 3.11 or 3.12 and an installed `uv` executable. From a normal local clone:

```bash
uv sync --locked --extra test
```

Run the focused tests for your change first, then the applicable repository checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src/project_memory_hub
uv run pytest -q
```

Do not report a command as passing unless you actually ran it against the submitted revision. If a
check was not run or could not run, say so explicitly.

## Preserve the product contracts

- Behavior recall remains isolated by `project_id + source_agent + model_id`; never retrieve across
  a namespace and filter afterward.
- Only supported ingestion sources may write behavior memory. Optional-source probes must remain
  bounded, read-only, and disabled from ingestion.
- Local security controls, provenance verification, approval gates, and deletion semantics must not
  be weakened to simplify a change.
- Changes to a public contract need focused tests and matching documentation.

## Privacy rules for every contribution

Issues, pull requests, commits, fixtures, test output, documentation, and Generated assets must not
contain private user material. Before submitting, remove or replace all of the following:

- absolute paths;
- access tokens;
- session transcripts;
- database contents;
- private screenshots;
- model credentials.

The same rule covers cookies, account identifiers, provider keys, conversation exports, runtime
backups, and other secrets. Use synthetic paths, synthetic sessions, and synthetic database rows in
tests and examples. Generated assets must be reproducible from synthetic inputs and visibly marked
as demo data where they depict application state.

For security vulnerabilities, follow [SECURITY.md](SECURITY.md). Never disclose security-sensitive
details in a public issue or pull request.

## Change workflow

1. Keep each change focused and explain the user-visible or maintenance problem it solves.
2. Add a failing test for a behavior change, then make the smallest implementation that satisfies
   the contract.
3. Update public documentation and the changelog when behavior, compatibility, or operator steps
   change.
4. Inspect the complete diff for private data, unrelated edits, and generated noise.
5. Open a pull request using the repository template.

Commit titles should be specific and describe the actual change. Avoid vague titles such as
"update code" or claims of testing, compatibility, or performance that were not verified.

## Pull-request evidence

In the pull request, list:

- **Tests actually run**, with their exact result;
- checks not run and the reason;
- changed behavior and compatibility impact;
- privacy and security review performed;
- documentation or changelog changes;
- risks, rollback approach, and any unfinished work.

Reviewers may ask for narrower fixtures, stronger isolation evidence, or a clean artifact test when
the change touches packaging, persistence, adapters, authentication, or release automation.
