"""Transactional sources and author decisions for paired writing/quality sessions."""

from copy import deepcopy
from difflib import SequenceMatcher

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from novel_harness.ai.base import ContextBudgetError, check_input_budget
from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import ActivityEvent, AIJob, ChapterSummary, ChapterVersion
from novel_harness.services.entity_states import assert_project_entity_states_resolved
from novel_harness.services.job_context import (
    assert_source,
    capture_manifest,
    capture_source,
    capture_summary_source,
)
from novel_harness.services.job_state import ACTIVE, command_hash
from novel_harness.services.projects import require_project
from novel_harness.services.retrieval import ordered_nodes
from novel_harness.services.versions import create_version, get_or_create_document, require_chapter
from novel_harness.services.writing_context import collect_explicit_context

TASK = "quality_workflow"


def capture_quality_context(session, project_id, chapter_id, instructions=""):
    context = capture_summary_source(session, project_id, chapter_id)["content_check_context"]
    document = get_or_create_document(session, chapter_id)
    fragments = collect_explicit_context(
        session,
        require_project(session, project_id),
        chapter_id,
        document.contract,
        instructions,
        document=document,
    )
    context["explicit_sources"] = [
        {
            "id": f"{fragment.source_type}:{fragment.source_id}",
            "type": fragment.source_type,
            "constraint": "explicit",
            "content": fragment.content,
            "citation": fragment.citation,
        }
        for fragment in fragments
    ]
    context["allowed_reference_ids"] = sorted(
        {
            *context["allowed_reference_ids"],
            *(item["id"] for item in context["explicit_sources"]),
        }
    )
    return jsonable_encoder(context)


def require_quality(session, project_id, job_id):
    job = session.get(AIJob, job_id)
    if job is None or job.project_id != project_id or job.task_type != TASK:
        raise HTTPException(404, detail={"code": "QUALITY_RUN_NOT_FOUND"})
    return job


def assert_quality_available(session, project_id, chapter_id=None, exclude_id=None):
    rows = session.execute(
        select(AIJob.id, AIJobControl.command)
        .join(AIJobControl, AIJobControl.job_id == AIJob.id)
        .where(
            AIJob.project_id == project_id,
            AIJob.task_type == TASK,
            AIJob.status.in_(ACTIVE | {"recovery_required"}),
        )
    )
    for job_id, command in rows:
        if job_id == exclude_id:
            continue
        ids = {item["chapter_id"] for item in command.get("chapters", [])}
        if chapter_id is None or chapter_id in ids:
            raise HTTPException(
                409,
                detail={
                    "code": "QUALITY_RUN_ACTIVE",
                    "job_id": job_id,
                    "message": "章节正在由写作与优化会话交替处理，请先完成或取消该流程。",
                },
            )


def capture_quality_source(session, project_id, command):
    require_project(session, project_id)
    ids = [item["chapter_id"] for item in command["chapters"]]
    ordered = [n.id for n in ordered_nodes(session) if n.kind == "chapter"]
    for chapter_id in ids:
        chapter = require_chapter(session, chapter_id)
        if chapter.project_id != project_id:
            raise HTTPException(404, detail={"code": "CHAPTER_NOT_FOUND"})
    if ids != [cid for cid in ordered if cid in ids]:
        raise HTTPException(
            422, detail={"code": "QUALITY_CHAPTER_ORDER", "message": "请按故事顺序选择章节。"}
        )
    chapters = []
    for item in command["chapters"]:
        chapter = require_chapter(session, item["chapter_id"])
        assert_project_entity_states_resolved(session, project_id, chapter.id)
        source = capture_source(session, project_id, chapter.id)
        if source["revision"] != item["expected_revision"]:
            raise HTTPException(
                409,
                detail={
                    "code": "QUALITY_SOURCE_CHANGED",
                    "message": "正文已更新，请重新加载后提交。",
                },
            )
        if command["mode"] == "polish" and not source["content"].strip():
            raise HTTPException(
                422,
                detail={"code": "QUALITY_EMPTY_MANUSCRIPT", "message": "请先保存需要优化的正文。"},
            )
        if len(source["content"]) > 50000:
            raise HTTPException(
                422,
                detail={
                    "code": "QUALITY_MANUSCRIPT_TOO_LONG",
                    "message": "单章超过 50,000 字符，请先拆分章节。",
                },
            )
        context = capture_quality_context(session, project_id, chapter.id, command["instructions"])
        capture_manifest(
            session,
            source,
            {
                "format_version": 2,
                "fragments": context["explicit_sources"],
            },
        )
        chapters.append(
            {
                "chapter_id": chapter.id,
                "title": chapter.title,
                "source": source,
                "context": context,
            }
        )
    return jsonable_encoder(
        {
            "kind": "quality",
            "project_id": project_id,
            "mode": command["mode"],
            "chapters": chapters,
            "instructions": command["instructions"],
            "token_budget": command["token_budget"],
            "quality_target": command["quality_target"],
        }
    )


