ALTER TABLE source_refs ADD COLUMN capture_project_id TEXT
    REFERENCES projects(project_id) ON DELETE RESTRICT;
ALTER TABLE source_refs ADD COLUMN capture_model_id TEXT
    CHECK (capture_model_id IS NULL OR length(trim(capture_model_id)) > 0);

UPDATE source_refs AS source
SET capture_project_id = (
        SELECT baseline.project_id
        FROM behavior_memories AS baseline
        WHERE baseline.source_reference_id = source.source_reference_id
        ORDER BY baseline.memory_id
        LIMIT 1
    ),
    capture_model_id = (
        SELECT baseline.model_id
        FROM behavior_memories AS baseline
        WHERE baseline.source_reference_id = source.source_reference_id
        ORDER BY baseline.memory_id
        LIMIT 1
    )
WHERE source.parser_version = 'capture-v1'
  AND source.source_path IS NULL
  AND EXISTS (
      SELECT 1
      FROM behavior_memories AS present
      WHERE present.source_reference_id = source.source_reference_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM behavior_memories AS candidate
      WHERE candidate.source_reference_id = source.source_reference_id
        AND (
            candidate.source_agent <> source.source_agent
            OR candidate.project_id <> (
                SELECT baseline.project_id
                FROM behavior_memories AS baseline
                WHERE baseline.source_reference_id = source.source_reference_id
                ORDER BY baseline.memory_id
                LIMIT 1
            )
            OR candidate.model_id <> (
                SELECT baseline.model_id
                FROM behavior_memories AS baseline
                WHERE baseline.source_reference_id = source.source_reference_id
                ORDER BY baseline.memory_id
                LIMIT 1
            )
        )
  );

CREATE TRIGGER capture_provenance_pair_insert
BEFORE INSERT ON source_refs
WHEN (NEW.capture_project_id IS NULL) <> (NEW.capture_model_id IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'capture provenance requires project and model');
END;

CREATE TRIGGER capture_provenance_pair_update
BEFORE UPDATE OF capture_project_id, capture_model_id ON source_refs
WHEN (NEW.capture_project_id IS NULL) <> (NEW.capture_model_id IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'capture provenance requires project and model');
END;

CREATE TABLE memory_issue_resolutions (
    resolution_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    source_agent TEXT NOT NULL,
    model_id TEXT NOT NULL CHECK (length(trim(model_id)) > 0),
    target_content_hash TEXT NOT NULL CHECK (length(target_content_hash) = 64),
    target_memory_id TEXT REFERENCES behavior_memories(memory_id) ON DELETE RESTRICT,
    source_reference_id TEXT NOT NULL
        REFERENCES source_refs(source_reference_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'not_found')),
    resolved_at TEXT NOT NULL,
    CHECK (
        (status = 'resolved' AND target_memory_id IS NOT NULL)
        OR (status = 'not_found' AND target_memory_id IS NULL)
    )
);

CREATE UNIQUE INDEX idx_issue_resolutions_resolved_unique
ON memory_issue_resolutions(
    project_id,
    source_agent,
    model_id,
    source_reference_id,
    target_content_hash,
    target_memory_id
)
WHERE status = 'resolved';

CREATE UNIQUE INDEX idx_issue_resolutions_not_found_unique
ON memory_issue_resolutions(
    project_id,
    source_agent,
    model_id,
    source_reference_id,
    target_content_hash
)
WHERE status = 'not_found';

CREATE INDEX idx_issue_resolutions_target
ON memory_issue_resolutions(project_id, source_agent, model_id, target_memory_id)
WHERE status = 'resolved';

CREATE TRIGGER issue_resolution_target_insert
BEFORE INSERT ON memory_issue_resolutions
WHEN NEW.target_memory_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1
     FROM behavior_memories AS target
     WHERE target.memory_id = NEW.target_memory_id
       AND target.memory_kind = 'open_issue'
       AND target.project_id = NEW.project_id
       AND target.source_agent = NEW.source_agent
       AND target.model_id = NEW.model_id
 )
BEGIN
    SELECT RAISE(ABORT, 'resolution target namespace mismatch');
END;

CREATE TRIGGER issue_resolution_target_update
BEFORE UPDATE OF project_id, source_agent, model_id, target_memory_id, status
ON memory_issue_resolutions
WHEN NEW.target_memory_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1
     FROM behavior_memories AS target
     WHERE target.memory_id = NEW.target_memory_id
       AND target.memory_kind = 'open_issue'
       AND target.project_id = NEW.project_id
       AND target.source_agent = NEW.source_agent
       AND target.model_id = NEW.model_id
 )
BEGIN
    SELECT RAISE(ABORT, 'resolution target namespace mismatch');
END;

CREATE TRIGGER issue_resolution_source_insert
BEFORE INSERT ON memory_issue_resolutions
WHEN NOT EXISTS (
    SELECT 1
    FROM source_refs AS source
    WHERE source.source_reference_id = NEW.source_reference_id
      AND source.source_agent = NEW.source_agent
      AND source.capture_project_id = NEW.project_id
      AND source.capture_model_id = NEW.model_id
)
BEGIN
    SELECT RAISE(ABORT, 'resolution source namespace mismatch');
END;

CREATE TRIGGER issue_resolution_source_update
BEFORE UPDATE OF project_id, source_agent, model_id, source_reference_id
ON memory_issue_resolutions
WHEN NOT EXISTS (
    SELECT 1
    FROM source_refs AS source
    WHERE source.source_reference_id = NEW.source_reference_id
      AND source.source_agent = NEW.source_agent
      AND source.capture_project_id = NEW.project_id
      AND source.capture_model_id = NEW.model_id
)
BEGIN
    SELECT RAISE(ABORT, 'resolution source namespace mismatch');
END;

CREATE TRIGGER issue_resolution_source_ref_update
BEFORE UPDATE OF source_agent, capture_project_id, capture_model_id
ON source_refs
WHEN EXISTS (
    SELECT 1
    FROM memory_issue_resolutions AS resolution
    WHERE resolution.source_reference_id = OLD.source_reference_id
      AND (
          NEW.source_agent IS NOT resolution.source_agent
          OR NEW.capture_project_id IS NOT resolution.project_id
          OR NEW.capture_model_id IS NOT resolution.model_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'resolution source namespace mismatch');
END;

CREATE TRIGGER issue_resolution_target_memory_update
BEFORE UPDATE OF project_id, source_agent, model_id, memory_kind
ON behavior_memories
WHEN EXISTS (
    SELECT 1
    FROM memory_issue_resolutions AS resolution
    WHERE resolution.target_memory_id = OLD.memory_id
      AND (
          NEW.project_id IS NOT resolution.project_id
          OR NEW.source_agent IS NOT resolution.source_agent
          OR NEW.model_id IS NOT resolution.model_id
          OR NEW.memory_kind IS NOT 'open_issue'
      )
)
BEGIN
    SELECT RAISE(ABORT, 'resolution target namespace mismatch');
END;
