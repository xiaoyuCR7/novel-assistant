from novel_harness.db.models import AIJob
from novel_harness.services.pipeline import CreationPipeline
from novel_harness.services.versions import get_or_create_document


def test_duplicate_accept_returns_original_without_overwriting_later_edit(
    client,
    project,
    seeded_chapter,
):
    database = client.app.state.vault_registry.require(project["id"]).database
    with database.job_session_scope() as session:
        document = get_or_create_document(session, seeded_chapter)
        job = AIJob(
            project_id=project["id"],
            chapter_id=seeded_chapter,
            task_type="draft",
            prompt_version="test",
            status="succeeded",
            result={"candidate_text": "候选稿", "source_revision": document.revision},
        )
        session.add(job)
        session.flush()
        job_id = job.id
    with database.job_session_scope() as session:
        version_id = CreationPipeline.accept(session, job_id).id
    with database.job_session_scope() as session:
        document = get_or_create_document(session, seeded_chapter)
        document.content = "作者后来修改的正文"
        document.revision += 1
    with database.job_session_scope() as session:
        assert CreationPipeline.accept(session, job_id).id == version_id
        assert get_or_create_document(session, seeded_chapter).content == "作者后来修改的正文"
