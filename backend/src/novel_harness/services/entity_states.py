"""Author-managed temporal entity state storage and validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from novel_harness.db.models import ChapterVersion, Entity, EntityState, StoryNode
from novel_harness.services.projects import require_project
from novel_harness.services.retrieval import ordered_nodes
from novel_harness.services.serialization import serialize

ENTITY_STATE_CONFLICT_MESSAGE = (
    "当前章节的人物状态存在未决矛盾，请由作者确认。"
)


@dataclass(frozen=True, slots=True)
class ResolvedEntityState:
    """Chapter-scoped authoritative state plus unresolved evidence."""

    data: dict[str, Any]
    conflicts: list[dict[str, Any]]


def _entity_row(session: Session, entity: Entity | str) -> Entity:
    row = session.get(Entity, entity) if isinstance(entity, str) else entity
    if row is None or row.deleted_at is not None:
        raise HTTPException(404, detail={"code": "ENTITY_NOT_FOUND"})
    return row


def resolve_entity_state(
    session: Session, entity: Entity | str, chapter_id: str
) -> ResolvedEntityState:
    """Overlay confirmed records applicable at *chapter_id* without hiding disputes."""

    row = _entity_row(session, entity)
    positions = {
        node.id: index
        for index, node in enumerate(
            item
            for item in ordered_nodes(session)
            if item.project_id == row.project_id and item.deleted_at is None
        )
    }
    history = list(
        session.scalars(
            select(EntityState).where(
                EntityState.project_id == row.project_id,
                EntityState.entity_id == row.id,
                EntityState.status == "confirmed",
            )
        ).all()
    )
    return _resolve_entity_state(row, chapter_id, positions, history)


def _resolve_entity_state(
    entity: Entity,
    chapter_id: str,
    positions: dict[str, int],
    history: list[EntityState],
) -> ResolvedEntityState:
    """Pure overlay core shared by single-entity and batch resolution."""

    if chapter_id not in positions:
        raise HTTPException(404, detail={"code": "REFERENCE_NOT_FOUND"})
    if not history:
        return ResolvedEntityState(dict(entity.state or {}), [])

    current = positions[chapter_id]
    applicable = [
        state
        for state in history
        if state.valid_from_node_id in positions
        and positions[state.valid_from_node_id] <= current
        and (
            state.valid_to_node_id is None
            or (
                state.valid_to_node_id in positions
                and current <= positions[state.valid_to_node_id]
            )
        )
    ]
    applicable.sort(
        key=lambda state: (
            positions[state.valid_from_node_id],
            state.created_at,
            state.id,
        )
    )
    data: dict[str, Any] = {}
    origins: dict[str, tuple[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for state in applicable:
        for key, value in state.data.items():
            if key not in data:
                data[key] = value
                origins[key] = (state.id, value)
                continue
            if data[key] == value:
                continue
            origin_id, origin_value = origins[key]
            conflicts.append(
                {
                    "code": "ENTITY_STATE_CONFLICT",
                    "entity_id": entity.id,
                    "key": key,
                    "state_ids": [origin_id, state.id],
                    "values": [origin_value, value],
                }
            )
    return ResolvedEntityState(data, conflicts)


def serialize_entity_for_chapter(
    session: Session, entity: Entity | str, chapter_id: str
) -> dict[str, Any]:
    """Serialize an entity while replacing only its legacy state projection."""

    row = _entity_row(session, entity)
    result = serialize(row)
    result["state"] = resolve_entity_state(session, row, chapter_id).data
    return result


def project_entity_state_conflicts(
    session: Session, project_id: str, chapter_id: str
) -> list[dict[str, Any]]:
    """Collect chapter-scoped conflicts once in stable entity order."""

    return project_entity_state_conflicts_by_chapter(
        session,
        project_id,
        {chapter_id},
    )[chapter_id]


def project_entity_state_conflicts_by_chapter(
    session: Session,
    project_id: str,
    chapter_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Collect conflicts for many chapters while loading project state once."""

    positions = {
        node.id: index
        for index, node in enumerate(
            item
            for item in ordered_nodes(session)
            if item.project_id == project_id and item.deleted_at is None
        )
    }
    if chapter_ids.difference(positions):
        raise HTTPException(404, detail={"code": "REFERENCE_NOT_FOUND"})
    entities = list(session.scalars(
        select(Entity)
        .where(Entity.project_id == project_id, Entity.deleted_at.is_(None))
        .order_by(Entity.created_at, Entity.id)
    ).all())
    states_by_entity: dict[str, list[EntityState]] = {}
    for state in session.scalars(
        select(EntityState)
        .where(
            EntityState.project_id == project_id,
            EntityState.status == "confirmed",
        )
        .order_by(EntityState.created_at, EntityState.id)
    ).all():
        states_by_entity.setdefault(state.entity_id, []).append(state)
    return {
        chapter_id: [
            conflict
            for entity in entities
            for conflict in _resolve_entity_state(
                entity,
                chapter_id,
                positions,
                states_by_entity.get(entity.id, []),
            ).conflicts
        ]
        for chapter_id in chapter_ids
    }


