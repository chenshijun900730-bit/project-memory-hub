ALTER TABLE projects ADD COLUMN path_device INTEGER
CHECK (path_device IS NULL OR (typeof(path_device) = 'integer' AND path_device >= 0));
ALTER TABLE projects ADD COLUMN path_inode INTEGER
CHECK (
    (path_device IS NULL) = (path_inode IS NULL)
    AND (path_inode IS NULL OR (typeof(path_inode) = 'integer' AND path_inode >= 0))
);

CREATE UNIQUE INDEX idx_projects_path_identity
ON projects(path_device, path_inode)
WHERE path_device IS NOT NULL AND path_inode IS NOT NULL;

CREATE TABLE project_registry_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL
        CHECK (typeof(generation) = 'integer' AND generation >= 0)
);

INSERT INTO project_registry_state(singleton, generation) VALUES (1, 0);

CREATE TRIGGER projects_registry_generation_insert
AFTER INSERT ON projects
BEGIN
    UPDATE project_registry_state
    SET generation = generation + 1
    WHERE singleton = 1;
END;

CREATE TRIGGER projects_registry_generation_delete
AFTER DELETE ON projects
BEGIN
    UPDATE project_registry_state
    SET generation = generation + 1
    WHERE singleton = 1;
END;

CREATE TRIGGER projects_registry_generation_update
AFTER UPDATE OF
    project_id,
    canonical_path,
    display_name,
    git_remote_fingerprint,
    enabled,
    path_device,
    path_inode
ON projects
BEGIN
    UPDATE project_registry_state
    SET generation = generation + 1
    WHERE singleton = 1;
END;

INSERT INTO app_state(name, value_json, updated_at)
SELECT
    'reconcile_catchup_required',
    '{"required":true}',
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE EXISTS (SELECT 1 FROM projects)
ON CONFLICT(name) DO UPDATE SET
    value_json = excluded.value_json,
    updated_at = excluded.updated_at;
