CREATE INDEX idx_behavior_memories_compaction_kind_order
    ON behavior_memories(
        project_id,
        source_agent,
        model_id,
        lifecycle_state,
        memory_kind ASC,
        created_at DESC,
        memory_id
    );
