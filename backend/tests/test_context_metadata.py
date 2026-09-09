import json

from novel_harness.db.models import Entity
from novel_harness.services.projects import require_project
from novel_harness.services.writing_context import collect_hard_context


def test_required_material_keeps_semantics_and_revision_without_storage_metadata(
    client, project, seeded_chapter
):
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.session_scope() as session:
        entity = Entity(
            project_id=project["id"],
            kind="character",
            name="林渡",
            summary="邮差",
            state={"alive": True},
        )
        session.add(entity)
        session.flush()
        hard, _ = collect_hard_context(
            session,
            require_project(session, project["id"]),
            seeded_chapter,
            {"required_entity_ids": [entity.id]},
        )
        content = json.loads(
            next(item.content for item in hard if item.source_type == "story_entity")
        )
        assert content["name"] == "林渡" and content["state"] == {"alive": True}
        assert content["id"] == entity.id and content["revision"] == entity.revision
        assert (
            not {"created_at", "updated_at", "deleted_at", "purge_after", "project_id", "is_pinned"}
            & content.keys()
        )
