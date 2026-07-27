ALTER TABLE source_refs ADD COLUMN capture_correlation_id TEXT
    CHECK (
        capture_correlation_id IS NULL
        OR length(trim(capture_correlation_id)) > 0
    );

CREATE TEMP TABLE pmh_v12_verified_correlations (
    source_reference_id TEXT NOT NULL,
    capture_correlation_id TEXT NOT NULL,
    PRIMARY KEY(source_reference_id, capture_correlation_id)
) WITHOUT ROWID;

INSERT INTO pmh_v12_verified_correlations(
    source_reference_id,
    capture_correlation_id
)
SELECT DISTINCT
    history.source_reference_id,
    history.source_record_id
FROM pending_capture_history AS history
JOIN source_refs AS source
  ON source.source_reference_id = history.source_reference_id
 AND source.source_agent = history.claimed_source_agent
 AND source.capture_project_id = history.project_id
 AND source.capture_model_id = history.claimed_model_id
 AND source.content_hash = history.structured_hash
WHERE history.final_state = 'verified'
  AND history.source_reference_id IS NOT NULL;

CREATE TEMP TABLE pmh_v12_invalid_verified_sources (
    source_reference_id TEXT PRIMARY KEY
) WITHOUT ROWID;

INSERT INTO pmh_v12_invalid_verified_sources(source_reference_id)
SELECT DISTINCT source.source_reference_id
FROM source_refs AS source
JOIN pending_capture_history AS history
  ON history.source_reference_id = source.source_reference_id
WHERE history.final_state = 'verified'
  AND (
      history.claimed_source_agent IS NOT source.source_agent
      OR history.project_id IS NOT source.capture_project_id
      OR history.claimed_model_id IS NOT source.capture_model_id
      OR history.structured_hash IS NOT source.content_hash
  );

CREATE TEMP TABLE pmh_v12_unique_verified_correlations (
    source_reference_id TEXT PRIMARY KEY,
    capture_correlation_id TEXT NOT NULL
) WITHOUT ROWID;

INSERT INTO pmh_v12_unique_verified_correlations(
    source_reference_id,
    capture_correlation_id
)
SELECT
    verified.source_reference_id,
    MIN(verified.capture_correlation_id)
FROM pmh_v12_verified_correlations AS verified
GROUP BY verified.source_reference_id
HAVING COUNT(*) = 1;

CREATE TEMP TABLE pmh_v12_conflicted_correlations (
    source_agent TEXT NOT NULL,
    capture_project_id TEXT NOT NULL,
    capture_model_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    capture_correlation_id TEXT NOT NULL,
    PRIMARY KEY(
        source_agent,
        capture_project_id,
        capture_model_id,
        content_hash,
        capture_correlation_id
    )
) WITHOUT ROWID;

INSERT INTO pmh_v12_conflicted_correlations(
    source_agent,
    capture_project_id,
    capture_model_id,
    content_hash,
    capture_correlation_id
)
SELECT
    source.source_agent,
    source.capture_project_id,
    source.capture_model_id,
    source.content_hash,
    verified.capture_correlation_id
FROM source_refs AS source
JOIN pmh_v12_verified_correlations AS verified
  ON verified.source_reference_id = source.source_reference_id
WHERE source.capture_project_id IS NOT NULL
  AND source.capture_model_id IS NOT NULL
GROUP BY
    source.source_agent,
    source.capture_project_id,
    source.capture_model_id,
    source.content_hash,
    verified.capture_correlation_id
HAVING COUNT(*) > 1;

CREATE TEMP TABLE pmh_v12_backfill_correlations (
    source_reference_id TEXT PRIMARY KEY,
    capture_correlation_id TEXT NOT NULL
) WITHOUT ROWID;

INSERT INTO pmh_v12_backfill_correlations(
    source_reference_id,
    capture_correlation_id
)
SELECT
    source.source_reference_id,
    unique_verified.capture_correlation_id
FROM source_refs AS source
JOIN pmh_v12_unique_verified_correlations AS unique_verified
  ON unique_verified.source_reference_id = source.source_reference_id
LEFT JOIN pmh_v12_invalid_verified_sources AS invalid_verified
  ON invalid_verified.source_reference_id = source.source_reference_id
LEFT JOIN pmh_v12_conflicted_correlations AS conflicted
  ON conflicted.source_agent = source.source_agent
 AND conflicted.capture_project_id = source.capture_project_id
 AND conflicted.capture_model_id = source.capture_model_id
 AND conflicted.content_hash = source.content_hash
 AND conflicted.capture_correlation_id = unique_verified.capture_correlation_id
WHERE source.parser_version = 'capture-v1'
  AND source.source_path IS NULL
  AND source.capture_project_id IS NOT NULL
  AND source.capture_model_id IS NOT NULL
  AND invalid_verified.source_reference_id IS NULL
  AND conflicted.source_agent IS NULL;

UPDATE source_refs AS source
SET capture_correlation_id = (
    SELECT backfill.capture_correlation_id
    FROM pmh_v12_backfill_correlations AS backfill
    WHERE backfill.source_reference_id = source.source_reference_id
)
WHERE source.source_reference_id IN (
    SELECT backfill.source_reference_id
    FROM pmh_v12_backfill_correlations AS backfill
);

DROP TABLE pmh_v12_backfill_correlations;
DROP TABLE pmh_v12_conflicted_correlations;
DROP TABLE pmh_v12_unique_verified_correlations;
DROP TABLE pmh_v12_invalid_verified_sources;
DROP TABLE pmh_v12_verified_correlations;

CREATE UNIQUE INDEX idx_source_refs_capture_correlation
ON source_refs(
    source_agent,
    capture_project_id,
    capture_model_id,
    capture_correlation_id,
    content_hash
)
WHERE capture_correlation_id IS NOT NULL;

CREATE TRIGGER capture_correlation_insert
BEFORE INSERT ON source_refs
WHEN NEW.capture_correlation_id IS NOT NULL
 AND (
     NEW.parser_version <> 'capture-v1'
     OR NEW.source_path IS NOT NULL
     OR NEW.capture_project_id IS NULL
     OR NEW.capture_model_id IS NULL
 )
BEGIN
    SELECT RAISE(ABORT, 'capture correlation requires capture provenance');
END;

CREATE TRIGGER capture_correlation_update
BEFORE UPDATE OF
    capture_correlation_id,
    parser_version,
    source_path,
    capture_project_id,
    capture_model_id
ON source_refs
WHEN NEW.capture_correlation_id IS NOT NULL
 AND (
     NEW.parser_version <> 'capture-v1'
     OR NEW.source_path IS NOT NULL
     OR NEW.capture_project_id IS NULL
     OR NEW.capture_model_id IS NULL
 )
BEGIN
    SELECT RAISE(ABORT, 'capture correlation requires capture provenance');
END;
