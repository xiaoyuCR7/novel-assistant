from novel_harness.db.models import CanonFact, Entity, StoryNode, TimelineEvent


def seed(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as s:
        first = StoryNode(project_id=project["id"], kind="chapter", title="初见", order_index=1)
        later = StoryNode(project_id=project["id"], kind="chapter", title="结局", order_index=2)
        entity = Entity(
            project_id=project["id"],
            kind="character",
            name="林渡",
            summary="旧城邮差",
            profile={"aliases": ["灰鸦"]},
        )
        s.add_all([first, later, entity])
        s.flush()
        fact = CanonFact(
            project_id=project["id"], subject_entity_id=entity.id, predicate="职业", value="邮差"
        )
        event = TimelineEvent(
            project_id=project["id"], title="林渡离城", description="前往北方", chapter_id=later.id
        )
        s.add_all([fact, event])
        s.flush()
        return entity.id, first.id, later.id, fact.id, event.id


def test_wiki_alias_links_scope_and_deletion(client, project):
    entity, first, later, fact, event = seed(client, project)
    base = f"/api/v1/projects/{project['id']}"
    index = client.get(base + "/wiki", params={"q": "灰鸦"}).json()
    assert index["items"][0]["id"] == entity
    page = client.get(base + f"/wiki/entity/{entity}").json()
    assert fact in [i["id"] for i in page["sources"]]
    assert event in [i["id"] for i in page["sources"]]
    scoped = client.get(base + f"/wiki/entity/{entity}", params={"chapter_id": first}).json()
    assert event not in [i["id"] for i in scoped["sources"]]
    assert (
        client.get(base + f"/wiki/timeline/{event}", params={"chapter_id": first}).status_code
        == 404
    )
    client.delete(base + f"/library/canon/{fact}")
    changed = client.get(base + f"/wiki/entity/{entity}").json()
    assert changed["fingerprint"] != page["fingerprint"]
    assert fact not in [i["id"] for i in changed["sources"]]


def test_wiki_summary_cache_and_staleness(client, project):
    entity, *_ = seed(client, project)
    base = f"/api/v1/projects/{project['id']}/wiki/entity/{entity}"
    page = client.get(base).json()
    headers = {"Idempotency-Key": "wiki-test"}
    payload = {"fingerprint": page["fingerprint"]}
    response = client.post(base + "/summarize", json=payload, headers=headers)
    assert response.status_code == 202, response.text
    job = response.json()
    assert client.post(base + "/summarize", json=payload, headers=headers).json()["id"] == job["id"]
    assert client.app.state.job_executor.run_once()
    page = client.get(base).json()
    assert page["summary"]["claims"]
    assert page["summary"]["stale"] is False
    assert (
        client.post(
            base + "/summarize", json=payload, headers={"Idempotency-Key": "another"}
        ).json()["id"]
        == job["id"]
    )
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as s:
        row = s.get(Entity, entity)
        row.summary = "作者新设定"
        row.revision += 1
    page = client.get(base).json()
    assert page["summary"]["stale"] is True
    assert (
        client.post(
            base + "/summarize", json=payload, headers={"Idempotency-Key": "new"}
        ).status_code
        == 409
    )


def test_wiki_project_isolation(client, project):
    entity, *_ = seed(client, project)
    other = client.post("/api/v1/projects", json={"title": "另一部小说"}).json()
    assert client.get(f"/api/v1/projects/{other['id']}/wiki/entity/{entity}").status_code == 404
    assert client.get(f"/api/v1/projects/{other['id']}/wiki").json()["items"] == []


def test_alias_expansion_improves_shared_search_without_model(client, project):
    entity, _, _, fact, _ = seed(client, project)
    base = f"/api/v1/projects/{project['id']}"
    from novel_harness.db.models import Idea
    from novel_harness.services.retrieval import search
    from novel_harness.services.wiki import expand_aliases

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as s:
        note = Idea(project_id=project["id"], title="林渡的路线", content="经过南门")
        s.add(note)
        s.flush()
        assert "林渡" in expand_aliases(s, "灰鸦走哪条路线")
        assert note.id in [i["id"] for i in search(s, "灰鸦")["items"]]
    assert client.get(base + "/library/search", params={"q": "灰鸦"}).status_code == 200


def test_scoped_state_history_and_plot_payoff(client, project):
    from novel_harness.db.models import EntityState, PlotThread

    entity, first, later, _, _ = seed(client, project)
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as s:
        s.get(Entity, entity).state = {"location": "未来秘密地点"}
        s.add_all(
            [
                EntityState(
                    project_id=project["id"],
                    entity_id=entity,
                    data={"location": "旧城"},
                    valid_from_node_id=first,
                    valid_to_node_id=first,
                ),
                EntityState(
                    project_id=project["id"],
                    entity_id=entity,
                    data={"location": "未来秘密地点"},
                    valid_from_node_id=later,
                ),
                EntityState(
                    project_id=project["id"],
                    entity_id=entity,
                    data={"unconfirmed": "未确认秘密"},
                    valid_from_node_id=first,
                    status="pending",
                ),
            ]
        )
        plot = PlotThread(
            project_id=project["id"],
            kind="foreshadowing",
            title="林渡的旧信",
            promise="寻找信件",
            start_node_id=first,
            due_node_id=later,
            payoff="终局凶手秘密",
            status="resolved",
        )
        s.add(plot)
        s.flush()
        plot_id = plot.id
    base = f"/api/v1/projects/{project['id']}/wiki"
    page = client.get(base + f"/entity/{entity}", params={"chapter_id": first})
    assert "未来秘密地点" not in page.text and "未确认秘密" not in page.text
    assert len(page.json()["state_history"]) == 1
    plot = client.get(base + f"/plot/{plot_id}", params={"chapter_id": first})
    assert "终局凶手秘密" not in plot.text and "resolved" not in plot.text
    index = client.get(base, params={"chapter_id": first})
    assert "终局凶手秘密" not in index.text


def test_generation_does_not_send_changed_sources_or_enter_chat(client, project):
    entity, *_ = seed(client, project)
    base = f"/api/v1/projects/{project['id']}"
    page = client.get(base + f"/wiki/entity/{entity}").json()
    job = client.post(
        base + f"/wiki/entity/{entity}/summarize",
        json={"fingerprint": page["fingerprint"]},
        headers={"Idempotency-Key": "stale-before-send"},
    ).json()
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as s:
        s.get(Entity, entity).summary = "改了设定"
        s.get(Entity, entity).revision += 1
    client.app.state.job_executor.run_once()
    result = client.get(base + "/ai/jobs/" + job["id"]).json()
    assert result["status"] == "recovery_required"
    assert result["error_code"] == "WIKI_SOURCE_CHANGED"
    assert client.get(base + "/ai/jobs").json() == []
    assert client.get(base + "/ai/jobs/page").json()["items"] == []


def test_unknown_result_requires_confirmation_after_cancellation(client, project):
    from novel_harness.services.job_store import JobStore

    entity, *_ = seed(client, project)
    base = f"/api/v1/projects/{project['id']}/wiki/entity/{entity}"
    payload = {"fingerprint": client.get(base).json()["fingerprint"]}
    job = client.post(
        base + "/summarize", json=payload, headers={"Idempotency-Key": "unknown-first"}
    ).json()
    vault = client.app.state.vault_registry.require(project["id"])
    store = JobStore(vault.database, project["id"])
    fence = store.claim(job["id"], "wiki-test-epoch")
    store.begin_attempt(fence, "wiki_summary", {})
    store.dispatch(fence, "wiki_summary", 1)
    store.pause(fence, "result_unknown")
    store.cancel(job["id"])
    blocked = client.post(
        base + "/summarize", json=payload, headers={"Idempotency-Key": "unknown-next"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"
    allowed = client.post(
        base + "/summarize",
        json={**payload, "confirm_unknown": True},
        headers={"Idempotency-Key": "unknown-approved"},
    )
    assert allowed.status_code == 202


def test_summary_citation_validation_and_no_model_when_browsing(client, project, monkeypatch):
    import pytest

    from novel_harness.services.wiki_generation import validate_result

    with pytest.raises(ValueError):
        validate_result(
            {
                "data": {"claims": [{"text": "捏造", "source_ids": ["entity:missing"]}]},
                "provider": "demo",
                "model": "demo",
            },
            set(),
        )
    entity, *_ = seed(client, project)

    def forbidden(*args, **kwargs):
        raise AssertionError("Browsing called the LLM")

    monkeypatch.setattr(client.app.state.ai_provider, "generate_structured", forbidden)
    assert client.get(f"/api/v1/projects/{project['id']}/wiki/entity/{entity}").status_code == 200


def test_reused_submission_receipt_survives_cancellation(client, project):
    entity, *_ = seed(client, project)
    base = f"/api/v1/projects/{project['id']}/wiki/entity/{entity}"
    payload = {"fingerprint": client.get(base).json()["fingerprint"]}

    def submit(key, data=payload):
        return client.post(base + "/summarize", json=data, headers={"Idempotency-Key": key})

    first = submit("receipt-first").json()
    assert submit("receipt-alias").json()["id"] == first["id"]
    client.post(f"/api/v1/projects/{project['id']}/ai/jobs/{first['id']}/cancel")
    assert submit("receipt-alias").json()["id"] == first["id"]
    assert submit("receipt-alias", {**payload, "confirm_unknown": True}).status_code == 409


def test_empty_entity_is_still_a_citable_page(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as s:
        entity = Entity(project_id=project["id"], kind="", name="未命名角色")
        s.add(entity)
        s.flush()
        identity = entity.id
    base = f"/api/v1/projects/{project['id']}/wiki/entity/{identity}"
    page = client.get(base).json()
    assert len(page["sources"]) == 1
    response = client.post(
        base + "/summarize",
        json={"fingerprint": page["fingerprint"]},
        headers={"Idempotency-Key": "empty-wiki"},
    )
    assert response.status_code == 202
    assert client.app.state.job_executor.run_once()
    assert client.get(base).json()["summary"] is not None


def test_old_failed_wiki_cannot_resume_alongside_new_task(client, project):
    from novel_harness.services.job_store import JobStore

    entity, *_ = seed(client, project)
    base = f"/api/v1/projects/{project['id']}"
    url = base + f"/wiki/entity/{entity}"
    payload = {"fingerprint": client.get(url).json()["fingerprint"]}
    first = client.post(
        url + "/summarize", json=payload, headers={"Idempotency-Key": "resume-first"}
    ).json()
    vault = client.app.state.vault_registry.require(project["id"])
    store = JobStore(vault.database, project["id"])
    fence = store.claim(first["id"], "test-wiki")
    store.pause(fence, "test_failed", failed=True)
    second = client.post(
        url + "/summarize", json=payload, headers={"Idempotency-Key": "resume-second"}
    ).json()
    old = store.read(first["id"])
    resume = client.post(
        base + f"/ai/jobs/{first['id']}/resume",
        json={"expected_control_revision": old["control_revision"]},
        headers={"Idempotency-Key": "resume-old"},
    )
    assert resume.status_code == 409 and resume.json()["detail"]["code"] == "WIKI_JOB_ACTIVE"
    store.cancel(second["id"])
    resume = client.post(
        base + f"/ai/jobs/{first['id']}/resume",
        json={"expected_control_revision": old["control_revision"]},
        headers={"Idempotency-Key": "resume-old-allowed"},
    )
    assert resume.status_code == 202
    assert client.get(url).json()["job"]["id"] == first["id"]


def test_alias_boundaries_and_single_character(client, project):
    from novel_harness.services.wiki import expand_aliases

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as s:
        s.add(
            Entity(
                project_id=project["id"],
                kind="character",
                name="林渡",
                profile={"aliases": ["Al", "鸦"]},
            )
        )
        s.flush()
        assert expand_aliases(s, "Alice walks") == "Alice walks"
        assert "林渡" in expand_aliases(s, "Al walks")
        assert "林渡" in expand_aliases(s, "鸦")
        assert expand_aliases(s, "乌鸦飞过") == "乌鸦飞过"


def test_inclusive_summary_scope_and_unrelated_future_chapter(client, project):
    from novel_harness.db.models import ChapterSummary, ChapterVersion

    entity, first, _, _, _ = seed(client, project)
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as s:
        version = ChapterVersion(project_id=project["id"], chapter_id=first, content="林渡走入旧城")
        s.add(version)
        s.flush()
        summary = ChapterSummary(
            project_id=project["id"],
            chapter_id=first,
            version_id=version.id,
            title="初见的总结",
            content_hash="a" * 64,
            recap="林渡走入旧城",
        )
        s.add(summary)
        s.flush()
        summary_id = summary.id
    url = f"/api/v1/projects/{project['id']}/wiki/entity/{entity}"
    page = client.get(url, params={"chapter_id": first}).json()
    assert summary_id in [s["id"] for s in page["sources"]]
    with vault.database.session_scope() as s:
        s.add(
            StoryNode(
                project_id=project["id"], kind="chapter", title="另一个未来章", order_index=50
            )
        )
    assert (
        client.get(url, params={"chapter_id": first}).json()["fingerprint"] == page["fingerprint"]
    )