def entity_state_conflict_detail(conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": "ENTITY_STATE_CONFLICT",
        "message": ENTITY_STATE_CONFLICT_MESSAGE,
        "conflicts": conflicts,
    }


def entity_state_conflict_evidence(conflict: dict[str, Any]) -> str:
    """Return the stable evidence identity stored on a generic conflict row."""

    return (
        f"entity_state:{conflict['entity_id']}:{conflict['key']}:"
        + ",".join(conflict["state_ids"])
    )


def assert_project_entity_states_resolved(
    session: Session, project_id: str, chapter_id: str
) -> None:
    """Raise a read-only author-decision error for any project state dispute."""

    conflicts = project_entity_state_conflicts(session, project_id, chapter_id)
    if conflicts:
        raise HTTPException(409, detail=entity_state_conflict_detail(conflicts))


def require_entity(session: Session, project_id: str, entity_id: str) -> Entity:
    require_project(session, project_id)
    entity = session.get(Entity, entity_id)
    if entity is None or entity.project_id != project_id or entity.deleted_at is not None:
        raise HTTPException(404, detail={"code": "ENTITY_NOT_FOUND"})
    return entity


def require_entity_state(session: Session, project_id: str, state_id: str) -> EntityState:
    require_project(session, project_id)
    state = session.get(EntityState, state_id)
    if state is None or state.project_id != project_id:
        raise HTTPException(404, detail={"code": "ENTITY_STATE_NOT_FOUND"})
    return state


def list_entity_states(
    session: Session, project_id: str, entity_id: str
) -> list[EntityState]:
    require_entity(session, project_id, entity_id)
    return list(
        session.scalars(
            select(EntityState)
            .where(
                EntityState.project_id == project_id,
                EntityState.entity_id == entity_id,
            )
            .order_by(EntityState.created_at, EntityState.id)
        ).all()
    )


def _active_node(session: Session, project_id: str, node_id: str) -> StoryNode:
    node = session.get(StoryNode, node_id)
    if node is None or node.project_id != project_id or node.deleted_at is not None:
        raise HTTPException(404, detail={"code": "REFERENCE_NOT_FOUND"})
    return node


def _source_version(
    session: Session, project_id: str, version_id: str | None
) -> ChapterVersion | None:
    if version_id is None:
        return None
    version = session.get(ChapterVersion, version_id)
    if version is None or version.project_id != project_id:
        raise HTTPException(404, detail={"code": "REFERENCE_NOT_FOUND"})
    _active_node(session, project_id, version.chapter_id)
    return version


def _positions(session: Session, project_id: str) -> dict[str, int]:
    return {
        node.id: index
        for index, node in enumerate(ordered_nodes(session))
        if node.project_id == project_id and node.deleted_at is None
    }


