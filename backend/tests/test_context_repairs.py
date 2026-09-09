import json

from job_helpers import complete_summary, run_job, save_chapter
from sqlalchemy import text

from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import CanonFact, Idea, Project, StyleProfile, StyleRule
from novel_harness.services.chunks import source_text
from novel_harness.services.references import resolve_references, source_citation
from novel_harness.services.retrieval import search
from novel_harness.services.writing_context import build_context, collect_hard_context


def context(client, project, chapter, contract=None, budget=20000, query=""):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        return build_context(
            session,
            session.get(Project, project["id"]),
            chapter,
            contract or {},
            budget,
            "chat",
            query,
        )


def test_project_chat_without_chapter_still_retrieves_soft_material(client, project):
    base = f"/api/v1/projects/{project['id']}"
    idea = client.post(
        base + "/ideas",
        json={"title": "Global material", "content": "projectwideuniqueneedle"},
    ).json()

    packet = context(client, project, None, query="projectwideuniqueneedle")

    assert idea["id"] in {fragment.source_id for fragment in packet.fragments}


def test_automatic_hard_context_requires_pinned_and_valid_material(
    client, project, seeded_chapter
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        pinned_canon = CanonFact(
            project_id=project["id"], predicate="PINNED_CANON", value="must remain", is_pinned=True
        )
        unpinned_canon = CanonFact(
            project_id=project["id"], predicate="UNPINNED_CANON", value="soft only"
        )
        pending_canon = CanonFact(
            project_id=project["id"], predicate="PENDING_CANON", value="not ready", is_pinned=True,
            status="pending",
        )
        pinned_profile = StyleProfile(
            project_id=project["id"], name="Pinned style", is_active=True, is_pinned=True
        )
        unpinned_profile = StyleProfile(
            project_id=project["id"], name="Unpinned style", is_active=True
        )
        inactive_profile = StyleProfile(
            project_id=project["id"], name="Inactive style", is_active=False, is_pinned=True
        )
        pinned_rule = StyleRule(
            project_id=project["id"], instruction="PINNED_RULE", is_pinned=True
        )
        unpinned_rule = StyleRule(project_id=project["id"], instruction="UNPINNED_RULE")
        pending_rule = StyleRule(
            project_id=project["id"], instruction="PENDING_RULE", is_pinned=True, status="pending"
        )
        ineffective_rule = StyleRule(
            project_id=project["id"], instruction="", rule_type="feedback", is_pinned=True
        )
        session.add_all(
            [
                pinned_canon, unpinned_canon, pending_canon, pinned_profile, unpinned_profile,
                inactive_profile, pinned_rule, unpinned_rule, pending_rule, ineffective_rule,
            ]
        )
        session.flush()

        hard, _ = collect_hard_context(
            session, session.get(Project, project["id"]), seeded_chapter, {}
        )

    automatic_ids = {fragment.source_id for fragment in hard}
    assert {pinned_canon.id, pinned_profile.id, pinned_rule.id} <= automatic_ids
    assert not {
        unpinned_canon.id,
        pending_canon.id,
        unpinned_profile.id,
        inactive_profile.id,
        unpinned_rule.id,
        pending_rule.id,
        ineffective_rule.id,
    } & automatic_ids


def test_unpinned_confirmed_canon_does_not_expand_automatic_hard_context(
    client, project, seeded_chapter
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        stored_project = session.get(Project, project["id"])
        baseline, _ = collect_hard_context(session, stored_project, seeded_chapter, {})
        session.add_all(
            [
                CanonFact(
                    project_id=project["id"],
                    predicate=f"UNPINNED_SCALE_{index}",
                    value="long confirmed material " * 40,
                    status="confirmed",
                )
                for index in range(300)
            ]
        )
        session.flush()

        expanded, _ = collect_hard_context(session, stored_project, seeded_chapter, {})

    assert len(expanded) == len(baseline)
    assert {fragment.source_id for fragment in expanded} == {
        fragment.source_id for fragment in baseline
    }
    assert sum(len(fragment.content) for fragment in expanded) == sum(
        len(fragment.content) for fragment in baseline
    )


def test_default_preflight_keeps_300_unpinned_confirmed_facts_out_of_hard_input(
    client, project, seeded_chapter
):
    """Regression: treating every confirmed fact as hard makes default capacity fail."""
    value = "甲" * 40
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        facts = [
            CanonFact(
                project_id=project["id"],
                predicate=f"UNPINNED_PREFLIGHT_{index}",
                value=value,
                status="confirmed",
            )
            for index in range(300)
        ]
        session.add_all(facts)
        session.flush()
        assert all(fact.status == "confirmed" and not fact.is_pinned for fact in facts)
        assert all(len(fact.value) == 40 for fact in facts)

    chapter = client.get(
        f"/api/v1/projects/{project['id']}/chapters/{seeded_chapter}"
    ).json()
    response = client.post(
        f"/api/v1/projects/{project['id']}/ai/jobs/preflight",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "chat",
            "instructions": "默认容量预检",
            "expected_revision": chapter["revision"],
        },
    )

    assert response.status_code == 200, response.text
    preflight = response.json()
    assert preflight["context_capacity"] == 32_768
    assert preflight["can_fit"] is True
    assert preflight["required_input_tokens"] * 4 < preflight["effective_input_limit"]


def test_retrieval_requires_confirmed_constraints_and_does_not_enumerate_unpinned(
    client, project, seeded_chapter
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        confirmed_canon = CanonFact(
            project_id=project["id"], predicate="CONFIRMED_CANON_NEEDLE", value="present"
        )
        pending_canon = CanonFact(
            project_id=project["id"],
            predicate="PENDING_CANON_NEEDLE",
            value="absent",
            status="pending",
        )
        confirmed_rule = StyleRule(
            project_id=project["id"], instruction="CONFIRMED_RULE_NEEDLE"
        )
        pending_rule = StyleRule(
            project_id=project["id"], instruction="PENDING_RULE_NEEDLE", status="pending"
        )
        session.add_all([confirmed_canon, pending_canon, confirmed_rule, pending_rule])
        session.flush()

        canon_hits = search(session, "CONFIRMED_CANON_NEEDLE", chapter_id=seeded_chapter)["items"]
        rule_hits = search(session, "CONFIRMED_RULE_NEEDLE", chapter_id=seeded_chapter)["items"]
        pending_hits = search(session, "PENDING_CANON_NEEDLE", chapter_id=seeded_chapter)["items"]
        pending_rule_hits = search(
            session, "PENDING_RULE_NEEDLE", chapter_id=seeded_chapter
        )["items"]
        unrelated_hits = search(session, "UNRELATED_QUERY", chapter_id=seeded_chapter)["items"]

    assert confirmed_canon.id in {item["id"] for item in canon_hits}
    assert confirmed_rule.id in {item["id"] for item in rule_hits}
    assert pending_canon.id not in {item["id"] for item in pending_hits}
    assert pending_rule.id not in {item["id"] for item in pending_rule_hits}
    assert not {
        confirmed_canon.id, confirmed_rule.id, pending_canon.id, pending_rule.id
    } & {item["id"] for item in unrelated_hits}


def test_unpinned_constraints_linked_to_chapter_do_not_use_structure_channel(
    client, project, seeded_chapter
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        canon = CanonFact(
            project_id=project["id"],
            predicate="CHAPTER_LINKED_CANON",
            value="soft only",
            valid_from_node_id=seeded_chapter,
        )
        rule = StyleRule(project_id=project["id"], instruction="CHAPTER_LINKED_RULE")
        session.add_all([canon, rule])
        session.flush()
        session.execute(
            text("UPDATE search_documents SET linked_nodes=:nodes WHERE key=:key"),
            {"key": f"style_rule:{rule.id}", "nodes": json.dumps([seeded_chapter])},
        )

        items = search(session, "UNRELATED_QUERY", chapter_id=seeded_chapter)["items"]

    assert not {canon.id, rule.id} & {item["id"] for item in items}


def test_queryless_browse_excludes_constraints_but_pinned_channel_keeps_pinned_material(
    client, project, seeded_chapter
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        unpinned_canon = CanonFact(
            project_id=project["id"], predicate="UNPINNED_BROWSE_CANON", value="not automatic"
        )
        unpinned_rule = StyleRule(
            project_id=project["id"], instruction="UNPINNED_BROWSE_RULE"
        )
        pinned_canon = CanonFact(
            project_id=project["id"],
            predicate="PINNED_BROWSE_CANON",
            value="must remain available",
            is_pinned=True,
        )
        pinned_rule = StyleRule(
            project_id=project["id"], instruction="PINNED_BROWSE_RULE", is_pinned=True
        )
        idea = Idea(project_id=project["id"], title="Browsable idea", content="ordinary material")
        session.add_all([unpinned_canon, unpinned_rule, pinned_canon, pinned_rule, idea])
        session.flush()

        whole_book = search(session, query="", chapter_id=None)["items"]
        chapter = search(session, query="", chapter_id=seeded_chapter)["items"]

    whole_book_ids = {item["id"] for item in whole_book}
    assert idea.id in whole_book_ids
    assert not {
        unpinned_canon.id,
        unpinned_rule.id,
        pinned_canon.id,
        pinned_rule.id,
    } & whole_book_ids
    chapter_items = {item["id"]: item for item in chapter}
    assert {pinned_canon.id, pinned_rule.id} <= chapter_items.keys()
    assert all("pinned" in chapter_items[item_id]["channels"] for item_id in chapter_items)


def test_relevant_constraints_are_hard_only_when_pinned(client, project, seeded_chapter):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        unpinned_canon = CanonFact(
            project_id=project["id"], predicate="SOFT_CANON_NEEDLE", value="relevant"
        )
        pinned_canon = CanonFact(
            project_id=project["id"],
            predicate="HARD_CANON_NEEDLE",
            value="relevant",
            is_pinned=True,
        )
        unpinned_rule = StyleRule(
            project_id=project["id"], instruction="SOFT_RULE_NEEDLE"
        )
        pinned_rule = StyleRule(
            project_id=project["id"], instruction="HARD_RULE_NEEDLE", is_pinned=True
        )
        session.add_all([unpinned_canon, pinned_canon, unpinned_rule, pinned_rule])
        session.flush()

        soft_canon = search(session, "SOFT_CANON_NEEDLE", chapter_id=seeded_chapter)["items"]
        hard_canon = search(session, "HARD_CANON_NEEDLE", chapter_id=seeded_chapter)["items"]
        soft_rule = search(session, "SOFT_RULE_NEEDLE", chapter_id=seeded_chapter)["items"]
        hard_rule = search(session, "HARD_RULE_NEEDLE", chapter_id=seeded_chapter)["items"]

    assert next(
        item for item in soft_canon if item["id"] == unpinned_canon.id
    )["constraint"] == "soft"
    assert next(
        item for item in hard_canon if item["id"] == pinned_canon.id
    )["constraint"] == "hard"
    assert next(
        item for item in soft_rule if item["id"] == unpinned_rule.id
    )["constraint"] == "soft"
    assert next(
        item for item in hard_rule if item["id"] == pinned_rule.id
    )["constraint"] == "hard"


def test_active_pinned_style_profile_is_hard_but_other_profiles_and_pinned_idea_are_soft(
    client, project, seeded_chapter
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        pinned_profile = StyleProfile(
            project_id=project["id"],
            name="HARD_PROFILE_NEEDLE",
            is_active=True,
            is_pinned=True,
            config={"instructions": "hard style"},
        )
        unpinned_profile = StyleProfile(
            project_id=project["id"],
            name="SOFT_PROFILE_NEEDLE",
            is_active=True,
            config={"instructions": "soft style"},
        )
        inactive_profile = StyleProfile(
            project_id=project["id"],
            name="INACTIVE_PROFILE_NEEDLE",
            is_active=False,
            is_pinned=True,
            config={"instructions": "not automatic"},
        )
        pinned_idea = Idea(
            project_id=project["id"],
            title="PINNED_IDEA_NEEDLE",
            content="still a soft reference",
            is_pinned=True,
        )
        session.add_all([pinned_profile, unpinned_profile, inactive_profile, pinned_idea])
        session.flush()

        hard_profile = search(
            session, "HARD_PROFILE_NEEDLE", chapter_id=seeded_chapter, lightweight=True
        )["items"]
        soft_profile = search(session, "SOFT_PROFILE_NEEDLE", chapter_id=seeded_chapter)[
            "items"
        ]
        idea = search(session, "PINNED_IDEA_NEEDLE", chapter_id=seeded_chapter)["items"]
        inactive = search(session, "INACTIVE_PROFILE_NEEDLE", chapter_id=seeded_chapter)[
            "items"
        ]

    pinned_item = next(item for item in hard_profile if item["id"] == pinned_profile.id)
    assert pinned_item["type"] == "style"
    assert pinned_item["constraint"] == "hard"
    assert next(item for item in soft_profile if item["id"] == unpinned_profile.id)[
        "constraint"
    ] == "soft"
    assert next(item for item in idea if item["id"] == pinned_idea.id)["constraint"] == "soft"
    assert next(item for item in inactive if item["id"] == inactive_profile.id)[
        "constraint"
    ] == "soft"


def test_stale_pending_constraint_projections_are_ineligible(client, project, seeded_chapter):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        canon = CanonFact(
            project_id=project["id"], predicate="STALE_PENDING_CANON_NEEDLE", value="not eligible"
        )
        rule = StyleRule(
            project_id=project["id"], instruction="STALE_PENDING_RULE_NEEDLE"
        )
        session.add_all([canon, rule])
        session.flush()
        for key in (f"canon:{canon.id}", f"style_rule:{rule.id}"):
            session.execute(
                text("UPDATE search_documents SET status='pending' WHERE key=:key"), {"key": key}
            )

        canon_hits = search(session, "STALE_PENDING_CANON_NEEDLE", chapter_id=seeded_chapter)[
            "items"
        ]
        rule_hits = search(session, "STALE_PENDING_RULE_NEEDLE", chapter_id=seeded_chapter)[
            "items"
        ]

    assert canon.id not in {item["id"] for item in canon_hits}
    assert rule.id not in {item["id"] for item in rule_hits}


def test_future_and_expired_facts_never_reenter_via_search(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    later = client.post(
        base + "/nodes", json={"kind": "chapter", "title": "Later", "order_index": 10}
    ).json()["id"]
    future = client.post(
        base + "/canon",
        json={"predicate": "SECRET", "value": "future", "valid_from_node_id": later},
    ).json()
    expired = client.post(
        base + "/canon",
        json={"predicate": "SECRET", "value": "old", "valid_to_node_id": seeded_chapter},
    ).json()
    assert future["id"] not in {
        f.source_id for f in context(client, project, seeded_chapter, query="SECRET").fragments
    }
    assert expired["id"] not in {
        f.source_id for f in context(client, project, later, query="SECRET").fragments
    }


def test_required_entity_survives_hard_fact_search_limit(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(base + "/entities", json={"kind": "character", "name": "NEEDED"}).json()
    for i in range(55):
        client.post(base + "/canon", json={"predicate": str(i), "value": "rule"})
    packet = context(
        client, project, seeded_chapter, {"required_entity_ids": [entity["id"]]}, query="NEEDED"
    )
    assert entity["id"] in {f.source_id for f in packet.fragments}


def test_author_summary_recap_is_single_authority(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": "Original events"})
    summary = complete_summary(client, base + f"/chapters/{seeded_chapter}").json()
    updated = client.patch(
        base + f"/chapters/{seeded_chapter}/summary",
        json={
            "revision": summary["revision"],
            "summary_id": summary["id"],
            "recap": "corrected",
            "details": {**summary["details"], "recap": "old"},
        },
    ).json()
    assert updated["details"]["recap"] == "corrected"


def test_over_budget_request_never_calls_provider(client, project, seeded_chapter):
    calls = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            calls.append(request)
            return super().generate_text(request)

    client.app.state.ai_provider = Recorder()
    base = f"/api/v1/projects/{project['id']}"
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": "正文" * 2000})
    job = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "chat",
            "token_budget": 256,
        },
    ).json()
    assert calls == []
    assert job["status"] == "failed"
    assert job["error_code"] == "CONTEXT_BUDGET_EXCEEDED"


def test_explicit_reference_overflow_makes_preflight_fail(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    material = client.post(
        base + "/entities",
        json={"kind": "character", "name": "large", "summary": "素材" * 10000},
    ).json()
    saved = save_chapter(
        client,
        base + f"/chapters/{seeded_chapter}",
        json={"content": ""},
    ).json()
    command = {
        "project_id": project["id"],
        "chapter_id": seeded_chapter,
        "task_type": "chat",
        "instructions": "baseline",
        "token_budget": 12000,
        "expected_revision": saved["revision"],
    }

    baseline = client.post(base + "/ai/jobs/preflight", json=command).json()
    assert baseline["can_fit"] is True

    command["instructions"] = f"[[ref:entity:{material['id']}]]"
    overflow = client.post(base + "/ai/jobs/preflight", json=command).json()
    assert overflow["can_fit"] is False
    assert overflow["required_input_tokens"] > overflow["effective_input_limit"]
    assert "超限" in overflow["message"]


def test_explicit_reference_overflow_never_calls_provider(client, project, seeded_chapter):
    calls = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            calls.append(request)
            return super().generate_text(request)

    client.app.state.ai_provider = Recorder()
    base = f"/api/v1/projects/{project['id']}"
    material = client.post(
        base + "/entities",
        json={"kind": "character", "name": "large", "summary": "素材" * 10000},
    ).json()
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": ""})

    job = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "chat",
            "instructions": f"[[ref:entity:{material['id']}]]",
            "token_budget": 12000,
        },
    ).json()

    assert calls == []
    assert job["status"] == "failed"
    assert job["error_code"] == "CONTEXT_BUDGET_EXCEEDED"


def test_unaccepted_history_identifies_candidate_status(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}/ai/jobs"
    payload = {"project_id": project["id"], "chapter_id": seeded_chapter}
    candidate = run_job(client, base, json={**payload, "task_type": "draft"}).json()
    chat = run_job(client, base, json={**payload, "task_type": "chat"}).json()
    history = next(
        f for f in chat["context_snapshot"]["fragments"] if f["source_type"] == "conversation"
    )
    turn = json.loads(history["content"])[0]
    assert turn["status"] == "unaccepted_candidate"
    assert turn["source_revision"] == candidate["result"]["source_revision"]


def test_explicit_required_entity_coexists_with_generic_required_context(
    client, project, seeded_chapter
):
    base = f"/api/v1/projects/{project['id']}"
    entity = client.post(base + "/entities", json={"kind": "character", "name": "sample"}).json()
    save_chapter(
        client,
        base + f"/chapters/{seeded_chapter}",
        json={"content": "", "contract": {"required_entity_ids": [entity["id"]]}},
    )
    query = f"[[ref:entity:{entity['id']}]]"
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        source = resolve_references(session, query)[0]
        expected_content = source_text(source)
        expected_citation = {
            **source_citation(source),
            "entity_state_chapter_id": seeded_chapter,
        }
    job = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "chat",
            "instructions": query,
        },
    ).json()
    assert job["status"] == "succeeded"
    matches = [
        fragment
        for fragment in job["context_snapshot"]["fragments"]
        if fragment["source_id"] == entity["id"]
    ]
    assert len(matches) == 2
    required = next(fragment for fragment in matches if fragment["source_type"] == "story_entity")
    explicit = next(fragment for fragment in matches if fragment["source_type"] == "entity")
    assert required["hard"] is True
    assert required["channel"] == "constraint"
    assert explicit["hard"] is True
    assert explicit["channel"] == "explicit"
    assert explicit["content"] == expected_content
    assert explicit["citation"] == expected_citation


def test_hard_budget_overflow_skips_embedding_retrieval(client, project, seeded_chapter):
    import pytest

    class ForbiddenVectorCall:
        def search(self, query, limit):
            pytest.fail("Hard input already exceeds budget; do not embed")

    base = f"/api/v1/projects/{project['id']}"
    save_chapter(client, base + f"/chapters/{seeded_chapter}", json={"content": "正文" * 2000})
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.info["vectors"] = ForbiddenVectorCall()
        packet = build_context(
            session, session.get(Project, project["id"]), seeded_chapter, {}, 256, "chat", "review"
        )
        assert packet.over_budget


def test_pov_and_location_are_required_even_beyond_search_limit(client, project, seeded_chapter):
    base = f"/api/v1/projects/{project['id']}"
    for i in range(52):
        client.post(base + "/entities", json={"kind": "character", "name": f"other {i}"})
    pov = client.post(base + "/entities", json={"kind": "character", "name": "POV"}).json()["id"]
    location = client.post(base + "/entities", json={"kind": "location", "name": "ROOM"}).json()[
        "id"
    ]
    packet = context(
        client,
        project,
        seeded_chapter,
        {"pov_entity_id": pov, "location_entity_id": location},
        budget=2000,
    )
    assert {pov, location} <= {f.source_id for f in packet.fragments if f.hard}


def test_full_hard_input_preflight_includes_prompt_before_vector_call(client):
    import pytest

    from novel_harness.ai.base import ContextBudgetError
    from novel_harness.services.pipeline import CreationPipeline

    project = client.post("/api/v1/projects", json={"title": "test"}).json()
    calls = []

    class Vectors:
        def search(self, query, limit):
            calls.append(query)
            return []

    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.info["vectors"] = Vectors()
        pipeline = CreationPipeline(DemoProvider())
        snapshot = pipeline.build_snapshot(
            session,
            session.get(Project, project["id"]),
            None,
            {},
            256,
            "chat",
            "question " * 90,
            None,
        )
        with pytest.raises(ContextBudgetError):
            pipeline._initial_request("chat", "question " * 90, {}, snapshot)
    assert calls == []


def test_soft_context_is_trimmed_to_fit_real_request_and_audited(client, project, seeded_chapter):
    from novel_harness.ai.base import check_input_budget

    calls = []

    class Recorder(DemoProvider):
        def generate_text(self, request):
            calls.append(request)
            return super().generate_text(request)

    base = f"/api/v1/projects/{project['id']}"
    for i in range(60):
        client.post(
            base + "/entities",
            json={"kind": "character", "name": f"other {i}", "summary": "background " * 20},
        )
    client.app.state.ai_provider = Recorder()
    response = run_job(
        client,
        base + "/ai/jobs",
        json={
            "project_id": project["id"],
            "chapter_id": seeded_chapter,
            "task_type": "chat",
            "instructions": "discuss",
            "token_budget": 1200,
        },
    ).json()
    assert response["status"] == "succeeded"
    assert check_input_budget(calls[0]) <= 1200
    persisted = client.get(base + "/ai/jobs/" + response["id"]).json()
    audit = persisted["context_snapshot"]["stage_inputs"]["chat"]
    assert audit["dropped_source_ids"]
    assert audit["included_sources"] == [
        {"source_type": f["source_type"], "source_id": f["source_id"]}
        for f in calls[0].context["context_packet"]["fragments"]
    ]
