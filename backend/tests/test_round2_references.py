"""Generation must receive complete explicit sources but only active automatic styles."""

import hashlib

import pytest
from fastapi import HTTPException

from novel_harness.ai.base import AITextRequest, ContextBudgetError, check_input_budget
from novel_harness.ai.demo import DemoProvider
from novel_harness.db.models import (
    ChapterDocument,
    ChapterSummary,
    ChapterVersion,
    Entity,
    EntityRelation,
    Idea,
    PlotThread,
    Project,
    StoryNode,
    StyleProfile,
)
from novel_harness.services.chunks import source_text
from novel_harness.services.local_vectors import LocalVectorIndex
from novel_harness.services.pipeline import CreationPipeline
from novel_harness.services.references import resolve_references, source_citation
from novel_harness.services.retrieval import search
from novel_harness.services.writing_context import collect_task_hard_context


def initial_request(session, project_id, chapter_id, query, budget=20000):
    pipeline = CreationPipeline(DemoProvider())
    snapshot = pipeline.build_snapshot(
        session, session.get(Project, project_id), chapter_id, {}, budget, "chat", query, None
    )
    request = pipeline._initial_request("chat", query, {}, snapshot)
    assert isinstance(request, AITextRequest)
    assert check_input_budget(request) <= budget
    return snapshot, request.context["context_packet"]["fragments"]


@pytest.fixture
def reference_vault(client, project):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.add_all(
            [
                StoryNode(
                    id=f"chapter-{i}",
                    project_id=project["id"],
                    title=f"Chapter {i}",
                    kind="chapter",
                    order_index=i,
                )
                for i in range(5)
            ]
            + [
                Entity(
                    id="person",
                    project_id=project["id"],
                    name="Person",
                    kind="character",
                    summary="",
                    profile={"secret": "PROFILE_SECRET"},
                    state={"injury": "BROKEN_ARM"},
                ),
                Entity(id="other", project_id=project["id"], name="Other", kind="character"),
                PlotThread(
                    id="plot",
                    project_id=project["id"],
                    title="Promise",
                    kind="subplot",
                    promise="The key",
                    payoff="PAYOFF_SECRET",
                ),
            ]
        )
        session.flush()
        session.add(
            EntityRelation(
                id="relation",
                project_id=project["id"],
                source_entity_id="person",
                target_entity_id="other",
                relation_type="siblings",
                description="Their link",
            )
        )
        for i in range(4):
            content = "Saved manuscript, pure prose." if i == 0 else f"Past chapter {i}"
            session.add(
                ChapterVersion(
                    id=f"version-{i}",
                    project_id=project["id"],
                    chapter_id=f"chapter-{i}",
                    content=content,
                )
            )
            session.flush()
            session.add(
                ChapterSummary(
                    id=f"summary-{i}",
                    project_id=project["id"],
                    chapter_id=f"chapter-{i}",
                    version_id=f"version-{i}",
                    title=f"Summary {i}",
                    recap="Recap",
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    details={"end_state": "OLDER_SUMMARY_SECRET" if i == 0 else "At home"},
                )
            )
        session.add(
            ChapterDocument(
                chapter_id="chapter-0",
                project_id=project["id"],
                content="Unsaved working copy",
                current_version_id="version-0",
            )
        )
    return vault


@pytest.mark.parametrize(
    ("kind", "source_id", "expected"),
    [
        ("entity", "person", ["PROFILE_SECRET", "BROKEN_ARM"]),
        ("relation", "relation", ['"source_entity_id": "person"', '"target_entity_id": "other"']),
        ("plot", "plot", ["PAYOFF_SECRET"]),
        ("summary", "summary-0", ["OLDER_SUMMARY_SECRET"]),
        ("manuscript", "chapter-0", ["Saved manuscript, pure prose."]),
    ],
)
def test_explicit_structured_source_reaches_actual_request(
    reference_vault, project, kind, source_id, expected
):
    query = f"[[ref:{kind}:{source_id}]]"
    with reference_vault.database.session_scope() as session:
        source = resolve_references(session, query)[0]
        citation_before = source_citation(source)
        snapshot, fragments = initial_request(session, project["id"], "chapter-4", query)
        matches = [fragment for fragment in fragments if fragment["source_id"] == source_id]
        assert len(matches) == 1
        assert all(value in matches[0]["content"] for value in expected)
        assert matches[0]["content"] == source_text(source)
        explicit = next(f for f in snapshot["fragments"] if f["source_id"] == source_id)
        assert explicit["channel"] == "explicit"
        assert explicit["citation"] == {
            **citation_before,
            **(
                {"entity_state_chapter_id": "chapter-4"}
                if kind == "entity"
                else {}
            ),
        }
        assert source_citation(resolve_references(session, query)[0]) == citation_before
        if kind == "manuscript":
            assert matches[0]["content"] == "Saved manuscript, pure prose."


