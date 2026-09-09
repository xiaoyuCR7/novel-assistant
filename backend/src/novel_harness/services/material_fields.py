"""Allowlisted typed edits reusing creation schemas and Vault reference checks."""

from fastapi import HTTPException
from pydantic import ValidationError

from novel_harness.db.models import ChapterVersion, Entity, StoryNode
from novel_harness.schemas import story
from novel_harness.services.serialization import serialize
from novel_harness.services.story import _require_same_project

SCHEMAS = {
    "entity": story.EntityCreate,
    "relation": story.RelationCreate,
    "node": story.NodeCreate,
    "canon": story.CanonFactCreate,
    "plot": story.PlotThreadCreate,
    "timeline": story.TimelineEventCreate,
    "style": story.StyleProfileCreate,
    "idea": story.IdeaCreate,
}
REFERENCES = {
    "subject_entity_id": Entity,
    "pov_entity_id": Entity,
    "source_entity_id": Entity,
    "target_entity_id": Entity,
    "parent_id": StoryNode,
    "chapter_id": StoryNode,
    "valid_from_node_id": StoryNode,
    "valid_to_node_id": StoryNode,
    "start_node_id": StoryNode,
    "due_node_id": StoryNode,
    "source_version_id": ChapterVersion,
}


def validate_material_values(session, project_id, kind, values):
    """Validate project-bound references and ordered story ranges."""

    for field, model in REFERENCES.items():
        if kind == "node" and field == "parent_id":
            continue
        record_id = values.get(field)
        if not record_id:
            continue
        record = _require_same_project(session, model, record_id, project_id)
        if isinstance(record, StoryNode) and record.deleted_at is not None:
            raise HTTPException(404, detail={"code": "REFERENCE_NOT_FOUND"})
        if field == "source_version_id":
            chapter = _require_same_project(
                session, StoryNode, record.chapter_id, project_id
            )
            if chapter.deleted_at is not None or chapter.kind != "chapter":
                raise HTTPException(404, detail={"code": "REFERENCE_NOT_FOUND"})
    if kind in {"canon", "plot"}:
        from novel_harness.services.retrieval import ordered_nodes

        positions = {node.id: i for i, node in enumerate(ordered_nodes(session))}
        start, end = (
            ("valid_from_node_id", "valid_to_node_id")
            if kind == "canon"
            else ("start_node_id", "due_node_id")
        )
        if (
            values.get(start)
            and values.get(end)
            and positions[values[start]] > positions[values[end]]
        ):
            raise HTTPException(422, detail={"code": "INVALID_STORY_RANGE"})
    return values


def validate_node_ancestors(session, item, parent_id):
    seen = {item.id}
    while parent_id:
        if parent_id in seen:
            raise HTTPException(422, detail={"code": "NODE_CYCLE"})
        seen.add(parent_id)
        parent = session.get(StoryNode, parent_id)
        if parent is None or parent.deleted_at:
            raise HTTPException(
                422,
                detail={
                    "code": "NODE_PARENT_UNAVAILABLE",
                    "message": "上级节点已删除或不存在，请先恢复上级节点，或将此节点移至有效目录。",
                },
            )
        if parent.project_id != item.project_id:
            raise HTTPException(409, detail={"code": "CROSS_PROJECT_REFERENCE"})
        parent_id = parent.parent_id


def validate_fields(session, kind, item, fields, values):
    schema = SCHEMAS.get(kind)
    allowed = set(schema.model_fields) if schema else set()
    if kind == "node":
        allowed -= {"kind", "status"}  # Chapter status belongs to completion workflow.
    if set(fields) - allowed or set(fields) & set(values):
        raise HTTPException(422, detail={"code": "INVALID_MATERIAL_FIELDS"})
    if schema is None:
        return {}
    try:
        checked = schema.model_validate({**serialize(item), **values, **fields}).model_dump()
    except ValidationError:
        raise HTTPException(422, detail={"code": "INVALID_MATERIAL_FIELDS"}) from None
    if kind == "node":
        validate_node_ancestors(session, item, checked.get("parent_id"))
    validate_material_values(session, item.project_id, kind, checked)
    return {field: checked[field] for field in fields}
