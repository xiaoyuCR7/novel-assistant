"""Story knowledge service and project-bound reference checks."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_harness.db.models import (
    CanonFact,
    Entity,
    EntityRelation,
    Idea,
    PlotThread,
    StoryNode,
    StyleProfile,
    TimelineEvent,
)
from novel_harness.services.projects import require_project
from novel_harness.services.serialization import serialize


def _require_same_project[ModelT](
    session: Session, model: type[ModelT], record_id: str, project_id: str
) -> ModelT:
    record = session.get(model, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "REFERENCE_NOT_FOUND"})
    if record.project_id != project_id:
        raise HTTPException(status_code=409, detail={"code": "CROSS_PROJECT_REFERENCE"})
    return record


def add_record[ModelT](
    session: Session, project_id: str, model: type[ModelT], values: dict
) -> ModelT:
    require_project(session, project_id)
    record = model(project_id=project_id, **values)
    session.add(record)
    session.flush()
    return record


def add_node(session: Session, project_id: str, values: dict) -> StoryNode:
    from novel_harness.services.material_fields import validate_node_ancestors
    from novel_harness.services.versions import begin_version_write

    begin_version_write(session)
    require_project(session, project_id)
    if values.get("parent_id"):
        _require_same_project(session, StoryNode, values["parent_id"], project_id)
    node = StoryNode(project_id=project_id, **values)
    validate_node_ancestors(session, node, node.parent_id)
    if values.get("pov_entity_id"):
        _require_same_project(session, Entity, values["pov_entity_id"], project_id)
    session.add(node)
    session.flush()
    return node


def add_relation(session: Session, project_id: str, values: dict) -> EntityRelation:
    _require_same_project(session, Entity, values["source_entity_id"], project_id)
    _require_same_project(session, Entity, values["target_entity_id"], project_id)
    return add_record(session, project_id, EntityRelation, values)


def add_canon(session: Session, project_id: str, values: dict) -> CanonFact:
    from novel_harness.services.material_fields import validate_material_values
    from novel_harness.services.versions import begin_version_write

    begin_version_write(session)
    validate_material_values(session, project_id, "canon", values)
    return add_record(session, project_id, CanonFact, values)


def add_timeline(session: Session, project_id: str, values: dict) -> TimelineEvent:
    if values.get("chapter_id"):
        _require_same_project(session, StoryNode, values["chapter_id"], project_id)
    return add_record(session, project_id, TimelineEvent, values)


def add_plot(session: Session, project_id: str, values: dict) -> PlotThread:
    from novel_harness.services.material_fields import validate_material_values
    from novel_harness.services.versions import begin_version_write

    begin_version_write(session)
    validate_material_values(session, project_id, "plot", values)
    return add_record(session, project_id, PlotThread, values)


def workspace(session: Session, project_id: str) -> dict:
    project = require_project(session, project_id)

    def records[ModelT](model: type[ModelT], *order_by) -> list[dict]:
        statement = select(model).where(model.project_id == project_id)
        if order_by:
            statement = statement.order_by(*order_by)
        return [serialize(item) for item in session.scalars(statement).all()]

    result = {
        "project": serialize(project),
        "ideas": records(Idea, Idea.created_at),
        "nodes": records(StoryNode, StoryNode.created_at),
        "entities": records(Entity, Entity.created_at),
        "relations": records(EntityRelation, EntityRelation.created_at),
        "canon_facts": records(CanonFact, CanonFact.created_at),
        "timeline": records(TimelineEvent, TimelineEvent.sort_key, TimelineEvent.created_at),
        "plots": records(PlotThread, PlotThread.created_at),
        "styles": records(StyleProfile, StyleProfile.created_at),
    }
    from novel_harness.services.memory_conflicts import detect_memory_conflicts
    from novel_harness.services.retrieval import ordered_nodes

    positions = {node.id: i for i, node in enumerate(ordered_nodes(session))}
    result["memory_conflicts"] = detect_memory_conflicts(
        result["styles"], result["canon_facts"], positions
    )
    return result


def navigation(session: Session, project_id: str) -> dict:
    project = require_project(session, project_id)
    table = StoryNode.__table__
    fields = ("id", "kind", "title", "status", "parent_id", "order_index", "target_words")
    nodes = session.execute(
        select(*(table.c[name] for name in fields))
        .where(table.c.project_id == project_id, table.c.deleted_at.is_(None))
        .order_by(table.c.created_at, table.c.id)
    ).mappings()
    return {"project": serialize(project), "nodes": [dict(row) for row in nodes]}


def workspace_view(session: Session, project_id: str, view: str) -> dict:
    views = {
        "ideas": {"ideas": Idea},
        "story": {"nodes": StoryNode, "plots": PlotThread},
        "entities": {"entities": Entity, "relations": EntityRelation},
        "knowledge": {"canon_facts": CanonFact, "timeline": TimelineEvent},
        "style": {"styles": StyleProfile},
        "library": {},
    }
    if view not in views:
        raise HTTPException(404, detail={"code": "WORKSPACE_VIEW_NOT_FOUND"})
    require_project(session, project_id)

    def records(model):
        order = (
            (model.sort_key, model.created_at) if model is TimelineEvent else (model.created_at,)
        )
        return [
            serialize(item)
            for item in session.scalars(
                select(model).where(model.project_id == project_id).order_by(*order)
            )
        ]

    result = {name: records(model) for name, model in views[view].items()}
    if view in {"style", "library"}:
        from novel_harness.services.memory_conflicts import detect_memory_conflicts
        from novel_harness.services.retrieval import ordered_nodes

        positions = {node.id: i for i, node in enumerate(ordered_nodes(session))}
        result["memory_conflicts"] = detect_memory_conflicts(
            result["styles"] if view == "style" else records(StyleProfile),
            records(CanonFact),
            positions,
        )
    return result
