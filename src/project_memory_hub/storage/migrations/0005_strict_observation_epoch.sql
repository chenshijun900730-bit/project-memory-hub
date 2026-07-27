ALTER TABLE projects
    ADD COLUMN last_observed_change_epoch_us INTEGER;

UPDATE projects
SET last_observed_change_epoch_us = strict_utc_epoch_us(last_observed_change);

DROP INDEX IF EXISTS idx_projects_active_observed_julianday;

CREATE INDEX idx_projects_active_observed_epoch
    ON projects(last_observed_change_epoch_us, project_id)
    WHERE enabled = 1
      AND inactivity_state = 'active'
      AND last_observed_change_epoch_us IS NOT NULL;

CREATE TRIGGER projects_observed_epoch_ai
AFTER INSERT ON projects
WHEN NEW.last_observed_change_epoch_us
     IS NOT strict_utc_epoch_us(NEW.last_observed_change)
BEGIN
    UPDATE projects
    SET last_observed_change_epoch_us = strict_utc_epoch_us(NEW.last_observed_change)
    WHERE project_id = NEW.project_id;
END;

CREATE TRIGGER projects_observed_epoch_au
AFTER UPDATE OF last_observed_change ON projects
WHEN NEW.last_observed_change_epoch_us
     IS NOT strict_utc_epoch_us(NEW.last_observed_change)
BEGIN
    UPDATE projects
    SET last_observed_change_epoch_us = strict_utc_epoch_us(NEW.last_observed_change)
    WHERE project_id = NEW.project_id;
END;

CREATE TRIGGER projects_observed_epoch_bu
BEFORE UPDATE OF last_observed_change_epoch_us ON projects
WHEN NEW.last_observed_change_epoch_us
     IS NOT strict_utc_epoch_us(NEW.last_observed_change)
BEGIN
    SELECT RAISE(ABORT, 'project observation epoch does not match timestamp');
END;