def test_explicit_structured_payload_is_hard_and_raises_over_budget(reference_vault, project):
    with reference_vault.database.session_scope() as session:
        entity = session.get(Entity, "person")
        entity.profile = {"secret": "PROFILE_SECRET " * 4000}
        session.flush()
        pipeline = CreationPipeline(DemoProvider())
        query = "[[ref:entity:person]]"
        snapshot = pipeline.build_snapshot(
            session,
            session.get(Project, project["id"]),
            "chapter-4",
            {},
            1200,
            "chat",
            query,
            None,
        )
        explicit = next(f for f in snapshot["fragments"] if f["source_id"] == "person")
        assert explicit["hard"] is True
        assert "person" not in snapshot["dropped_source_ids"]
        with pytest.raises(ContextBudgetError):
            pipeline._initial_request("chat", query, {}, snapshot)


def test_explicit_sources_from_manuscript_instructions_and_contract_are_deduplicated(
    reference_vault, project
):
    contract = {
        "purpose": "[[ref:plot:plot]] [[ref:entity:person]]",
    }
    query = "[[ref:entity:other]] [[ref:entity:person]]"
    with reference_vault.database.session_scope() as session:
        session.add(
            ChapterDocument(
                chapter_id="chapter-4",
                project_id=project["id"],
                content="[[ref:entity:person]]",
                contract=contract,
            )
        )
        session.flush()

        hard, _, _ = collect_task_hard_context(
            session,
            session.get(Project, project["id"]),
            "chapter-4",
            contract,
            "chat",
            query,
        )

        expected = {
            item["id"]: {
                **source_citation(item),
                **(
                    {"entity_state_chapter_id": "chapter-4"}
                    if item["type"] == "entity"
                    else {}
                ),
            }
            for item in resolve_references(
                session,
                "[[ref:entity:person]] [[ref:entity:other]] [[ref:plot:plot]]",
            )
        }
        explicit = [fragment for fragment in hard if fragment.channel == "explicit"]
        assert {fragment.source_id for fragment in explicit} == set(expected)
        assert all(fragment.hard for fragment in explicit)
        assert all(fragment.citation == expected[fragment.source_id] for fragment in explicit)
        assert sum(fragment.source_id == "person" for fragment in explicit) == 1


def test_explicit_current_manuscript_coexists_with_contract_and_current_draft(
    reference_vault, project
):
    query = "[[ref:manuscript:chapter-0]]"
    with reference_vault.database.session_scope() as session:
        source = resolve_references(session, query)[0]
        hard, _, _ = collect_task_hard_context(
            session,
            session.get(Project, project["id"]),
            "chapter-0",
            {},
            "chat",
            query,
        )

        matches = {
            fragment.source_type: fragment
            for fragment in hard
            if fragment.source_id == "chapter-0"
        }
        assert set(matches) == {
            "chapter_contract",
            "chapter_node",
            "current_draft",
            "manuscript",
        }
        assert matches["current_draft"].channel == "task_input"
        assert matches["current_draft"].content == "Unsaved working copy"
        assert matches["manuscript"].hard is True
        assert matches["manuscript"].channel == "explicit"
        assert matches["manuscript"].content == source_text(source)
        assert matches["manuscript"].content == "Saved manuscript, pure prose."
        assert matches["manuscript"].citation == source_citation(source)


def test_review_pipeline_keeps_current_draft_with_explicit_current_manuscript(
    reference_vault, project
):
    query = "[[ref:manuscript:chapter-0]]"
    with reference_vault.database.session_scope() as session:
        pipeline = CreationPipeline(DemoProvider())
        snapshot = pipeline.build_snapshot(
            session,
            session.get(Project, project["id"]),
            "chapter-0",
            {},
            20000,
            "review",
            query,
            None,
        )
        request = pipeline._initial_request("review", query, {}, snapshot)

        fragments = request.context["context_packet"]["fragments"]
        current = next(
            fragment for fragment in fragments if fragment["source_type"] == "current_draft"
        )
        manuscript = next(
            fragment for fragment in fragments if fragment["source_type"] == "manuscript"
        )
        assert current["content"] == "Unsaved working copy"
        assert manuscript["content"] == "Saved manuscript, pure prose."


def test_explicit_sources_with_same_id_keep_first_reference_kind(reference_vault, project):
    query = "[[ref:entity:person]] [[ref:plot:person]]"
    with reference_vault.database.session_scope() as session:
        session.add(
            PlotThread(
                id="person",
                project_id=project["id"],
                title="Same id plot",
                kind="subplot",
                payoff="SHOULD_BE_DEDUPLICATED",
            )
        )
        session.flush()
        expected = resolve_references(session, query)[0]

        hard, _, _ = collect_task_hard_context(
            session,
            session.get(Project, project["id"]),
            "chapter-4",
            {},
            "chat",
            query,
        )

        matches = [fragment for fragment in hard if fragment.source_id == "person"]
        assert len(matches) == 1
        assert matches[0].source_type == "entity"
        assert matches[0].channel == "explicit"
        assert matches[0].content == source_text(expected)
        assert matches[0].citation == {
            **source_citation(expected),
            "entity_state_chapter_id": "chapter-4",
        }


