import json
import sqlite3
import threading
from contextlib import closing
from queue import Queue
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from job_helpers import finish_job, run_job, save_chapter
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from novel_harness.ai.demo import DemoProvider
from novel_harness.main import create_app
from novel_harness.services.continuity import ContinuityChecker, ContinuityInput


def make_node(client, project_id, title, order_index):
    response = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        json={"kind": "chapter", "title": title, "order_index": order_index},
    )
    assert response.status_code == 201
    return response.json()


def make_entity(client, project_id, *, state=None):
    response = client.post(
        f"/api/v1/projects/{project_id}/entities",
        json={
            "kind": "character",
            "name": "林渡",
            "state": state or {},
        },
    )
    assert response.status_code == 201
    return response.json()


def make_version(client, project_id, chapter_id, content="正文"):
    document = client.get(f"/api/v1/projects/{project_id}/chapters/{chapter_id}").json()
    response = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/versions",
        json={"content": content, "expected_revision": document["revision"]},
    )
    assert response.status_code == 201
    return response.json()


def state_url(project_id, entity_id):
    return f"/api/v1/projects/{project_id}/entities/{entity_id}/states"


def test_create_and_list_confirmed_state_with_inclusive_range_and_source_version(
    client, project
):
    first = make_node(client, project["id"], "第一章", 1)
    last = make_node(client, project["id"], "第二章", 2)
    entity = make_entity(client, project["id"])
    version = make_version(client, project["id"], first["id"])

    response = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True, "location": "旧邮局"},
            "valid_from_node_id": first["id"],
            "valid_to_node_id": last["id"],
            "source_version_id": version["id"],
            "status": "confirmed",
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["project_id"] == project["id"]
    assert created["entity_id"] == entity["id"]
    assert created["valid_from_node_id"] == first["id"]
    assert created["valid_to_node_id"] == last["id"]
    assert created["source_version_id"] == version["id"]
    assert created["status"] == "confirmed"
    assert created["revision"] == 1
    assert client.get(state_url(project["id"], entity["id"])).json() == [created]


def test_reversed_range_is_rejected(client, project):
    first = make_node(client, project["id"], "第一章", 1)
    last = make_node(client, project["id"], "第二章", 2)
    entity = make_entity(client, project["id"])

    response = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": False},
            "valid_from_node_id": last["id"],
            "valid_to_node_id": first["id"],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_STORY_RANGE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid_from_node_id", "missing-node"),
        ("valid_to_node_id", "missing-node"),
        ("source_version_id", "missing-version"),
    ],
)
def test_missing_references_are_not_disclosed(client, project, field, value):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"])
    payload = {"data": {"alive": True}, "valid_from_node_id": chapter["id"], field: value}

    response = client.post(state_url(project["id"], entity["id"]), json=payload)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REFERENCE_NOT_FOUND"


def test_missing_or_foreign_entities_are_not_disclosed(client, project):
    other = client.post("/api/v1/projects", json={"title": "别的项目"}).json()
    foreign = make_entity(client, other["id"])

    for entity_id in ("missing-entity", foreign["id"]):
        response = client.get(state_url(project["id"], entity_id))
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "ENTITY_NOT_FOUND"


@pytest.mark.parametrize("reference_kind", ["node", "version"])
def test_foreign_references_are_not_disclosed(client, project, reference_kind):
    local = make_node(client, project["id"], "本地章", 1)
    entity = make_entity(client, project["id"])
    other = client.post("/api/v1/projects", json={"title": "别的项目"}).json()
    foreign_node = make_node(client, other["id"], "外部章", 1)
    payload = {"data": {"alive": True}, "valid_from_node_id": local["id"]}
    if reference_kind == "node":
        payload["valid_to_node_id"] = foreign_node["id"]
    else:
        payload["source_version_id"] = make_version(
            client, other["id"], foreign_node["id"]
        )["id"]

    response = client.post(state_url(project["id"], entity["id"]), json=payload)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REFERENCE_NOT_FOUND"


def test_source_version_chapter_must_fall_inside_state_range(client, project):
    first = make_node(client, project["id"], "第一章", 1)
    second = make_node(client, project["id"], "第二章", 2)
    entity = make_entity(client, project["id"])
    future_version = make_version(client, project["id"], second["id"])

    response = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True},
            "valid_from_node_id": first["id"],
            "valid_to_node_id": first["id"],
            "source_version_id": future_version["id"],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SOURCE_VERSION_OUTSIDE_STATE_RANGE"


def test_patch_uses_revision_cas_and_returns_current_row(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"])
    created = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True},
            "valid_from_node_id": chapter["id"],
            "status": "pending",
        },
    ).json()
    url = f"/api/v1/projects/{project['id']}/entity-states/{created['id']}"

    updated = client.patch(
        url,
        json={"revision": created["revision"], "data": {"alive": False}},
    )
    stale = client.patch(
        url,
        json={"revision": created["revision"], "status": "retracted"},
    )

    assert updated.status_code == 200
    assert updated.json()["data"] == {"alive": False}
    assert updated.json()["revision"] == 2
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    assert stale.json()["detail"]["current"] == updated.json()


