from importlib import import_module

import pytest
from fastapi import HTTPException

from novel_harness.db.models import (
    CanonFact,
    ChapterSummary,
    ChapterVersion,
    Idea,
    StoryNode,
)


def test_summary_source_freezes_bounded_content_check_context(
    client, project, seeded_chapter
):
    context = import_module("novel_harness.services.job_context")
    database = client.app.state.vault_registry.require(project["id"]).database
    summary_ids = []
    with database.job_session_scope() as session:
        current = session.get(StoryNode, seeded_chapter)
        current.order_index = 4
        fact = CanonFact(
            project_id=project["id"],
            predicate="寄信人",
            value="守钟人",
            status="confirmed",
            is_pinned=True,
        )
        unpinned = CanonFact(
            project_id=project["id"],
            predicate="钟楼状态",
            value="关闭",
            status="confirmed",
            is_pinned=False,
        )
        session.add_all([fact, unpinned])
        for index in range(5):
            chapter = StoryNode(
                project_id=project["id"],
                kind="chapter",
                parent_id=current.parent_id,
                title=f"上下文章节 {index}",
                order_index=index,
            )
            session.add(chapter)
            session.flush()
            version = ChapterVersion(
                project_id=project["id"],
                chapter_id=chapter.id,
                content=f"正文 {index}",
            )
            session.add(version)
            session.flush()
            summary = ChapterSummary(
                project_id=project["id"],
                chapter_id=chapter.id,
                version_id=version.id,
                title=chapter.title,
                content_hash=str(index) * 64,
                recap=f"总结 {index}",
                details={"end_state": f"状态 {index}"},
            )
            session.add(summary)
            session.flush()
            summary_ids.append(summary.id)
        fact_id = fact.id
        unpinned_id = unpinned.id
        source = context.capture_summary_source(session, project["id"], seeded_chapter)

    frozen = source["content_check_context"]
    assert frozen["project"]["premise"] == project["premise"]
    assert frozen["chapter"]["id"] == seeded_chapter
    assert [item["id"] for item in frozen["recent_summaries"]] == summary_ids[1:4][::-1]
    assert f"canon_fact:{fact_id}" in frozen["allowed_reference_ids"]
    assert f"canon_fact:{unpinned_id}" in frozen["allowed_reference_ids"]
    assert any(
        item["id"] == f"canon_fact:{unpinned_id}"
        for item in frozen["confirmed_fact_sources"]
    )
    assert summary_ids[4] not in frozen["allowed_reference_ids"]

    with database.job_session_scope() as session:
        session.get(CanonFact, fact_id).value = "邮差"
    with database.job_session_scope() as session:
        with pytest.raises(HTTPException) as error:
            context.assert_source(session, source)
        assert error.value.detail["code"] == "SOURCE_CHANGED"


def test_hard_change_invalidates_but_unselected_soft_does_not(client, project, seeded_chapter):
    context = import_module("novel_harness.services.job_context")
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        source = context.capture_source(session, project["id"], seeded_chapter)
    with database.job_session_scope() as session:
        session.add(Idea(project_id=project["id"], title="未选软参考", content="无关"))
    with database.job_session_scope() as session:
        context.assert_source(session, source)
    with database.job_session_scope() as session:
        session.add(
            CanonFact(
                project_id=project["id"],
                predicate="禁令",
                value="不可逆转时间",
                is_pinned=True,
            )
        )
    with database.job_session_scope() as session:
        with pytest.raises(HTTPException) as error:
            context.assert_source(session, source)
        assert error.value.detail["code"] == "SOURCE_CHANGED"


def test_selected_reference_change_invalidates(client, project):
    context = import_module("novel_harness.services.job_context")
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        idea = Idea(project_id=project["id"], title="选中", content="原内容")
        session.add(idea)
        session.flush()
        source = context.capture_source(session, project["id"], None)
        context.capture_manifest(
            session,
            source,
            {
                "fragments": [
                    {"citation": {"type": "idea", "id": idea.id}},
                ]
            },
        )
        idea_id = idea.id
    with database.job_session_scope() as session:
        session.get(Idea, idea_id).content = "新内容"
    with database.job_session_scope() as session:
        with pytest.raises(HTTPException):
            context.assert_source(session, source)


def test_identity_excludes_credentials(client):
    identity = client.app.state.model_settings.identity()
    assert set(identity) == {
        "mode",
        "base_url",
        "model",
        "external_consent",
        "output_token_budget",
        "context_capacity",
        "deadline_seconds",
        "output_parameter",
        "thinking_mode",
    }


def test_manuscript_manifest_uses_chapter_reference_and_tracks_current_version(
    client,
    project,
    seeded_chapter,
):
    from novel_harness.services.versions import create_version

    context = import_module("novel_harness.services.job_context")
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        create_version(session, seeded_chapter, content="引用原文", source="manual")
        source = context.capture_source(session, project["id"], None)
        context.capture_manifest(
            session,
            source,
            {
                "fragments": [
                    {"citation": {"type": "manuscript", "id": seeded_chapter}},
                ]
            },
        )
    with database.job_session_scope() as session:
        context.assert_source(session, source)
        create_version(session, seeded_chapter, content="替换的版本", source="manual")
    with database.job_session_scope() as session:
        with pytest.raises(HTTPException):
            context.assert_source(session, source)


def test_provider_identity_allows_key_rotation_but_not_consent_or_target_change(client):
    from novel_harness.services.model_settings import ModelConfigInput

    settings = client.app.state.model_settings
    config = dict(
        mode="api",
        base_url="https://example.com/v1",
        model="test-model",
        external_consent=True,
        api_key="fake-key-one",
    )
    settings.save(ModelConfigInput(**config))
    identity = settings.identity()
    config["api_key"] = "fake-key-two"
    settings.save(ModelConfigInput(**config))
    assert settings.identity() == identity
    assert settings.provider_for_identity(identity, None)._api_key == "fake-key-two"
    settings.save(ModelConfigInput(mode="demo", clear_api_key=True))
    with pytest.raises(HTTPException) as error:
        settings.provider_for_identity(identity, None)
    assert error.value.detail["code"] == "PROVIDER_CHANGED"