def assert_quality_source(session, source):
    try:
        for item in source["chapters"]:
            assert_source(session, item["source"])
            assert_project_entity_states_resolved(session, source["project_id"], item["chapter_id"])
            context = capture_quality_context(
                session, source["project_id"], item["chapter_id"], source["instructions"]
            )
            if jsonable_encoder(context) != item["context"]:
                raise ValueError("Quality context changed")
    except (HTTPException, ValueError) as exc:
        raise HTTPException(
            409,
            detail={
                "code": "QUALITY_SOURCE_CHANGED",
                "message": "正文、章节顺序或参考资料已变化，旧流程已暂停。请按最新资料重新开始。",
            },
        ) from exc


def enqueue_quality(store, payload, key, identity):
    command = {
        **payload.model_dump(),
        "project_id": store.project_id,
        "chapter_id": payload.chapters[0].chapter_id,
        "task_type": TASK,
    }

    def prepare(session):
        assert_quality_available(session, store.project_id)
        ids = [item["chapter_id"] for item in command["chapters"]]
        busy = session.scalar(
            select(AIJob.id).where(
                AIJob.project_id == store.project_id,
                AIJob.chapter_id.in_(ids),
                AIJob.status.in_(ACTIVE | {"recovery_required"}),
            )
        )
        if busy:
            raise HTTPException(
                409,
                detail={
                    "code": "CHAPTER_JOB_ACTIVE",
                    "message": "选定章节仍有未结束任务，请先处理原任务。",
                },
            )
        source = capture_quality_source(session, store.project_id, command)
        from novel_harness.services.quality_generation import first_request

        try:
            for index in range(len(source["chapters"])):
                request, schema = first_request(
                    {**source, "chapters": source["chapters"][index:]}, identity
                )
                check_input_budget(request, schema)
        except ContextBudgetError as exc:
            raise HTTPException(
                422, detail={"code": "CONTEXT_BUDGET_EXCEEDED", "message": str(exc)}
            ) from exc
        return {
            "source_snapshot": source,
            "provider_identity": identity,
            "embedding_identity": None,
        }

    return store.enqueue(command, key, prepare)


def approval_hash(item):
    return command_hash({"content": item["candidate_text"]})


def validate_quality_resume(session, control):
    if control.effects.get("accepted_chapters"):
        raise HTTPException(
            409,
            detail={
                "code": "QUALITY_ALREADY_ACCEPTED",
                "message": "本任务已有章节采纳，请对剩余章节新建质量任务。",
            },
        )
    source = control.source_snapshot
    assert_quality_available(session, source["project_id"], exclude_id=control.job_id)
    busy = session.scalar(
        select(AIJob.id).where(
            AIJob.project_id == source["project_id"],
            AIJob.id != control.job_id,
            AIJob.chapter_id.in_([item["chapter_id"] for item in source["chapters"]]),
            AIJob.status.in_(ACTIVE | {"recovery_required"}),
        )
    )
    if busy:
        raise HTTPException(
            409,
            detail={
                "code": "CHAPTER_JOB_ACTIVE",
                "job_id": busy,
                "message": "选定章节已有其他未结束任务，请先处理原任务。",
            },
        )
    assert_quality_source(session, source)
    job = require_quality(session, source["project_id"], control.job_id)
    for item in job.result.get("chapters", []):
        if item["status"] == "needs_review" and control.effects.get("quality_approvals", {}).get(
            item["chapter_id"]
        ) != approval_hash(item):
            raise HTTPException(
                409,
                detail={
                    "code": "QUALITY_REVIEW_REQUIRED",
                    "message": "请先阅读质量报告并明确确认本章，再继续协作。",
                },
            )