@pytest.mark.parametrize("stale_changes", ["reversed_range", "null_status"])
def test_stale_revision_wins_before_payload_validation(
    client, project, stale_changes
):
    first = make_node(client, project["id"], "第一章", 1)
    last = make_node(client, project["id"], "第二章", 2)
    entity = make_entity(client, project["id"])
    created = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True},
            "valid_from_node_id": first["id"],
            "status": "pending",
        },
    ).json()
    url = f"/api/v1/projects/{project['id']}/entity-states/{created['id']}"
    current = client.patch(
        url,
        json={"revision": created["revision"], "data": {"alive": False}},
    ).json()
    invalid = (
        {
            "valid_from_node_id": last["id"],
            "valid_to_node_id": first["id"],
        }
        if stale_changes == "reversed_range"
        else {"status": None}
    )

    response = client.patch(
        url,
        json={"revision": created["revision"], **invalid},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "revision_conflict"
    assert response.json()["detail"]["current"] == current


def test_first_confirmed_temporal_state_requires_explicit_legacy_baseline(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    url = state_url(project["id"], entity["id"])

    draft = client.post(
        url,
        json={
            "data": {"alive": True},
            "valid_from_node_id": chapter["id"],
            "status": "pending",
        },
    )
    blocked = client.post(
        url,
        json={"data": {"alive": True}, "valid_from_node_id": chapter["id"]},
    )
    baseline = client.post(
        url,
        json={
            "data": {"alive": True},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "baseline",
        },
    )

    assert draft.status_code == 201
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "LEGACY_STATE_TRANSITION_REQUIRED"
    assert baseline.status_code == 201
    assert baseline.json()["legacy_transition"] == "baseline"

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        from novel_harness.db.models import Entity

        assert session.get(Entity, entity["id"]).state == {"alive": True}


@pytest.mark.parametrize(
    ("legacy_transition", "data", "code"),
    [
        ("baseline", {}, "LEGACY_BASELINE_DATA_REQUIRED"),
        ("retire", {"alive": True}, "LEGACY_RETIRE_DATA_MUST_BE_EMPTY"),
    ],
)
def test_legacy_transition_shape_is_enforced(
    client, project, legacy_transition, data, code
):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})

    response = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": data,
            "valid_from_node_id": chapter["id"],
            "legacy_transition": legacy_transition,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code


