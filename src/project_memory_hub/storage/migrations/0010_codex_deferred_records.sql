CREATE TABLE codex_deferred_records (
    deferred_id TEXT PRIMARY KEY NOT NULL CHECK (
        typeof(deferred_id) = 'text'
        AND length(deferred_id) = 36
        AND deferred_id = lower(deferred_id)
        AND substr(deferred_id, 9, 1) = '-'
        AND substr(deferred_id, 14, 1) = '-'
        AND substr(deferred_id, 19, 1) = '-'
        AND substr(deferred_id, 24, 1) = '-'
        AND substr(deferred_id, 15, 1) = '4'
        AND substr(deferred_id, 20, 1) IN ('8', '9', 'a', 'b')
        AND length(replace(deferred_id, '-', '')) = 32
        AND replace(deferred_id, '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    source_agent TEXT NOT NULL CHECK (
        typeof(source_agent) = 'text' AND source_agent = 'codex'
    ),
    scope TEXT NOT NULL CHECK (
        typeof(scope) = 'text'
        AND length(scope) BETWEEN 1 AND 4096
        AND length(CAST(scope AS BLOB)) <= 16384
        AND instr(scope, char(0)) = 0
        AND scope NOT GLOB '*[^A-Za-z0-9._/-]*'
        AND substr(scope, 1, 1) <> '/'
        AND substr(scope, -6) = '.jsonl'
        AND instr(scope, '//') = 0
        AND instr('/' || scope || '/', '/./') = 0
        AND instr('/' || scope || '/', '/../') = 0
        AND instr('/' || scope || '/', '/.git/') = 0
    ),
    source_record_id TEXT NOT NULL CHECK (
        typeof(source_record_id) = 'text'
        AND length(source_record_id) BETWEEN 1 AND 513
        AND length(CAST(source_record_id AS BLOB)) <= 2049
        AND instr(source_record_id, char(0)) = 0
        AND source_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
    ),
    parser_version TEXT NOT NULL CHECK (
        typeof(parser_version) = 'text' AND parser_version = 'codex-v3'
    ),
    source_device INTEGER NOT NULL CHECK (
        typeof(source_device) = 'integer' AND source_device >= 0
    ),
    source_inode INTEGER NOT NULL CHECK (
        typeof(source_inode) = 'integer' AND source_inode >= 0
    ),
    prefix_length INTEGER NOT NULL CHECK (
        typeof(prefix_length) = 'integer' AND prefix_length > 0
    ),
    prefix_sha256 TEXT NOT NULL CHECK (
        typeof(prefix_sha256) = 'text'
        AND length(prefix_sha256) = 64
        AND prefix_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    reason_code TEXT NOT NULL CHECK (
        typeof(reason_code) = 'text' AND reason_code = 'project_not_found'
    ),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (
        typeof(state) = 'text' AND state IN ('pending', 'recovered')
    ),
    first_seen_at TEXT NOT NULL CHECK (
        typeof(first_seen_at) = 'text'
        AND strict_utc_epoch_us(first_seen_at) IS NOT NULL
    ),
    last_attempt_at TEXT CHECK (
        last_attempt_at IS NULL OR (
            typeof(last_attempt_at) = 'text'
            AND strict_utc_epoch_us(last_attempt_at) IS NOT NULL
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(attempt_count) = 'integer' AND attempt_count BETWEEN 0 AND 2147483647
    ),
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR (
            typeof(last_error_code) = 'text'
            AND last_error_code IN (
                'project_not_found',
                'source_unavailable',
                'source_changed',
                'replay_limit',
                'ambiguous_source',
                'rejected'
            )
        )
    ),
    recovered_at TEXT CHECK (
        recovered_at IS NULL OR (
            typeof(recovered_at) = 'text'
            AND strict_utc_epoch_us(recovered_at) IS NOT NULL
        )
    ),
    CHECK (
        (state = 'pending' AND recovered_at IS NULL)
        OR (state = 'recovered' AND recovered_at IS NOT NULL)
    ),
    UNIQUE(
        source_agent,
        scope,
        source_device,
        source_inode,
        parser_version,
        source_record_id
    )
);

CREATE INDEX idx_codex_deferred_pending
ON codex_deferred_records(state, last_attempt_at, first_seen_at, deferred_id)
WHERE state = 'pending';
