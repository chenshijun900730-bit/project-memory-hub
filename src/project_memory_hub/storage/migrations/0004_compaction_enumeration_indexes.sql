CREATE INDEX idx_behavior_memories_active_namespace
    ON behavior_memories(project_id, source_agent, model_id)
    WHERE lifecycle_state = 'active'
      AND memory_kind <> 'retrospective';

CREATE INDEX idx_projects_active_observed_julianday
    ON projects(julianday(last_observed_change), project_id)
    WHERE enabled = 1
      AND inactivity_state = 'active'
      AND last_observed_change IS NOT NULL
      AND (
          last_observed_change GLOB '*Z'
          OR last_observed_change GLOB '*[T ][0-9][0-9]:[0-9][0-9]*[-+]*'
      );