def candidate_entity_state_conflict(
    session: Session,
    project_id: str,
    entity: Entity,
    values: dict[str, Any],
) -> dict[str, Any] | None:
    """Return pairwise closed-interval conflicts for a proposed confirmed state."""

    _validate_references_and_range(session, project_id, values)
    positions = _positions(session, project_id)
    candidate_start = positions[values["valid_from_node_id"]]
    candidate_end = (
        positions[values["valid_to_node_id"]]
        if values.get("valid_to_node_id") is not None
        else len(positions)
    )
    conflicts: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    for state in session.scalars(
        select(EntityState)
        .where(
            EntityState.project_id == project_id,
            EntityState.entity_id == entity.id,
            EntityState.status == "confirmed",
        )
        .order_by(EntityState.created_at, EntityState.id)
    ):
        if state.valid_from_node_id not in positions or (
            state.valid_to_node_id is not None
            and state.valid_to_node_id not in positions
        ):
            continue
        existing_start = positions[state.valid_from_node_id]
        existing_end = (
            positions[state.valid_to_node_id]
            if state.valid_to_node_id is not None
            else len(positions)
        )
        if candidate_start > existing_end or existing_start > candidate_end:
            continue
        state_differences = []
        for key in sorted(set(state.data) | set(values["data"])):
            existing_present = key in state.data
            candidate_present = key in values["data"]
            existing_value = state.data.get(key)
            candidate_value = values["data"].get(key)
            if existing_present == candidate_present and existing_value == candidate_value:
                continue
            difference = {
                "key": key,
                "existing_present": existing_present,
                "candidate_present": candidate_present,
                "existing_value": existing_value,
                "candidate_value": candidate_value,
            }
            state_differences.append(difference)
            differences.append(difference)
        if state_differences:
            conflicts.append(
                {
                    "existing_state_id": state.id,
                    "existing_start": state.valid_from_node_id,
                    "existing_end": state.valid_to_node_id,
                    "candidate_start": values["valid_from_node_id"],
                    "candidate_end": values.get("valid_to_node_id"),
                }
            )
    if not conflicts:
        return None
    bounded_conflicts = conflicts[:200]
    return {
        "code": "CANDIDATE_ENTITY_STATE_CONFLICT",
        "entity_id": entity.id,
        "existing_state_ids": [
            item["existing_state_id"] for item in bounded_conflicts
        ],
        "existing_state_count": len(conflicts),
        "truncated": len(conflicts) > len(bounded_conflicts)
        or len(differences) > 200,
        "ranges": bounded_conflicts,
        "differences": differences[:200],
    }


def _validate_references_and_range(
    session: Session, project_id: str, values: dict[str, Any]
) -> None:
    start_id = values.get("valid_from_node_id")
    if not isinstance(start_id, str) or not start_id:
        raise HTTPException(422, detail={"code": "INVALID_ENTITY_STATE"})
    start = _active_node(session, project_id, start_id)
    end_id = values.get("valid_to_node_id")
    end = _active_node(session, project_id, end_id) if end_id else None
    version = _source_version(session, project_id, values.get("source_version_id"))
    positions = _positions(session, project_id)
    if positions[start.id] > positions[end.id] if end else False:
        raise HTTPException(422, detail={"code": "INVALID_STORY_RANGE"})
    if version is not None:
        source_position = positions[version.chapter_id]
        if source_position < positions[start.id] or (
            end is not None and source_position > positions[end.id]
        ):
            raise HTTPException(
                422, detail={"code": "SOURCE_VERSION_OUTSIDE_STATE_RANGE"}
            )


def _validate_transition_shape(values: dict[str, Any]) -> None:
    data = values.get("data")
    if not isinstance(data, dict):
        raise HTTPException(422, detail={"code": "INVALID_ENTITY_STATE"})
    transition = values.get("legacy_transition")
    if transition == "baseline" and not data:
        raise HTTPException(422, detail={"code": "LEGACY_BASELINE_DATA_REQUIRED"})
    if transition == "retire" and data:
        raise HTTPException(422, detail={"code": "LEGACY_RETIRE_DATA_MUST_BE_EMPTY"})