def test_retire_preserves_legacy_json_and_counts_as_confirmed_history(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    url = state_url(project["id"], entity["id"])

    retired = client.post(
        url,
        json={
            "data": {},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "retire",
        },
    )
    next_state = client.post(
        url,
        json={"data": {"alive": False}, "valid_from_node_id": chapter["id"]},
    )

    assert retired.status_code == 201
    assert next_state.status_code == 201
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        from novel_harness.db.models import Entity

        assert session.get(Entity, entity["id"]).state == {"alive": True}


def test_patch_cannot_bypass_first_confirmed_legacy_transition(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    pending = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": False},
            "valid_from_node_id": chapter["id"],
            "status": "pending",
        },
    ).json()

    response = client.patch(
        f"/api/v1/projects/{project['id']}/entity-states/{pending['id']}",
        json={"revision": pending["revision"], "status": "confirmed"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "LEGACY_STATE_TRANSITION_REQUIRED"


def test_create_acquires_writer_lock_before_reading_legacy_history(
    client, project, monkeypatch
):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    vault = client.app.state.vault_registry.require(project["id"])
    from novel_harness.services import entity_states

    owner = Session(vault.database.engine, expire_on_commit=False)
    contender = Session(vault.database.engine, expire_on_commit=False)
    observed_read = False
    original = entity_states.require_entity

    def observe_read(session, project_id, entity_id):
        nonlocal observed_read
        if session is contender:
            observed_read = True
        return original(session, project_id, entity_id)

    monkeypatch.setattr(entity_states, "require_entity", observe_read)
    try:
        owner.execute(text("BEGIN IMMEDIATE"))
        contender.execute(text("PRAGMA busy_timeout=50"))
        with pytest.raises(OperationalError):
            entity_states.create_entity_state(
                contender,
                project["id"],
                entity["id"],
                {
                    "data": {"alive": True},
                    "valid_from_node_id": chapter["id"],
                    "valid_to_node_id": None,
                    "source_version_id": None,
                    "status": "confirmed",
                    "legacy_transition": "baseline",
                },
            )
        assert observed_read is False
    finally:
        contender.rollback()
        owner.rollback()
        contender.close()
        owner.close()


def test_concurrent_first_confirmed_states_serialize_legacy_gate(
    client, project, monkeypatch
):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    vault = client.app.state.vault_registry.require(project["id"])
    from novel_harness.services import entity_states

    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    gate_count = 0
    gate_guard = threading.Lock()
    outcomes: Queue[str] = Queue()
    original_gate = entity_states._validate_legacy_gate

    def coordinated_gate(*args, **kwargs):
        nonlocal gate_count
        with gate_guard:
            gate_count += 1
            position = gate_count
        if position == 1:
            first_entered.set()
            assert release_first.wait(5)
        else:
            second_entered.set()
        return original_gate(*args, **kwargs)

    monkeypatch.setattr(entity_states, "_validate_legacy_gate", coordinated_gate)

    def create_baseline():
        with Session(vault.database.engine, expire_on_commit=False) as session:
            try:
                entity_states.create_entity_state(
                    session,
                    project["id"],
                    entity["id"],
                    {
                        "data": {"alive": True},
                        "valid_from_node_id": chapter["id"],
                        "valid_to_node_id": None,
                        "source_version_id": None,
                        "status": "confirmed",
                        "legacy_transition": "baseline",
                    },
                )
                session.commit()
                outcomes.put("created")
            except HTTPException as exc:
                session.rollback()
                outcomes.put(exc.detail["code"])

    first = threading.Thread(target=create_baseline)
    second = threading.Thread(target=create_baseline)
    first.start()
    assert first_entered.wait(5)
    second.start()
    serialized = not second_entered.wait(0.2)
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert serialized is True
    assert sorted([outcomes.get_nowait(), outcomes.get_nowait()]) == [
        "INVALID_LEGACY_TRANSITION",
        "created",
    ]


def test_pending_state_can_edit_fields_and_then_confirm(client, project):
    first = make_node(client, project["id"], "第一章", 1)
    second = make_node(client, project["id"], "第二章", 2)
    version = make_version(client, project["id"], second["id"])
    entity = make_entity(client, project["id"], state={"alive": True})
    pending = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True},
            "valid_from_node_id": first["id"],
            "status": "pending",
            "legacy_transition": "baseline",
        },
    ).json()

    response = client.patch(
        f"/api/v1/projects/{project['id']}/entity-states/{pending['id']}",
        json={
            "revision": pending["revision"],
            "data": {"alive": False},
            "valid_from_node_id": second["id"],
            "valid_to_node_id": second["id"],
            "source_version_id": version["id"],
            "legacy_transition": "baseline",
            "status": "confirmed",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"alive": False}
    assert response.json()["status"] == "confirmed"


@pytest.mark.parametrize(
    "change",
    [
        {"data": {"alive": False}},
        {"valid_from_node_id": "other"},
        {"valid_to_node_id": "other"},
        {"source_version_id": "other"},
        {"legacy_transition": "retire"},
        {"status": "pending"},
    ],
)
def test_confirmed_state_fact_and_lifecycle_are_immutable(client, project, change):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    confirmed = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "baseline",
        },
    ).json()

    response = client.patch(
        f"/api/v1/projects/{project['id']}/entity-states/{confirmed['id']}",
        json={"revision": confirmed["revision"], **change},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ENTITY_STATE_IMMUTABLE"


def test_confirmed_state_can_only_retract_and_retracted_state_cannot_change(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"])
    confirmed = client.post(
        state_url(project["id"], entity["id"]),
        json={"data": {"alive": True}, "valid_from_node_id": chapter["id"]},
    ).json()
    url = f"/api/v1/projects/{project['id']}/entity-states/{confirmed['id']}"

    retracted = client.patch(
        url,
        json={"revision": confirmed["revision"], "status": "retracted"},
    )
    blocked = client.patch(
        url,
        json={"revision": retracted.json()["revision"], "status": "retracted"},
    )

    assert retracted.status_code == 200
    assert retracted.json()["status"] == "retracted"
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "ENTITY_STATE_IMMUTABLE"


def test_confirmed_state_can_retract_after_its_source_node_is_deleted(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"])
    confirmed = client.post(
        state_url(project["id"], entity["id"]),
        json={"data": {"alive": True}, "valid_from_node_id": chapter["id"]},
    ).json()
    assert client.delete(
        f"/api/v1/projects/{project['id']}/library/node/{chapter['id']}"
    ).status_code == 200

    response = client.patch(
        f"/api/v1/projects/{project['id']}/entity-states/{confirmed['id']}",
        json={"revision": confirmed["revision"], "status": "retracted"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "retracted"


def test_legacy_transition_is_rejected_without_legacy_state(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"])

    response = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True},
            "valid_from_node_id": chapter["id"],
            "status": "pending",
            "legacy_transition": "baseline",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_LEGACY_TRANSITION"


def test_second_legacy_transition_is_rejected(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    url = state_url(project["id"], entity["id"])
    assert client.post(
        url,
        json={
            "data": {"alive": True},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "baseline",
        },
    ).status_code == 201

    response = client.post(
        url,
        json={
            "data": {},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "retire",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_LEGACY_TRANSITION"


def test_confirmed_legacy_marker_stays_immutable_after_later_history(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    url = state_url(project["id"], entity["id"])
    first = client.post(
        url,
        json={
            "data": {"alive": True},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "baseline",
        },
    ).json()
    assert client.post(
        url,
        json={"data": {"alive": False}, "valid_from_node_id": chapter["id"]},
    ).status_code == 201

    response = client.patch(
        f"/api/v1/projects/{project['id']}/entity-states/{first['id']}",
        json={"revision": first["revision"], "legacy_transition": "retire", "data": {}},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ENTITY_STATE_IMMUTABLE"


def test_pending_confirmation_rechecks_transition_after_history_changes(client, project):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    url = state_url(project["id"], entity["id"])
    pending = client.post(
        url,
        json={
            "data": {"alive": True},
            "valid_from_node_id": chapter["id"],
            "status": "pending",
            "legacy_transition": "baseline",
        },
    ).json()
    assert client.post(
        url,
        json={
            "data": {},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "retire",
        },
    ).status_code == 201

    response = client.patch(
        f"/api/v1/projects/{project['id']}/entity-states/{pending['id']}",
        json={"revision": pending["revision"], "status": "confirmed"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_LEGACY_TRANSITION"


def test_soft_deleted_source_chapter_is_rejected(client, project):
    first = make_node(client, project["id"], "第一章", 1)
    source_chapter = make_node(client, project["id"], "第二章", 2)
    version = make_version(client, project["id"], source_chapter["id"])
    entity = make_entity(client, project["id"])
    assert client.delete(
        f"/api/v1/projects/{project['id']}/library/node/{source_chapter['id']}"
    ).status_code == 200

    response = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True},
            "valid_from_node_id": first["id"],
            "source_version_id": version["id"],
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "REFERENCE_NOT_FOUND"


def test_patch_source_version_revalidates_final_range(client, project):
    first = make_node(client, project["id"], "第一章", 1)
    second = make_node(client, project["id"], "第二章", 2)
    future_version = make_version(client, project["id"], second["id"])
    entity = make_entity(client, project["id"])
    pending = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": True},
            "valid_from_node_id": first["id"],
            "valid_to_node_id": first["id"],
            "status": "pending",
        },
    ).json()

    response = client.patch(
        f"/api/v1/projects/{project['id']}/entity-states/{pending['id']}",
        json={"revision": pending["revision"], "source_version_id": future_version["id"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SOURCE_VERSION_OUTSIDE_STATE_RANGE"


def test_entity_states_table_has_portable_constraints_and_query_indexes(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    table = inspect(vault.database.engine)
    checks = {item["name"] for item in table.get_check_constraints("entity_states")}
    indexes = {tuple(item["column_names"]) for item in table.get_indexes("entity_states")}

    assert {
        "ck_entity_states_status",
        "ck_entity_states_legacy_transition",
        "ck_entity_states_revision",
    } <= checks
    assert ("project_id", "entity_id", "created_at", "id") in indexes
    assert ("entity_id", "status") in indexes


def test_existing_vault_gets_entity_states_table_on_next_open(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NOVEL_AI_PROVIDER", "demo")
    with TestClient(create_app(start_executor=False)) as client:
        project = client.post("/api/v1/projects", json={"title": "老项目"}).json()
        vault_path = client.app.state.vault_registry.require(project["id"]).database.path
    with closing(sqlite3.connect(vault_path)) as db, db:
        db.execute("DROP TABLE entity_states")
        db.commit()
        assert "entity_states" not in {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    with TestClient(create_app(start_executor=False)) as client:
        assert client.get(f"/api/v1/projects/{project['id']}/workspace").status_code == 200
    with closing(sqlite3.connect(vault_path)) as db, db:
        assert "entity_states" in {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def _resolve(client, project_id, entity_id, chapter_id):
    vault = client.app.state.vault_registry.require(project_id)
    with vault.database.session_scope() as session:
        from novel_harness.services.entity_states import resolve_entity_state

        return resolve_entity_state(session, entity_id, chapter_id)


def test_resolver_uses_inclusive_temporal_ranges_without_leaking_late_death(
    client, project
):
    early = make_node(client, project["id"], "第五章", 5)
    late = make_node(client, project["id"], "第二十章", 20)
    entity = make_entity(client, project["id"], state={"alive": True})
    url = state_url(project["id"], entity["id"])
    assert client.post(
        url,
        json={
            "data": {"alive": True},
            "valid_from_node_id": early["id"],
            "valid_to_node_id": early["id"],
            "legacy_transition": "baseline",
        },
    ).status_code == 201
    assert client.post(
        url,
        json={"data": {"alive": False}, "valid_from_node_id": late["id"]},
    ).status_code == 201

    early_state = _resolve(client, project["id"], entity["id"], early["id"])
    late_state = _resolve(client, project["id"], entity["id"], late["id"])

    assert early_state.data == {"alive": True}
    assert late_state.data == {"alive": False}
    assert early_state.conflicts == late_state.conflicts == []


def test_resolver_retire_blocks_legacy_but_retracted_history_restores_fallback(
    client, project
):
    chapter = make_node(client, project["id"], "第一章", 1)
    entity = make_entity(client, project["id"], state={"alive": True})
    url = state_url(project["id"], entity["id"])
    retired = client.post(
        url,
        json={
            "data": {},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "retire",
        },
    ).json()

    assert _resolve(client, project["id"], entity["id"], chapter["id"]).data == {}
    response = client.patch(
        f"/api/v1/projects/{project['id']}/entity-states/{retired['id']}",
        json={"revision": retired["revision"], "status": "retracted"},
    )
    assert response.status_code == 200
    assert _resolve(client, project["id"], entity["id"], chapter["id"]).data == {
        "alive": True
    }


def test_resolver_overlays_different_keys_and_reports_disputed_key_without_last_write(
    client, project
):
    first = make_node(client, project["id"], "第一章", 1)
    second = make_node(client, project["id"], "第二章", 2)
    entity = make_entity(client, project["id"])
    url = state_url(project["id"], entity["id"])
    rows = [
        client.post(
            url,
            json={"data": data, "valid_from_node_id": start["id"]},
        ).json()
        for data, start in (
            ({"alive": True, "location": "旧邮局"}, first),
            ({"inventory": ["钥匙"], "alive": True}, second),
            ({"alive": False}, second),
        )
    ]

    resolved = _resolve(client, project["id"], entity["id"], second["id"])

    assert resolved.data == {
        "alive": True,
        "location": "旧邮局",
        "inventory": ["钥匙"],
    }
    assert resolved.conflicts == [
        {
            "code": "ENTITY_STATE_CONFLICT",
            "entity_id": entity["id"],
            "key": "alive",
            "state_ids": [rows[0]["id"], rows[2]["id"]],
            "values": [True, False],
        }
    ]


def test_chapter_serialization_drives_early_and_late_continuity(client, project):
    early = make_node(client, project["id"], "第五章", 5)
    late = make_node(client, project["id"], "第二十章", 20)
    entity = make_entity(client, project["id"], state={"alive": True})
    url = state_url(project["id"], entity["id"])
    client.post(
        url,
        json={
            "data": {"alive": True},
            "valid_from_node_id": early["id"],
            "valid_to_node_id": early["id"],
            "legacy_transition": "baseline",
        },
    )
    client.post(
        url,
        json={"data": {"alive": False}, "valid_from_node_id": late["id"]},
    )
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        from novel_harness.db.models import Entity
        from novel_harness.services.entity_states import serialize_entity_for_chapter

        row = session.get(Entity, entity["id"])
        early_entity = serialize_entity_for_chapter(session, row, early["id"])
        late_entity = serialize_entity_for_chapter(session, row, late["id"])

    def codes(serialized):
        return {
            finding.code
            for finding in ContinuityChecker().check(
                ContinuityInput(
                    draft="林渡推门而入。",
                    chapter_id=early["id"],
                    entities=[serialized],
                    acting_entity_ids=[entity["id"]],
                )
            )
        }

    assert "CHARACTER_STATE" not in codes(early_entity)
    assert "CHARACTER_STATE" in codes(late_entity)


def test_required_and_explicit_entity_context_use_chapter_scoped_state(client, project):
    chapter = make_node(client, project["id"], "第二十章", 20)
    entity = make_entity(client, project["id"], state={"alive": True})
    state = client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": False},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "baseline",
        },
    )
    assert state.status_code == 201
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        from novel_harness.db.models import Project
        from novel_harness.services.writing_context import collect_task_hard_context

        fragments, _, _ = collect_task_hard_context(
            session,
            session.get(Project, project["id"]),
            chapter["id"],
            {"required_entity_ids": [entity["id"]]},
            "chat",
            f"[[ref:entity:{entity['id']}]]",
        )

    matches = [fragment for fragment in fragments if fragment.source_id == entity["id"]]
    required = next(item for item in matches if item.source_type == "story_entity")
    explicit = next(item for item in matches if item.source_type == "entity")
    assert json.loads(required.content)["state"] == {"alive": False}
    assert '"alive": false' in explicit.content
    assert '"alive": true' not in explicit.content


def test_pipeline_continuity_uses_chapter_scoped_state(client, project):
    chapter = make_node(client, project["id"], "第二十章", 20)
    entity = make_entity(client, project["id"])
    assert client.post(
        state_url(project["id"], entity["id"]),
        json={"data": {"alive": False}, "valid_from_node_id": chapter["id"]},
    ).status_code == 201
    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
    assert save_chapter(client, chapter_url, json={"content": "林渡推门而入。"}).status_code == 200

    class ActingProvider(DemoProvider):
        def generate_structured(self, request, schema):
            result = super().generate_structured(request, schema)
            if request.task == "review":
                result.data["observations"] = [
                    {
                        "kind": "action",
                        "evidence": "林渡推门而入",
                        "entity_id": entity["id"],
                        "predicate": "",
                        "value": None,
                    }
                ]
            return result

    client.app.state.ai_provider = ActingProvider()
    result = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "review",
        },
    )

    assert result.status_code == 200
    assert result.json()["status"] == "succeeded"
    conflicts = client.get(f"/api/v1/projects/{project['id']}/conflicts").json()
    assert any(item["code"] == "CHARACTER_STATE" for item in conflicts)


def test_state_conflict_is_persisted_and_blocks_provider_before_first_call(client, project):
    calls = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            calls.append(request.task)
            return super().generate_text(request)

        def generate_structured(self, request, schema):
            calls.append(request.task)
            return super().generate_structured(request, schema)

    chapter = make_node(client, project["id"], "冲突章", 1)
    entity = make_entity(client, project["id"])
    url = state_url(project["id"], entity["id"])
    for alive in (True, False):
        assert client.post(
            url,
            json={"data": {"alive": alive}, "valid_from_node_id": chapter["id"]},
        ).status_code == 201
    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
    assert save_chapter(
        client,
        chapter_url,
        json={"content": "", "contract": {"required_entity_ids": [entity["id"]]}},
    ).status_code == 200
    client.app.state.ai_provider = Recorder()

    response = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
        },
    )

    assert calls == []
    assert response.status_code == 200
    assert response.json()["status"] == "recovery_required"
    assert response.json()["error_code"] == "ENTITY_STATE_CONFLICT"
    conflicts = client.get(f"/api/v1/projects/{project['id']}/conflicts").json()
    stored = [item for item in conflicts if item["code"] == "ENTITY_STATE_CONFLICT"]
    assert len(stored) == 1
    assert stored[0]["related_entity_ids"] == [entity["id"]]
    assert stored[0]["options"]

    replay = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
        },
    )
    assert replay.json()["status"] == "recovery_required"
    assert calls == []
    replayed = client.get(f"/api/v1/projects/{project['id']}/conflicts").json()
    assert sum(item["code"] == "ENTITY_STATE_CONFLICT" for item in replayed) == 1


def test_persisted_state_conflict_requires_retracting_a_state_before_it_closes(
    client, project
):
    chapter = make_node(client, project["id"], "冲突修复章", 1)
    entity = make_entity(client, project["id"])
    states = [
        client.post(
            state_url(project["id"], entity["id"]),
            json={"data": {"alive": alive}, "valid_from_node_id": chapter["id"]},
        ).json()
        for alive in (True, False)
    ]
    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
    assert save_chapter(
        client,
        chapter_url,
        json={"content": "", "contract": {"required_entity_ids": [entity["id"]]}},
    ).status_code == 200
    blocked = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
        },
    )
    assert blocked.json()["error_code"] == "ENTITY_STATE_CONFLICT"
    conflict = next(
        item
        for item in client.get(
            f"/api/v1/projects/{project['id']}/conflicts"
        ).json()
        if item["code"] == "ENTITY_STATE_CONFLICT"
    )
    assert {item["id"] for item in conflict["entity_state_resolution"]["states"]} == {
        state["id"] for state in states
    }

    false_close = client.post(
        f"/api/v1/projects/{project['id']}/conflicts/{conflict['id']}/decide",
        json={"option_id": conflict["options"][0]["id"], "note": "只关闭告警"},
    )
    assert false_close.status_code == 409
    assert false_close.json()["detail"]["code"] == "ENTITY_STATE_CONFLICT_UNRESOLVED"

    resolved = client.post(
        f"/api/v1/projects/{project['id']}/conflicts/{conflict['id']}/resolve-entity-state",
        json={
            "state_id": states[1]["id"],
            "revision": states[1]["revision"],
            "note": "保留存活状态",
        },
    )

    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolved"] is True
    assert resolved.json()["status"] == "decided"
    assert resolved.json()["entity_state_resolution"]["conflicts"] == []
    stored_states = client.get(state_url(project["id"], entity["id"])).json()
    retracted = next(
        item for item in stored_states if item["id"] == states[1]["id"]
    )
    assert retracted["status"] == "retracted"

    replay = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
        },
    )
    assert replay.json()["status"] != "recovery_required"


def test_state_conflict_stays_open_when_retracting_the_origin_reveals_another_pair(
    client, project
):
    chapter = make_node(client, project["id"], "三值冲突修复章", 1)
    entity = make_entity(client, project["id"])
    states = [
        client.post(
            state_url(project["id"], entity["id"]),
            json={"data": {"mood": mood}, "valid_from_node_id": chapter["id"]},
        ).json()
        for mood in ("A", "B", "C")
    ]
    blocked = run_job(
        client,
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
        },
    )
    assert blocked.json()["error_code"] == "ENTITY_STATE_CONFLICT"
    conflict = next(
        item
        for item in client.get(
            f"/api/v1/projects/{project['id']}/conflicts"
        ).json()
        if item["code"] == "ENTITY_STATE_CONFLICT"
    )
    resolution_url = (
        f"/api/v1/projects/{project['id']}/conflicts/{conflict['id']}"
        "/resolve-entity-state"
    )

    first = client.post(
        resolution_url,
        json={"state_id": states[0]["id"], "revision": 1},
    )

    assert first.status_code == 200, first.text
    assert first.json()["resolved"] is False
    assert first.json()["status"] == "open"
    assert {
        item["id"] for item in first.json()["entity_state_resolution"]["states"]
    } == {states[1]["id"], states[2]["id"]}

    second = client.post(
        resolution_url,
        json={"state_id": states[2]["id"], "revision": 1},
    )
    assert second.status_code == 200, second.text
    assert second.json()["resolved"] is True
    assert second.json()["status"] == "decided"


def test_retrieved_entity_and_context_snapshot_use_resolved_state(client, project):
    chapter = make_node(client, project["id"], "第二十章", 20)
    response = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={
            "kind": "character",
            "name": "林渡",
            "summary": "铜钥匙线索只属于这个人物",
            "state": {"alive": True},
        },
    )
    entity = response.json()
    assert client.post(
        state_url(project["id"], entity["id"]),
        json={
            "data": {"alive": False},
            "valid_from_node_id": chapter["id"],
            "legacy_transition": "baseline",
        },
    ).status_code == 201
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        from novel_harness.db.models import Project
        from novel_harness.services.pipeline import CreationPipeline

        snapshot = CreationPipeline(DemoProvider()).build_snapshot(
            session,
            session.get(Project, project["id"]),
            chapter["id"],
            {},
            12_000,
            "chat",
            "铜钥匙线索",
            1,
        )

    retrieved = next(
        item
        for item in snapshot["fragments"]
        if item["source_type"] == "story_entity" and item["source_id"] == entity["id"]
    )
    assert '"alive": false' in retrieved["content"]
    assert '"alive": true' not in retrieved["content"]


def test_preflight_rejects_nonrequired_project_entity_conflict_without_writes_or_provider(
    client, project
):
    calls = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            calls.append(request.task)
            return super().generate_text(request)

    chapter = make_node(client, project["id"], "预检章", 1)
    entity = make_entity(client, project["id"])
    for alive in (True, False):
        assert client.post(
            state_url(project["id"], entity["id"]),
            json={"data": {"alive": alive}, "valid_from_node_id": chapter["id"]},
        ).status_code == 201
    client.app.state.ai_provider = Recorder()
    revision = client.get(
        f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
    ).json()["revision"]

    response = client.post(
        f"/api/v1/projects/{project['id']}/ai/jobs/preflight",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
            "expected_revision": revision,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ENTITY_STATE_CONFLICT"
    assert response.json()["detail"]["conflicts"]
    assert calls == []
    assert client.get(f"/api/v1/projects/{project['id']}/conflicts").json() == []


def test_ready_checkpoint_rechecks_new_state_conflict_before_resume_provider_call(
    client, project
):
    settings = client.app.state.model_settings
    original_budget = settings.default["output_token_budget"]
    calls = []

    class InterruptedProvider:
        attempt_observer = None

        def generate_text(self, request):
            self.attempt_observer.before_send()
            calls.append("interrupted")
            settings.default["output_token_budget"] += 1
            self.attempt_observer.preview("private partial")
            raise AssertionError("preview guard should interrupt first attempt")

    class CountedDemo(DemoProvider):
        def generate_text(self, request):
            calls.append("resumed")
            return super().generate_text(request)

    chapter = make_node(client, project["id"], "恢复章", 1)
    client.app.state.ai_provider = InterruptedProvider()
    receipt = client.post(
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
            "expected_revision": 1,
        },
        headers={"Idempotency-Key": "temporal-ready-interrupt"},
    )
    paused = finish_job(client, receipt).json()
    assert paused["status"] == "recovery_required"
    assert calls == ["interrupted"]
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.job_session_scope() as session:
        from novel_harness.db.job_models import AIJobControl

        assert session.get(AIJobControl, paused["id"]).context_ready is True

    entity = make_entity(client, project["id"])
    for alive in (True, False):
        assert client.post(
            state_url(project["id"], entity["id"]),
            json={"data": {"alive": alive}, "valid_from_node_id": chapter["id"]},
        ).status_code == 201
    settings.default["output_token_budget"] = original_budget
    client.app.state.ai_provider = CountedDemo()
    resumed = client.post(
        paused["status_url"] + "/resume",
        json={
            "expected_control_revision": paused["control_revision"],
            "confirm_unknown": True,
        },
        headers={"Idempotency-Key": "temporal-ready-resume"},
    )
    result = finish_job(client, resumed).json()

    assert result["status"] == "recovery_required"
    assert result["error_code"] == "ENTITY_STATE_CONFLICT"
    assert calls == ["interrupted"]
    conflicts = client.get(f"/api/v1/projects/{project['id']}/conflicts").json()
    assert sum(item["code"] == "ENTITY_STATE_CONFLICT" for item in conflicts) == 1


def test_stale_source_wins_error_priority_but_state_conflict_evidence_is_persisted(
    client, project
):
    calls = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            calls.append(request.task)
            return super().generate_text(request)

    chapter = make_node(client, project["id"], "双重失效章", 1)
    entity = make_entity(client, project["id"])
    client.app.state.ai_provider = Recorder()
    receipt = client.post(
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
            "expected_revision": 1,
        },
        headers={"Idempotency-Key": "stale-and-state-conflict"},
    )
    assert receipt.status_code == 202
    chapter_url = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
    assert save_chapter(client, chapter_url, json={"content": "作者已修改正文"}).status_code == 200
    for alive in (True, False):
        assert client.post(
            state_url(project["id"], entity["id"]),
            json={"data": {"alive": alive}, "valid_from_node_id": chapter["id"]},
        ).status_code == 201

    result = finish_job(client, receipt).json()

    assert calls == []
    assert result["status"] == "recovery_required"
    assert result["error_code"] == "SOURCE_CHANGED"
    conflicts = client.get(f"/api/v1/projects/{project['id']}/conflicts").json()
    stored = [item for item in conflicts if item["code"] == "ENTITY_STATE_CONFLICT"]
    assert len(stored) == 1
    assert stored[0]["related_entity_ids"] == [entity["id"]]
    assert stored[0]["options"]


@pytest.mark.parametrize("reference_path", ["explicit", "search"])
def test_scoped_entity_manifest_invalidates_ready_context_after_new_state(
    client, project, reference_path
):
    settings = client.app.state.model_settings
    original_budget = settings.default["output_token_budget"]
    calls = []

    class InterruptedProvider:
        attempt_observer = None

        def generate_text(self, request):
            self.attempt_observer.before_send()
            calls.append("interrupted")
            settings.default["output_token_budget"] += 1
            self.attempt_observer.preview("private partial")
            raise AssertionError("preview guard should interrupt")

    chapter = make_node(client, project["id"], "清单章", 1)
    response = client.post(
        f"/api/v1/projects/{project['id']}/entities",
        json={
            "kind": "character",
            "name": "清单人物",
            "summary": "唯一清单检索词",
        },
    )
    entity = response.json()
    instructions = (
        f"[[ref:entity:{entity['id']}]]"
        if reference_path == "explicit"
        else "唯一清单检索词"
    )
    client.app.state.ai_provider = InterruptedProvider()
    receipt = client.post(
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
            "instructions": instructions,
            "expected_revision": 1,
        },
        headers={"Idempotency-Key": f"scoped-manifest-{reference_path}"},
    )
    paused = finish_job(client, receipt).json()
    assert paused["status"] == "recovery_required"
    assert calls == ["interrupted"]
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.job_session_scope() as session:
        from novel_harness.db.job_models import AIJobControl

        manifest = session.get(AIJobControl, paused["id"]).source_snapshot["manifest"]
    entry = next(
        item
        for item in manifest
        if item["type"] == "entity" and item["id"] == entity["id"]
    )
    assert entry["entity_state_chapter_id"] == chapter["id"]

    assert client.post(
        state_url(project["id"], entity["id"]),
        json={"data": {"new_key": "new-value"}, "valid_from_node_id": chapter["id"]},
    ).status_code == 201
    settings.default["output_token_budget"] = original_budget
    client.app.state.ai_provider = DemoProvider()
    resumed = client.post(
        paused["status_url"] + "/resume",
        json={
            "expected_control_revision": paused["control_revision"],
            "confirm_unknown": True,
        },
        headers={"Idempotency-Key": f"scoped-manifest-resume-{reference_path}"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["detail"]["code"] == "SOURCE_CHANGED"
    assert calls == ["interrupted"]


def test_legacy_entity_manifest_without_scope_marker_keeps_raw_hash_semantics(
    client, project
):
    chapter = make_node(client, project["id"], "旧清单章", 1)
    entity = make_entity(client, project["id"])
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.job_session_scope() as session:
        from novel_harness.services.job_context import capture_manifest, capture_source

        source = capture_source(session, project["id"], chapter["id"])
        capture_manifest(
            session,
            source,
            {"fragments": [{"citation": {"type": "entity", "id": entity["id"]}}]},
        )
    entry = next(item for item in source["manifest"] if item["type"] == "entity")
    assert "entity_state_chapter_id" not in entry
    assert client.post(
        state_url(project["id"], entity["id"]),
        json={"data": {"new_key": "new-value"}, "valid_from_node_id": chapter["id"]},
    ).status_code == 201

    with vault.database.job_session_scope() as session:
        from novel_harness.services.job_context import assert_source

        assert_source(session, source)


def test_project_conflict_scan_has_constant_query_count(client, project):
    chapter = make_node(client, project["id"], "查询计数章", 1)
    vault = client.app.state.vault_registry.require(project["id"])

    def add_entities(amount):
        for _ in range(amount):
            entity = make_entity(client, project["id"])
            assert client.post(
                state_url(project["id"], entity["id"]),
                json={"data": {"alive": True}, "valid_from_node_id": chapter["id"]},
            ).status_code == 201

    def count_scan_queries():
        statements = []

        def count_select(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(vault.database.engine, "before_cursor_execute", count_select)
        try:
            with vault.database.job_session_scope() as session:
                from novel_harness.services.entity_states import (
                    project_entity_state_conflicts,
                )

                assert project_entity_state_conflicts(
                    session, project["id"], chapter["id"]
                ) == []
        finally:
            event.remove(vault.database.engine, "before_cursor_execute", count_select)
        return len(statements)

    add_entities(1)
    small = count_scan_queries()
    add_entities(11)
    large = count_scan_queries()

    assert small == large == 3


def test_single_entity_resolver_rejects_chapter_from_another_project(client, project):
    local_chapter = make_node(client, project["id"], "本地章", 1)
    entity = make_entity(client, project["id"])
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.job_session_scope() as session:
        from novel_harness.db.models import Entity, Project, StoryNode
        from novel_harness.services.entity_states import resolve_entity_state

        foreign = Project(title="同库外部项目")
        session.add(foreign)
        session.flush()
        foreign_chapter = StoryNode(
            project_id=foreign.id,
            kind="chapter",
            title="外部章",
            order_index=1,
        )
        session.add(foreign_chapter)
        session.flush()
        row = session.get(Entity, entity["id"])
        assert resolve_entity_state(session, row, local_chapter["id"]).data == {}
        with pytest.raises(HTTPException) as caught:
            resolve_entity_state(session, row, foreign_chapter.id)

    assert caught.value.status_code == 404
    assert caught.value.detail["code"] == "REFERENCE_NOT_FOUND"


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [("provider", "PROVIDER_CHANGED"), ("embedding", "EMBEDDING_CHANGED")],
)
def test_validation_error_keeps_priority_after_persisting_state_conflict(
    client, project, change, expected_code
):
    calls = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            calls.append(request.task)
            return super().generate_text(request)

    chapter = make_node(client, project["id"], "验证组合章", 1)
    entity = make_entity(client, project["id"])
    client.app.state.ai_provider = Recorder()
    receipt = client.post(
        f"/api/v1/projects/{project['id']}/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": chapter["id"],
            "task_type": "chat",
            "expected_revision": 1,
        },
        headers={"Idempotency-Key": f"validation-plus-state-{change}"},
    )
    for alive in (True, False):
        assert client.post(
            state_url(project["id"], entity["id"]),
            json={"data": {"alive": alive}, "valid_from_node_id": chapter["id"]},
        ).status_code == 201
    if change == "provider":
        client.app.state.model_settings.default["output_token_budget"] += 1
    else:
        client.app.state.embedding_provider = SimpleNamespace(
            endpoint="http://changed.invalid", model="changed"
        )

    result = finish_job(client, receipt).json()

    assert calls == []
    assert result["status"] == "recovery_required"
    assert result["error_code"] == expected_code
    conflicts = client.get(f"/api/v1/projects/{project['id']}/conflicts").json()
    assert sum(item["code"] == "ENTITY_STATE_CONFLICT" for item in conflicts) == 1


def test_three_conflicting_values_keep_first_and_stable_pairwise_evidence(client, project):
    chapter = make_node(client, project["id"], "三值章", 1)
    entity = make_entity(client, project["id"])
    rows = [
        client.post(
            state_url(project["id"], entity["id"]),
            json={"data": {"mood": value}, "valid_from_node_id": chapter["id"]},
        ).json()
        for value in ("A", "B", "C")
    ]

    resolved = _resolve(client, project["id"], entity["id"], chapter["id"])

    assert resolved.data["mood"] == "A"
    assert resolved.conflicts == [
        {
            "code": "ENTITY_STATE_CONFLICT",
            "entity_id": entity["id"],
            "key": "mood",
            "state_ids": [rows[0]["id"], rows[1]["id"]],
            "values": ["A", "B"],
        },
        {
            "code": "ENTITY_STATE_CONFLICT",
            "entity_id": entity["id"],
            "key": "mood",
            "state_ids": [rows[0]["id"], rows[2]["id"]],
            "values": ["A", "C"],
        },
    ]
