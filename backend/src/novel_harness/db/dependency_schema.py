"""Additive indexes for dependency lifecycle queries in existing Vaults."""

DEPENDENCY_INDEXES = {
    "ix_canon_facts_subject_entity_id": (
        "canon_facts",
        "subject_entity_id",
    ),
    "ix_entity_relations_source_entity_id": (
        "entity_relations",
        "source_entity_id",
    ),
    "ix_entity_relations_target_entity_id": (
        "entity_relations",
        "target_entity_id",
    ),
}

CANDIDATE_PAGE_INDEXES = {
    "ix_memory_candidates_project_created_id": (
        "memory_candidates",
        ("project_id", "created_at", "id"),
    ),
    "ix_memory_candidates_project_status_created_id": (
        "memory_candidates",
        ("project_id", "status", "created_at", "id"),
    ),
    "ix_memory_candidates_project_chapter_created_id": (
        "memory_candidates",
        ("project_id", "chapter_id", "created_at", "id"),
    ),
    "ix_memory_candidates_project_status_chapter_created_id": (
        "memory_candidates",
        ("project_id", "status", "chapter_id", "created_at", "id"),
    ),
    "ix_generated_memory_candidates_project_created_id": (
        "generated_memory_candidates",
        ("project_id", "created_at", "id"),
    ),
    "ix_generated_memory_candidates_project_status_created_id": (
        "generated_memory_candidates",
        ("project_id", "status", "created_at", "id"),
    ),
    "ix_generated_memory_candidates_project_chapter_created_id": (
        "generated_memory_candidates",
        ("project_id", "chapter_id", "created_at", "id"),
    ),
    "ix_generated_memory_candidates_project_status_chapter_created_id": (
        "generated_memory_candidates",
        ("project_id", "status", "chapter_id", "created_at", "id"),
    ),
}


def ensure_dependency_indexes(connection):
    for name, (table, column) in DEPENDENCY_INDEXES.items():
        connection.exec_driver_sql(
            f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ("{column}")'
        )
    for name, (table, columns) in CANDIDATE_PAGE_INDEXES.items():
        quoted = ", ".join(f'"{column}"' for column in columns)
        connection.exec_driver_sql(
            f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ({quoted})'
        )