def _has_other_confirmed_history(
    session: Session, entity_id: str, *, excluding_id: str | None = None
) -> bool:
    statement = select(EntityState.id).where(
        EntityState.entity_id == entity_id,
        EntityState.status == "confirmed",
    )
    if excluding_id is not None:
        statement = statement.where(EntityState.id != excluding_id)
    return session.scalar(statement.limit(1)) is not None


def _validate_legacy_gate(
    session: Session,
    entity: Entity,
    values: dict[str, Any],
    *,
    excluding_id: str | None = None,
) -> None:
    _validate_transition_shape(values)
    has_confirmed = _has_other_confirmed_history(
        session, entity.id, excluding_id=excluding_id
    )
    transition = values.get("legacy_transition")
    if transition is not None and (
        not entity.state
        or has_confirmed
        or values.get("status") not in {"pending", "confirmed"}
    ):
        raise HTTPException(422, detail={"code": "INVALID_LEGACY_TRANSITION"})
    if (
        values.get("status") == "confirmed"
        and bool(entity.state)
        and not has_confirmed
        and transition not in {"baseline", "retire"}
    ):
        raise HTTPException(
            409, detail={"code": "LEGACY_STATE_TRANSITION_REQUIRED"}
        )


def create_entity_state(
    session: Session, project_id: str, entity_id: str, values: dict[str, Any]
) -> EntityState:
    _begin_write(session)
    entity = require_entity(session, project_id, entity_id)
    _validate_references_and_range(session, project_id, values)
    _validate_legacy_gate(session, entity, values)
    state = EntityState(project_id=project_id, entity_id=entity_id, **values)
    session.add(state)
    session.flush()
    return state


def _begin_write(session: Session) -> None:
    connection = session.connection().connection.driver_connection
    if not connection.in_transaction:
        session.execute(text("BEGIN IMMEDIATE"))
        session.expire_all()


def _raise_revision_conflict(current: EntityState) -> None:
    raise HTTPException(
        409,
        detail={
            "code": "revision_conflict",
            "current": jsonable_encoder(serialize(current)),
        },
    )


def _validate_edit_lifecycle(current: EntityState, changes: dict[str, Any]) -> None:
    if current.status == "pending":
        return
    if current.status == "confirmed" and changes == {"status": "retracted"}:
        return
    raise HTTPException(409, detail={"code": "ENTITY_STATE_IMMUTABLE"})


def edit_entity_state(
    session: Session,
    project_id: str,
    state_id: str,
    revision: int,
    changes: dict[str, Any],
) -> EntityState:
    _begin_write(session)
    current = require_entity_state(session, project_id, state_id)
    if current.revision != revision:
        _raise_revision_conflict(current)
    _validate_edit_lifecycle(current, changes)
    entity = require_entity(session, project_id, current.entity_id)
    if not (current.status == "confirmed" and changes == {"status": "retracted"}):
        values = {**serialize(current), **changes}
        if values.get("status") not in {"pending", "confirmed", "retracted"}:
            raise HTTPException(422, detail={"code": "INVALID_ENTITY_STATE"})
        _validate_references_and_range(session, project_id, values)
        if values.get("status") == "retracted":
            _validate_transition_shape(values)
        else:
            _validate_legacy_gate(session, entity, values, excluding_id=current.id)
    result = session.execute(
        update(EntityState)
        .where(EntityState.id == state_id, EntityState.revision == revision)
        .values(**changes, revision=revision + 1)
        .execution_options(synchronize_session=False)
    )
    session.expire_all()
    current = require_entity_state(session, project_id, state_id)
    if result.rowcount != 1:
        _raise_revision_conflict(current)
    return current
