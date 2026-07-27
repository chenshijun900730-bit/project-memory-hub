# Security Policy

Project Memory Hub is a local-first application that handles development metadata, local session
records, and a private SQLite runtime. Security reports must therefore be coordinated without
placing private evidence in a public repository.

## Supported versions

| Version | Security status |
| --- | --- |
| 0.2.1 | Unreleased Public Beta candidate; fixes are prepared on the active development line |
| 0.2.0 | Previous local Beta baseline; upgrade guidance may accompany a verified fix |
| 0.1.x and earlier | No guaranteed fixes |

This project is in Public Beta. Long-term support periods, response times, and fix timelines are
not guaranteed.

## Reporting a vulnerability

If the repository shows a **Report a vulnerability** button on its Security page, use that button
to open a GitHub private vulnerability report. Include the smallest sanitized reproduction that is
needed to understand the issue.

If private vulnerability reporting is not visible, a private project reporting channel is not
currently published. Do not open a public issue, discussion, or pull request with vulnerability
details. Wait until the repository enables private vulnerability reporting or this policy names a
verified private channel. The availability of either option is not guaranteed.

Never submit any of the following as evidence:

- absolute paths or other local filesystem identifiers;
- access tokens, cookies, bootstrap secrets, or CSRF values;
- session transcripts or exported conversation bodies;
- database contents, backups, or raw runtime records;
- private screenshots or screen recordings;
- model credentials, provider keys, or account secrets.

Replace private values with synthetic examples and describe impact without exposing user data. Do
not test a suspected vulnerability against data or systems that you do not own or have permission
to assess.

## What to include privately

- the affected version or commit, without a private checkout location;
- the security boundary that may be crossed;
- minimal, synthetic steps to reproduce;
- expected and observed behavior;
- any known mitigation that does not destroy user data.

Please separate security vulnerabilities from ordinary defects. A non-security bug may use the
public bug form only after all private material has been removed.

## Disclosure and fixes

Maintainers will validate reports against the documented local-first threat model and coordinate a
fix when one is feasible. Do not publish exploit details before a fix and disclosure plan have been
agreed through the private report. This policy does not promise a bounty, response deadline, release
date, or support for an unmaintained version.