def approve_quality(store, job_id, chapter_id, expected_revision, confirmed):
    if not confirmed:
        raise HTTPException(422, detail={"code": "QUALITY_CONFIRMATION_REQUIRED"})
    with store.write() as session:
        job = require_quality(session, store.project_id, job_id)
        control = session.get(AIJobControl, job_id)
        if control.control_revision != expected_revision:
            raise HTTPException(409, detail={"code": "JOB_CONTROL_CHANGED"})
        if job.status != "recovery_required" or job.error_code != "QUALITY_REVIEW_REQUIRED":
            raise HTTPException(409, detail={"code": "QUALITY_NOT_AWAITING_REVIEW"})
        item = next(
            (
                c
                for c in job.result.get("chapters", [])
                if c["chapter_id"] == chapter_id and c["status"] == "needs_review"
            ),
            None,
        )
        if not item:
            raise HTTPException(409, detail={"code": "QUALITY_NOT_AWAITING_REVIEW"})
        assert_quality_source(session, control.source_snapshot)
        control.effects = {
            **control.effects,
            "quality_approvals": {
                **control.effects.get("quality_approvals", {}),
                chapter_id: approval_hash(item),
            },
        }
        control.control_revision += 1
        session.add(
            ActivityEvent(
                project_id=store.project_id,
                kind="quality_override",
                entity_id=job.id,
                details={
                    "chapter_id": chapter_id,
                    "candidate_hash": approval_hash(item),
                    "confirmed": True,
                },
            )
        )
        session.flush()
        return store.serialize_in_session(session, job_id)


def _assert_acceptance_context(session, project_id, source, current, accepted):
    original = deepcopy(source["context"])
    # Verify the exact frozen references. Accepting a preceding candidate makes
    # its old summary stale and can fill the recent-three window with older,
    # unused summaries; recomputing that window is not a source-identity check.
    for reference in original["recent_summaries"]:
        if reference["chapter_id"] in accepted:
            continue
        summary = session.get(ChapterSummary, reference["id"])
        if (
            summary is None
            or summary.deleted_at
            or summary.project_id != project_id
            or summary.chapter_id != reference["chapter_id"]
            or summary.status != "valid"
            or summary.revision != reference["revision"]
            or command_hash({"recap": summary.recap, "details": summary.details})
            != reference["hash"]
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "QUALITY_SOURCE_CHANGED",
                    "message": "任务使用的章节摘要已变化，请重新优化。",
                },
            )
    for context in (current, original):
        summary_ids = {f"summary:{item['id']}" for item in context["recent_summaries"]}
        context["recent_summaries"] = []
        context["allowed_reference_ids"] = [
            ref for ref in context["allowed_reference_ids"] if ref not in summary_ids
        ]
    if current != original:
        raise HTTPException(
            409,
            detail={"code": "QUALITY_SOURCE_CHANGED", "message": "参考资料已变化，请重新优化。"},
        )


def _quality_diff_parts(original, candidate):
    before, after = original.splitlines(keepends=True), candidate.splitlines(keepends=True)
    coarse = len(before) > 2048 or len(after) > 2048
    if coarse:
        before, after = [original], [candidate]
    operations = SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    if sum(tag != "equal" for tag, *_ in operations) > 128:
        before, after, coarse = [original], [candidate], True
        operations = SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
    fingerprint = command_hash({"original": original, "candidate": candidate})
    hunks = []
    for tag, start, end, new_start, new_end in operations:
        if tag == "equal":
            continue
        hunks.append({
            "id": command_hash({"source": fingerprint, "range": [start, end, new_start, new_end]}),
            "old_text": "".join(before[start:end]),
            "new_text": "".join(after[new_start:new_end]),
        })
    return before, after, operations, hunks, coarse


