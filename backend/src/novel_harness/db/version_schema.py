"""Additive schema maintenance for chapter-version history pages."""

VERSION_PAGE_INDEX_NAME = "ix_chapter_versions_chapter_created_id_desc"


def ensure_version_page_index(connection) -> None:
    """Create the keyset-pagination index for new and existing SQLite vaults."""
    connection.exec_driver_sql(
        f"CREATE INDEX IF NOT EXISTS {VERSION_PAGE_INDEX_NAME} "
        "ON chapter_versions (chapter_id, created_at DESC, id DESC)"
    )
