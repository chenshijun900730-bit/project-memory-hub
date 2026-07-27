CREATE TABLE pending_capture_history (
    pending_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    claimed_source_agent TEXT NOT NULL,
    claimed_model_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    structured_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finalized_at TEXT NOT NULL,
    final_state TEXT NOT NULL CHECK (final_state IN ('verified', 'expired', 'rejected')),
    source_reference_id TEXT REFERENCES source_refs(source_reference_id) ON DELETE SET NULL,
    UNIQUE(
        project_id,
        claimed_source_agent,
        claimed_model_id,
        source_record_id,
        structured_hash
    )
);

INSERT INTO pending_capture_history(
    pending_id, project_id, claimed_source_agent, claimed_model_id,
    source_record_id, structured_hash, created_at, expires_at,
    finalized_at, final_state, source_reference_id
)
SELECT
    pending.pending_id,
    pending.project_id,
    pending.claimed_source_agent,
    pending.claimed_model_id,
    pending.source_record_id,
    pending.structured_hash,
    pending.created_at,
    pending.expires_at,
    CASE
        WHEN pending.verification_state = 'expired'
             AND strict_utc_epoch_us(pending.expires_at) IS NOT NULL
            THEN pending.expires_at
        WHEN strict_utc_epoch_us(pending.created_at) IS NOT NULL
            THEN pending.created_at
        ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    END,
    pending.verification_state,
    NULL
FROM pending_captures AS pending
WHERE pending.verification_state IN ('verified', 'expired', 'rejected')
ORDER BY
    strict_utc_epoch_us(
        CASE
            WHEN pending.verification_state = 'expired'
                 AND strict_utc_epoch_us(pending.expires_at) IS NOT NULL
                THEN pending.expires_at
            WHEN strict_utc_epoch_us(pending.created_at) IS NOT NULL
                THEN pending.created_at
            ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        END
    ) DESC,
    pending.pending_id DESC
LIMIT 50000;

CREATE TABLE pending_captures_active (
    pending_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    claimed_source_agent TEXT NOT NULL,
    claimed_model_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    structured_payload_json TEXT NOT NULL,
    structured_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    verification_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (verification_state = 'pending'),
    UNIQUE(
        project_id,
        claimed_source_agent,
        claimed_model_id,
        source_record_id,
        structured_hash
    )
);

INSERT INTO pending_captures_active(
    pending_id, project_id, claimed_source_agent, claimed_model_id,
    source_record_id, structured_payload_json, structured_hash,
    created_at, expires_at, verification_state
)
SELECT
    pending_id, project_id, claimed_source_agent, claimed_model_id,
    source_record_id, structured_payload_json, structured_hash,
    created_at, expires_at, 'pending'
FROM pending_captures
WHERE verification_state = 'pending';

DROP INDEX idx_pending_captures_verification_expiry;
DROP TABLE pending_captures;
ALTER TABLE pending_captures_active RENAME TO pending_captures;

CREATE INDEX idx_pending_captures_verification_expiry
    ON pending_captures(verification_state, expires_at, pending_id);
CREATE INDEX idx_pending_captures_project_state
    ON pending_captures(project_id, verification_state);
CREATE INDEX idx_pending_capture_history_finalized
    ON pending_capture_history(
        strict_utc_epoch_us(finalized_at),
        finalized_at,
        pending_id
    );
CREATE INDEX idx_pending_capture_history_project_finalized
    ON pending_capture_history(project_id, finalized_at, pending_id);

DELETE FROM app_state WHERE name GLOB 'pending_confirmation:*';