def quality_diff(session, project_id, job_id, chapter_id):
    job = require_quality(session, project_id, job_id)
    control = session.get(AIJobControl, job.id)
    item = next((c for c in job.result.get("chapters", []) if c["chapter_id"] == chapter_id), None)
    source = next(
        (c for c in control.source_snapshot.get("chapters", []) if c["chapter_id"] == chapter_id),
        None,
    )
    if not source or not item or not item.get("candidate_text"):
        raise HTTPException(409, detail={"code": "QUALITY_NO_CANDIDATE"})
    _, _, _, hunks, coarse = _quality_diff_parts(
        source["source"]["content"], item["candidate_text"]
    )
    return {
        "chapter_id": chapter_id,
        "source_revision": source["source"]["revision"],
        "mode": control.command["mode"],
        "hunks": hunks,
        "coarse": coarse,
    }


def _selected_quality_text(original, candidate, selected_hunk_ids):
    before, after, operations, hunks, _ = _quality_diff_parts(original, candidate)
    selected = set(selected_hunk_ids)
    if not selected or len(selected) != len(selected_hunk_ids):
        raise HTTPException(422, detail={
            "code": "QUALITY_SELECTION_REQUIRED", "message": "请至少选择一处修改，不能重复选择。",
        })
    if not selected.issubset({hunk["id"] for hunk in hunks}):
        raise HTTPException(409, detail={
            "code": "QUALITY_DIFF_CHANGED", "message": "差异区块已变化，请重新加载后选择。",
        })
    output, index = [], 0
    for tag, start, end, new_start, new_end in operations:
        if tag == "equal":
            output.extend(before[start:end])
            continue
        use_candidate = hunks[index]["id"] in selected
        output.extend(after[new_start:new_end] if use_candidate else before[start:end])
        index += 1
    content = "".join(output)
    if not content.strip() or content == original:
        raise HTTPException(422, detail={
            "code": "QUALITY_NO_SELECTED_CHANGE", "message": "所选修改没有形成有效正文，无需采纳。",
        })
    return content