def test_explicit_source_from_other_vault_is_unavailable(reference_vault, client, project):
    other = client.post("/api/v1/projects", json={"title": "Other Vault"}).json()
    foreign = client.post(
        f"/api/v1/projects/{other['id']}/entities",
        json={"kind": "character", "name": "Foreign", "profile": {"secret": "FOREIGN_SECRET"}},
    ).json()
    with reference_vault.database.session_scope() as session:
        with pytest.raises(HTTPException) as error:
            initial_request(session, project["id"], "chapter-4", f"[[ref:entity:{foreign['id']}]]")
        assert error.value.status_code == 422
        assert error.value.detail["code"] == "REFERENCE_UNAVAILABLE"


def test_ordinary_retrieval_still_sends_only_matching_chunk(reference_vault, project):
    with reference_vault.database.session_scope() as session:
        session.add(
            Idea(
                id="long-idea",
                project_id=project["id"],
                title="Long idea",
                content="ordinary " * 600 + "needle unique passage " + "ordinary " * 600,
            )
        )
        session.flush()
        snapshot, fragments = initial_request(session, project["id"], "chapter-4", "needle")
        fragment = next(f for f in fragments if f["source_id"] == "long-idea")
        audit = next(f for f in snapshot["fragments"] if f["source_id"] == "long-idea")
        assert "needle" in fragment["content"] and len(fragment["content"]) <= 1200
        assert audit["citation"]["chunk"]["ordinal"] > 0


@pytest.mark.parametrize(("pinned", "query"), [(True, "unrelated"), (False, "STYLE_NEEDLE")])
def test_inactive_styles_are_explicit_only_not_automatic(
    client, project, seeded_chapter, pinned, query
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.add_all(
            [
                StyleProfile(
                    id="inactive",
                    project_id=project["id"],
                    name="STYLE_NEEDLE",
                    is_active=False,
                    is_pinned=pinned,
                    config={"instructions": "INACTIVE_SECRET"},
                ),
                StyleProfile(
                    id="active",
                    project_id=project["id"],
                    name="Active",
                    is_active=True,
                    is_pinned=True,
                    config={"instructions": "ACTIVE_SECRET"},
                ),
            ]
        )
        session.flush()
        _, fragments = initial_request(session, project["id"], seeded_chapter, query)
        assert "inactive" not in {f["source_id"] for f in fragments}
        assert any(f["source_id"] == "active" and f["hard"] for f in fragments)
        assert "inactive" in {item["id"] for item in search(session, "STYLE_NEEDLE")["items"]}
        _, explicit = initial_request(
            session, project["id"], seeded_chapter, "[[ref:style:inactive]]"
        )
        assert any(
            f["source_id"] == "inactive" and "INACTIVE_SECRET" in f["content"] for f in explicit
        )
    base = f"/api/v1/projects/{project['id']}"
    assert client.get(base + "/library/style/inactive").status_code == 200
    assert "inactive" in {
        row["id"]
        for row in client.get(base + "/library/page", params={"category": "style"}).json()["items"]
    }


class FixedEmbeddings:
    model = "round2-test-only"

    def embed(self, _text):
        return [1.0, 0.0]


@pytest.mark.parametrize("use_vectors", [False, True])
def test_inactive_styles_excluded_before_candidate_limits(
    client, project, seeded_chapter, use_vectors
):
    vault = client.app.state.vault_registry.require(project["id"])
    with vault.database.session_scope() as session:
        session.add_all(
            [
                StyleProfile(
                    id=f"inactive-{i:03}",
                    project_id=project["id"],
                    name="needle",
                    is_active=False,
                    is_pinned=True,
                    config={"instructions": "needle"},
                )
                for i in range(105)
            ]
            + [
                Idea(
                    id="eligible",
                    project_id=project["id"],
                    title="Available material",
                    content="needle " + "filler " * 80,
                ),
                StyleProfile(
                    id="active",
                    project_id=project["id"],
                    name="Active",
                    is_active=True,
                    is_pinned=True,
                    config={"instructions": "ACTIVE_SECRET"},
                ),
            ]
        )
    with vault.database.session_scope() as session:
        if use_vectors:
            vectors = LocalVectorIndex(vault.root, FixedEmbeddings())
            vectors.rebuild(session)
            session.info["vectors"] = vectors
        query = "semanticquery" if use_vectors else "needle"
        _, fragments = initial_request(session, project["id"], seeded_chapter, query)
        ids = {f["source_id"] for f in fragments}
        assert "eligible" in ids
        assert not any(source_id.startswith("inactive-") for source_id in ids)
        assert any(f["source_id"] == "active" and f["hard"] for f in fragments)
