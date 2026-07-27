## Summary

Describe the outcome of this pull request in plain language.

## Motivation

Explain the concrete problem and why it belongs in Project Memory Hub.

## Changes

- List the focused implementation, test, and documentation changes.
- Identify any behavior that intentionally remains unchanged.

## Privacy and security

- [ ] I reviewed the complete diff and removed absolute paths, access tokens, session transcripts,
      database contents, private screenshots, and model credentials.
- [ ] Fixtures, examples, and generated assets use synthetic data.
- [ ] The change preserves local-first behavior and `project_id + source_agent + model_id`
      isolation, or the impact is explained below.
- [ ] Security vulnerabilities are being handled through the private process in `SECURITY.md`, not
      disclosed in this pull request.

Explain any privacy, authentication, storage, deletion, or trust-boundary impact:

## Verification actually run

List only commands and manual checks actually run against this revision, with their exact outcomes.
Do not pre-mark checks as successful.

- [ ] Focused tests:
- [ ] Full test suite:
- [ ] Ruff format and lint:
- [ ] Mypy:
- [ ] Build, artifact, browser, or manual smoke checks:

## Verification not run

List every relevant check not run and explain why. Write `None` only when every applicable check was
actually run.

## Compatibility

State the verified impact on macOS, Linux experimental behavior, unsupported Windows behavior,
Python versions, browsers, stored data, and public commands. Do not infer compatibility from an
untested environment.

## Documentation and changelog

- [ ] User-facing documentation was updated where behavior or operator steps changed.
- [ ] `CHANGELOG.md` was updated for a notable user-visible change.
- [ ] No documentation or changelog change is needed; the reason is recorded below.

Reason or links to changed repository files:

## Risks and rollback

Describe failure modes, data or security risk, rollback steps, and any irreversible effects.

## Incomplete items

List known follow-up work, deferred checks, or unresolved questions. Do not hide unfinished work
behind a success claim.