def accept_quality(store, job_id, chapter_id, confirmed=False, selected_hunk_ids=None):
    with store.write() as session:
        job = require_quality(session, store.project_id, job_id)
        control = session.get(AIJobControl, job_id)
        accepted = control.effects.get("accepted_chapters", {})
        if chapter_id in accepted:
            previous_selection = control.effects.get("accepted_selections", {}).get(chapter_id)
            if (sorted(previous_selection) if previous_selection is not None else None) != (
                sorted(selected_hunk_ids) if selected_hunk_ids is not None else None
            ):
                raise HTTPException(409, detail={
                    "code": "QUALITY_ACCEPTANCE_CHANGED",
                    "message": "本章已按另一组选项采纳，请查看已保存版本，不会再次覆盖。",
                })
            version = session.get(ChapterVersion, accepted[chapter_id])
            if not version:
                raise HTTPException(409, detail={"code": "ACCEPTED_VERSION_UNAVAILABLE"})
            from novel_harness.services.serialization import serialize

            return serialize(version)
        if job.status not in {"succeeded", "failed", "cancelled"}:
            raise HTTPException(
                409,
                detail={
                    "code": "QUALITY_ACCEPT_AFTER_STOP",
                    "message": "请等待流程完成，或先取消流程再采纳已完成候选。",
                },
            )
        chapters = job.result.get("chapters", [])
        item = next((c for c in chapters if c["chapter_id"] == chapter_id), None)
        if (
            not item
            or item["status"] not in {"ready", "needs_review"}
            or not item.get("candidate_text", "").strip()
        ):
            raise HTTPException(409, detail={"code": "QUALITY_NO_CANDIDATE"})
        assert_quality_available(session, store.project_id, chapter_id, exclude_id=job.id)
        busy = session.scalar(
            select(AIJob.id).where(
                AIJob.project_id == store.project_id,
                AIJob.id != job.id,
                AIJob.chapter_id == chapter_id,
                AIJob.status.in_(ACTIVE | {"recovery_required"}),
            )
        )
        if busy:
            raise HTTPException(
                409,
                detail={
                    "code": "CHAPTER_JOB_ACTIVE",
                    "job_id": busy,
                    "message": "本章已有未结束任务，请先完成或取消它，再采纳旧候选。",
                },
            )
        previous = chapters[: chapters.index(item)]
        if any(c["chapter_id"] not in accepted for c in previous):
            raise HTTPException(
                409,
                detail={
                    "code": "QUALITY_ACCEPT_ORDER",
                    "message": "请按章节顺序采纳，保证后章所用的前章候选已写入正文。",
                },
            )
        for prior in previous:
            doc = get_or_create_document(session, prior["chapter_id"])
            version = session.get(ChapterVersion, accepted[prior["chapter_id"]])
            if (
                not version
                or doc.current_version_id != version.id
                or doc.content != version.content
            ):
                raise HTTPException(
                    409,
                    detail={
                        "code": "QUALITY_SOURCE_CHANGED",
                        "message": "前章已修改，请重新优化后续章节。",
                    },
                )
            prior_source = next(
                c
                for c in control.source_snapshot["chapters"]
                if c["chapter_id"] == prior["chapter_id"]
            )
            # Only the author-approved text/version/revision transition is
            # expected. A changed prior chapter contract or hard context is not.
            assert_source(
                session,
                {
                    **prior_source["source"],
                    "content": version.content,
                    "version_id": version.id,
                    "revision": doc.revision,
                },
            )
            assert_project_entity_states_resolved(session, store.project_id, prior["chapter_id"])
            prior_context = jsonable_encoder(
                capture_summary_source(session, store.project_id, prior["chapter_id"])[
                    "content_check_context"
                ]
            )
            # Revalidate every context dependency that influenced the handed-off
            # candidate. Its accepted text may no longer contain the old explicit
            # reference markers; assert_source already checked their manifest.
            explicit = deepcopy(prior_source["context"].get("explicit_sources", []))
            prior_context["explicit_sources"] = explicit
            prior_context["allowed_reference_ids"] = sorted(
                {*prior_context["allowed_reference_ids"], *(ref["id"] for ref in explicit)}
            )
            _assert_acceptance_context(
                session, store.project_id, prior_source, prior_context, accepted
            )
        source = next(
            c for c in control.source_snapshot["chapters"] if c["chapter_id"] == chapter_id
        )
        assert_source(session, source["source"])
        assert_project_entity_states_resolved(session, store.project_id, chapter_id)
        current = capture_quality_context(
            session, store.project_id, chapter_id, control.source_snapshot["instructions"]
        )
        _assert_acceptance_context(session, store.project_id, source, current, accepted)
        content = item["candidate_text"]
        if selected_hunk_ids is not None:
            if control.command["mode"] != "polish" or len(chapters) != 1:
                raise HTTPException(422, detail={
                    "code": "QUALITY_PARTIAL_POLISH_ONLY",
                    "message": "局部采纳仅支持单章精修；交替协作需要完整采纳交接正文。",
                })
            content = _selected_quality_text(
                source["source"]["content"], content, selected_hunk_ids
            )
        severe = [
            s
            for s in source["source"]["contract"].get("forbidden_revelations", [])
            if s and s in content
        ]
        if (severe or item["status"] == "needs_review") and not confirmed:
            raise HTTPException(
                409,
                detail={
                    "code": "SEVERE_CONFIRMATION_REQUIRED",
                    "message": "候选仍有质量或禁止揭示问题，请核对后明确确认采纳。",
                },
            )
        version = create_version(
            session,
            chapter_id,
            content=content,
            source="ai",
            summary=("局部采纳质量优化建议（组合稿未整体复核）"
                     if selected_hunk_ids is not None else "采纳文章质量优化候选"),
            generation_job_id=job.id,
            expected_revision=item["source_revision"],
        )
        control.effects = {
            **control.effects,
            "accepted_chapters": {**accepted, chapter_id: version.id},
        }
        if selected_hunk_ids is not None:
            control.effects = {**control.effects, "accepted_selections": {
                **control.effects.get("accepted_selections", {}),
                chapter_id: sorted(selected_hunk_ids),
            }}
        session.add(
            ActivityEvent(
                project_id=store.project_id,
                kind="quality_accept",
                entity_id=job.id,
                details={
                    "chapter_id": chapter_id,
                    "version_id": version.id,
                    "confirmed": confirmed,
                    **({"selected_hunk_ids": sorted(selected_hunk_ids)}
                       if selected_hunk_ids is not None else {}),
                },
            )
        )
        from novel_harness.services.serialization import serialize

        return serialize(version)
