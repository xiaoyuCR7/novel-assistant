"""Wiki audit regressions use only temporary project fixtures."""

import pytest

from novel_harness.db.models import Entity, EntityState, StoryNode, TimelineEvent
from novel_harness.services.wiki import expand_aliases, snapshot


def test_alias_false_candidates_do_not_starve_real_match(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.add_all(
            [
                Entity(
                    project_id=project["id"],
                    name=f"A{i:03}",
                    kind="character",
                    profile={"aliases": ["Al"]},
                )
                for i in range(100)
            ]
        )
        session.add(
            Entity(
                project_id=project["id"],
                name="真正的角色",
                kind="character",
                profile={"aliases": ["Alice"]},
            )
        )
        session.flush()
        assert expand_aliases(session, "Alice walks") == "Alice walks 真正的角色"


def test_wiki_mentions_honor_name_boundaries_before_source_limit(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        entity = Entity(
            project_id=project["id"], name="林渡", kind="character", profile={"aliases": ["Al"]}
        )
        session.add(entity)
        session.add_all(
            [
                TimelineEvent(
                    project_id=project["id"], title=f"A{i:03} Alice walks", description="无关人物"
                )
                for i in range(30)
            ]
        )
        wanted = TimelineEvent(project_id=project["id"], title="Z Al walks", description="目标人物")
        session.add(wanted)
        session.flush()
        page = snapshot(session, project["id"], "entity", entity.id)
        assert [source["id"] for source in page["sources"]] == [entity.id, wanted.id]
        assert not page["truncated"]


def test_history_keeps_latest_chronological_rows(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        entity = Entity(project_id=project["id"], name="历史人物", kind="character")
        chapters = [
            StoryNode(project_id=project["id"], kind="chapter", title=f"第{i}章", order_index=i)
            for i in range(102)
        ]
        session.add_all([entity, *chapters])
        session.flush()
        # The latest chapter was imported first; creation time is not narrative order.
        session.add_all(
            [
                EntityState(
                    project_id=project["id"],
                    entity_id=entity.id,
                    valid_from_node_id=chapter.id,
                    valid_to_node_id=chapter.id,
                    data={"location": f"地点{chapter.order_index}"},
                )
                for chapter in [chapters[-1], *chapters[:-1]]
            ]
        )
        session.flush()
        page = snapshot(session, project["id"], "entity", entity.id)
        assert page["truncated"]
        assert page["state_history"][0]["chapter_id"] == chapters[2].id
        assert page["state_history"][-1]["chapter_id"] == chapters[-1].id


@pytest.mark.parametrize("change", ["delete_end", "reverse_range"])
def test_history_excludes_state_with_invalid_range(client, project, change):
    from novel_harness.db.base import utc_now

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        entity = Entity(project_id=project["id"], name="角色", kind="character")
        first = StoryNode(project_id=project["id"], kind="chapter", title="开端", order_index=1)
        deleted = StoryNode(project_id=project["id"], kind="chapter", title="删除章", order_index=2)
        session.add_all([entity, first, deleted])
        session.flush()
        session.add(
            EntityState(
                project_id=project["id"],
                entity_id=entity.id,
                valid_from_node_id=first.id,
                valid_to_node_id=deleted.id,
                data={"secret": "已失效的状态"},
            )
        )
        if change == "delete_end":
            deleted.deleted_at = utc_now()
        else:
            first.order_index = 3
        session.flush()
        page = snapshot(session, project["id"], "entity", entity.id, first.id)
        assert page["state_history"] == []
        assert "已失效的状态" not in page["sources"][0]["content"]


@pytest.mark.parametrize("resume_old", [False, True])
def test_matching_summary_cache_survives_newer_stale_job(client, project, resume_old):
    from novel_harness.services.job_store import JobStore

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        entity = Entity(project_id=project["id"], name="林渡", kind="character")
        first = StoryNode(project_id=project["id"], kind="chapter", title="开端", order_index=1)
        second = StoryNode(project_id=project["id"], kind="chapter", title="后续", order_index=2)
        session.add_all([entity, first, second])
        session.flush()
        entity_id, first_id = entity.id, first.id
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/wiki/entity/{entity_id}"
    store = JobStore(vault.database, project["id"])

    def submit(key):
        response = client.post(
            url + "/summarize",
            json={"fingerprint": client.get(url).json()["fingerprint"]},
            headers={"Idempotency-Key": key},
        )
        assert response.status_code == 202, response.text
        return response.json()

    original = client.get(url).json()["fingerprint"]
    old = submit("cache-first")
    if resume_old:
        fence = store.claim(old["id"], "wiki-audit")
        store.pause(fence, "known-failure", failed=True)
    else:
        assert client.app.state.job_executor.run_once()

    with vault.database.session_scope() as session:
        session.get(StoryNode, first_id).order_index = 3
    newer = submit("cache-second")
    assert newer["id"] != old["id"]
    assert client.app.state.job_executor.run_once()

    with vault.database.session_scope() as session:
        session.get(StoryNode, first_id).order_index = 1
    assert client.get(url).json()["fingerprint"] == original
    if resume_old:
        paused = store.read(old["id"])
        response = client.post(
            base + f"/ai/jobs/{old['id']}/resume",
            json={"expected_control_revision": paused["control_revision"]},
            headers={"Idempotency-Key": "cache-resume"},
        )
        assert response.status_code == 202, response.text
        assert client.app.state.job_executor.run_once()

    page = client.get(url).json()
    assert page["summary"]["job_id"] == old["id"]
    assert page["summary"]["stale"] is False
    assert submit("cache-third")["id"] == old["id"]
    assert not client.app.state.job_executor.run_once()
