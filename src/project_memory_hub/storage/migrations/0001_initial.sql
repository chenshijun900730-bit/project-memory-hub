CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    git_root TEXT,
    git_remote_fingerprint TEXT,
    manifest_fingerprint TEXT,
    discovery_status TEXT NOT NULL DEFAULT 'active',
    permission_status TEXT NOT NULL DEFAULT 'ok',
    last_observed_change TEXT,
    inactivity_state TEXT NOT NULL DEFAULT 'active',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE project_facts (
    fact_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    evidence_reference TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    supersedes_fact_id TEXT REFERENCES project_facts(fact_id),
    stale_at TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active', 'cold', 'archived', 'deleted')),
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE project_facts_fts USING fts5(
    normalized_content,
    category,
    content='project_facts',
    content_rowid='rowid'
);

CREATE TRIGGER project_facts_ai AFTER INSERT ON project_facts BEGIN
    INSERT INTO project_facts_fts(rowid, normalized_content, category)
    VALUES (new.rowid, new.normalized_content, new.category);
END;

CREATE TRIGGER project_facts_ad AFTER DELETE ON project_facts BEGIN
    INSERT INTO project_facts_fts(
        project_facts_fts, rowid, normalized_content, category
    ) VALUES (
        'delete', old.rowid, old.normalized_content, old.category
    );
END;

CREATE TRIGGER project_facts_au AFTER UPDATE ON project_facts BEGIN
    INSERT INTO project_facts_fts(
        project_facts_fts, rowid, normalized_content, category
    ) VALUES (
        'delete', old.rowid, old.normalized_content, old.category
    );
    INSERT INTO project_facts_fts(rowid, normalized_content, category)
    VALUES (new.rowid, new.normalized_content, new.category);
END;

CREATE TABLE source_refs (
    source_reference_id TEXT PRIMARY KEY,
    source_agent TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_path TEXT,
    content_hash TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_agent, source_record_id, content_hash)
);

CREATE TABLE behavior_memories (
    memory_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    source_agent TEXT NOT NULL,
    model_id TEXT NOT NULL CHECK (length(trim(model_id)) > 0),
    task_fingerprint TEXT NOT NULL,
    memory_kind TEXT NOT NULL CHECK (
        memory_kind IN (
            'decision',
            'failed_attempt',
            'verified_method',
            'preference',
            'risk',
            'open_issue',
            'reusable_lesson',
            'outcome',
            'retrospective'
        )
    ),
    normalized_content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_reference_id TEXT NOT NULL REFERENCES source_refs(source_reference_id),
    created_at TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    lifecycle_state TEXT NOT NULL DEFAULT 'active'
        CHECK (lifecycle_state IN ('active', 'cold', 'archived', 'deleted')),
    UNIQUE(
        project_id,
        source_agent,
        model_id,
        task_fingerprint,
        memory_kind,
        content_hash
    )
);

CREATE TABLE pending_captures (
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
        CHECK (verification_state IN ('pending', 'verified', 'expired', 'rejected')),
    UNIQUE(
        project_id,
        claimed_source_agent,
        claimed_model_id,
        source_record_id,
        structured_hash
    )
);

CREATE TABLE memory_promotions (
    promotion_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES behavior_memories(memory_id) ON DELETE RESTRICT,
    proposed_rule TEXT NOT NULL,
    requester TEXT NOT NULL,
    approval_actor TEXT,
    requested_at TEXT NOT NULL,
    approved_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE TABLE checkpoints (
    adapter TEXT NOT NULL,
    scope TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(adapter, scope)
);

CREATE TABLE import_receipts (
    source_hash TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY(source_hash, source_record_id)
);

CREATE TABLE retry_items (
    retry_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_attempt_at TEXT
);

CREATE TABLE improvement_proposals (
    proposal_id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    patch TEXT,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')),
    verification_argv_json TEXT NOT NULL DEFAULT '[]',
    verification_summary TEXT NOT NULL DEFAULT '',
    approval_status TEXT NOT NULL DEFAULT 'draft' CHECK (
        approval_status IN (
            'draft',
            'approved',
            'applying',
            'applied',
            'rejected',
            'failed',
            'rolled_back'
        )
    ),
    target_version TEXT,
    rollback_ref TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE app_state (
    name TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_projects_canonical_path ON projects(canonical_path);
CREATE INDEX idx_project_facts_project_category_lifecycle
    ON project_facts(project_id, category, lifecycle_state);
CREATE INDEX idx_behavior_memories_project_namespace_lifecycle
    ON behavior_memories(project_id, source_agent, model_id, lifecycle_state);
CREATE INDEX idx_pending_captures_verification_expiry
    ON pending_captures(verification_state, expires_at);
CREATE INDEX idx_checkpoints_adapter_scope ON checkpoints(adapter, scope);
CREATE INDEX idx_retry_items_created_at ON retry_items(created_at);
CREATE INDEX idx_improvement_proposals_status_created_at
    ON improvement_proposals(approval_status, created_at);
CREATE UNIQUE INDEX idx_improvement_proposals_active_signature
    ON improvement_proposals(signature)
    WHERE approval_status IN ('draft', 'approved', 'applying');
