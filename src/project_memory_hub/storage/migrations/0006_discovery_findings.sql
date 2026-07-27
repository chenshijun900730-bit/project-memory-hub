CREATE TABLE discovery_issues (
    path TEXT NOT NULL,
    code TEXT NOT NULL CHECK (
        code IN ('blocked_permission', 'missing_root', 'scan_error')
    ),
    affected_capability TEXT NOT NULL CHECK (
        affected_capability = 'project_discovery'
    ),
    remediation TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (path, code)
);

CREATE TABLE discovery_duplicate_candidates (
    fingerprint_kind TEXT NOT NULL CHECK (
        fingerprint_kind IN ('git_remote', 'manifest')
    ),
    fingerprint TEXT NOT NULL,
    candidate_path TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (fingerprint_kind, fingerprint, candidate_path)
);

CREATE INDEX idx_discovery_issues_code
    ON discovery_issues(code, path);

CREATE INDEX idx_discovery_duplicates_fingerprint
    ON discovery_duplicate_candidates(fingerprint_kind, fingerprint, candidate_path);
