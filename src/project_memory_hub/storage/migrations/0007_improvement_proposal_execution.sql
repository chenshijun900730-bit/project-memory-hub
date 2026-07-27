ALTER TABLE improvement_proposals
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'legacy'
    CHECK (origin IN (
        'legacy', 'local_cli', 'codex_task', 'control_panel', 'analyzer'
    ));

ALTER TABLE improvement_proposals ADD COLUMN approval_actor TEXT;
ALTER TABLE improvement_proposals ADD COLUMN updated_at TEXT;
ALTER TABLE improvement_proposals ADD COLUMN apply_attempt_id TEXT;
ALTER TABLE improvement_proposals ADD COLUMN repository_root TEXT;
ALTER TABLE improvement_proposals ADD COLUMN original_branch TEXT;
ALTER TABLE improvement_proposals ADD COLUMN base_commit TEXT;
ALTER TABLE improvement_proposals ADD COLUMN proposal_branch TEXT;
ALTER TABLE improvement_proposals ADD COLUMN applied_commit TEXT;
ALTER TABLE improvement_proposals ADD COLUMN applied_at TEXT;
ALTER TABLE improvement_proposals ADD COLUMN rolled_back_at TEXT;
ALTER TABLE improvement_proposals ADD COLUMN failure_code TEXT;

UPDATE improvement_proposals
SET updated_at = created_at
WHERE updated_at IS NULL;
