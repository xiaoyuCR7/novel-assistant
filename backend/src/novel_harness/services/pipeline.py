"""Explicit, traceable novel creation pipeline."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from time import monotonic
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_harness.ai.base import (
    AIProvider,
    AITextRequest,
    ContextBudgetError,
    ExecutionLimits,
    ProviderExecutionError,
    StructuredResult,
    TextResult,
    check_input_budget,
    estimate_input_tokens,
)
from novel_harness.ai.prompts import (
    ARCHITECT_INSTRUCTION,
    AUTHOR_INSTRUCTION,
    CHAT_INSTRUCTION,
    CONTINUITY_INSTRUCTION,
    FINAL_REVIEW_INSTRUCTION,
    REFERENCE_POLICY,
    RESOLVER_INSTRUCTION,
    STYLE_INSTRUCTION,
)
from novel_harness.ai.schema import compact_schema
from novel_harness.db.models import (
    ActivityEvent,
    AIJob,
    CanonFact,
    ChapterVersion,
    Conflict,
    Entity,
    PlotThread,
    Project,
    TimelineEvent,
)
from novel_harness.schemas.stages import PlanOutput, ResolutionOutput, ReviewOutput
from novel_harness.services.context import ContextAssembler
from novel_harness.services.continuity import ContinuityChecker, ContinuityInput
from novel_harness.services.entity_states import (
    assert_project_entity_states_resolved,
    entity_state_conflict_detail,
    project_entity_state_conflicts,
    serialize_entity_for_chapter,
)
from novel_harness.services.feedback import create_conflict, suggest_options
from novel_harness.services.projects import require_project
from novel_harness.services.serialization import serialize
from novel_harness.services.versions import (
    begin_version_write,
    create_version,
    get_or_create_document,
)

_STAGE_OUTPUTS = {
    "plan": PlanOutput,
    "review": ReviewOutput,
    "continuity_review": ReviewOutput,
    "style_review": ReviewOutput,
    "final_review": ReviewOutput,
    "resolve": ResolutionOutput,
    "suggest": ResolutionOutput,
}

_TASK_INSTRUCTIONS = {
    "chat": CHAT_INSTRUCTION,
    "plan": ARCHITECT_INSTRUCTION,
    "draft": AUTHOR_INSTRUCTION,
    "rewrite": AUTHOR_INSTRUCTION,
    "continue": AUTHOR_INSTRUCTION,
    "scene_description": AUTHOR_INSTRUCTION,
    "review": CONTINUITY_INSTRUCTION,
    "suggest": RESOLVER_INSTRUCTION,
}


def _compact_output_schema(value):
    """Keep typed stage schemas within legacy task budgets."""
    return compact_schema(value)


def serialize_job(job: AIJob) -> dict:
    return serialize(job)


class CreationPipeline:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider
        self.context_assembler = ContextAssembler()
        self.continuity_checker = ContinuityChecker()
        self.stage_runner = None
        self.provider_factory = None
        self.pending_reviews = []

    def run_durable(self, store, fence, provider_factory, *, vectors=None, validate_provider=None):
        from novel_harness.db.job_models import AIJobControl
        from novel_harness.services.conversation import capture_history, prepare_conversation
        from novel_harness.services.job_context import assert_source, capture_manifest
        from novel_harness.services.job_stages import StageRunner

        def guard():
            state_conflicts = []
            chapter_id = None
            provider_error = None
            source_error = None
            with store.database.job_session_scope() as session:
                guarded_job, control = store.assert_running(session, fence)
                chapter_id = guarded_job.chapter_id
                if chapter_id:
                    state_conflicts = project_entity_state_conflicts(
                        session, guarded_job.project_id, chapter_id
                    )
                if validate_provider:
                    try:
                        validate_provider()
                    except Exception as exc:
                        provider_error = exc
                from novel_harness.services.search_index import require_ready_index

                require_ready_index(session)
                try:
                    assert_source(session, control.source_snapshot)
                except HTTPException as exc:
                    source_error = exc
            if state_conflicts:
                with store.write() as session:
                    self._persist_entity_state_conflict(
                        session, chapter_id, state_conflicts
                    )
            if provider_error is not None:
                raise provider_error
            if source_error is not None:
                raise source_error
            if state_conflicts:
                raise HTTPException(
                    409, detail=entity_state_conflict_detail(state_conflicts)
                )

        self.provider_factory = provider_factory
        self.stage_runner = StageRunner(store, fence, guard)
        self.pending_reviews = []
        guard()
        state_conflict = None
        with store.database.job_session_scope() as session:
            job = store._job(session, fence.job_id)
            control = session.get(AIJobControl, job.id)
            self.execution_limits = ExecutionLimits.model_validate(
                control.provider_identity
            ).model_dump()
            source = deepcopy(control.source_snapshot)
            ready = control.context_ready
            snapshot = deepcopy(job.context_snapshot)
            if ready and snapshot.get("format_version") != 2:
                raise HTTPException(409, detail={"code": "CHECKPOINT_INCOMPATIBLE"})
        if not ready and "conversation" not in snapshot:
            # Freeze scope/order/content before retrieval or compression can send
            # a paid request. A restart must not recollect newly arriving turns.
            with store.write() as session:
                current, control = store.assert_running(session, fence)
                assert_source(session, source)
                snapshot = {"conversation": capture_history(
                    session, current, source, control.provider_identity,
                )}
                current.context_snapshot = deepcopy(snapshot)
        if not ready and "fragments" not in snapshot:
            with store.database.job_session_scope() as session:
                if vectors:
                    session.info["vectors"] = vectors(self.stage_runner)
                    session.info["durable_retrieval"] = True
                try:
                    snapshot = self.build_snapshot(
                        session,
                        require_project(session, job.project_id),
                        job.chapter_id,
                        source["contract"],
                        job.token_budget,
                        job.task_type,
                        job.instructions,
                        source["revision"],
                        conversation_state=snapshot["conversation"],
                    )
                    capture_manifest(session, source, snapshot)
                except HTTPException as exc:
                    if (
                        exc.status_code == 409
                        and isinstance(exc.detail, dict)
                        and exc.detail.get("code") == "ENTITY_STATE_CONFLICT"
                    ):
                        state_conflict = deepcopy(exc.detail)
                    else:
                        raise
        if state_conflict is not None:
            with store.write() as session:
                self._persist_entity_state_conflict(
                    session, job.chapter_id, state_conflict["conflicts"]
                )
            raise HTTPException(409, detail=state_conflict)
        if not ready:
            with store.write() as session:
                persisted, control = store.assert_running(session, fence)
                assert_source(session, source)
                persisted.context_snapshot = deepcopy(snapshot)
                control.source_snapshot = source
            snapshot = prepare_conversation(self, store, fence, job, source, snapshot)
            guard()
            with store.write() as session:
                persisted, control = store.assert_running(session, fence)
                assert_source(session, source)
                persisted.context_snapshot = deepcopy(snapshot)
                control.context_ready = True
        contract = source["contract"]
        started = monotonic()
        result = (
            self._run_full_pipeline(None, job, contract, snapshot)
            if job.task_type == "full_chapter"
            else self._run_single(None, job, job.task_type, contract, snapshot)
        )
        if "candidate_text" in result:
            result["source_revision"] = source["revision"]
            if job.task_type == "continue":
                result["candidate_text"] = (
                    source["content"].rstrip()
                    + ("\n\n" if source["content"].strip() else "")
                    + result["candidate_text"]
                )
            if job.task_type != "full_chapter":
                self._persist_rule_conflicts(None, job, result["candidate_text"], contract)
        result["execution"] = {
            **self.stage_runner.provider_metadata,
            "prompt_version": job.prompt_version,
            "duration_ms": round((monotonic() - started) * 1000),
        }

        def apply(session, current):
            assert_source(session, source)
            current.context_snapshot = deepcopy(snapshot)
            for method, args, kwargs in self.pending_reviews:
                getattr(self, method)(session, current, *args, **kwargs)
            current.result = result

        guard()
        store.publish(fence, apply)

    def _invoke(self, request, observer, schema=None):
        from novel_harness.ai.demo import DemoProvider

        provider = self.provider_factory(observer)
        if isinstance(provider, DemoProvider):
            observer.before_send()
        elif hasattr(provider, "attempt_observer"):
            provider.attempt_observer = observer
        else:
            raise ProviderExecutionError("提供者缺少发送检查点支持。", outcome="known")
        result = (
            provider.generate_structured(request, schema)
            if schema
            else provider.generate_text(request)
        )
        return result.model_dump()

    def _text(self, request):
        request.stream_preview = True
        if self.stage_runner is None:
            raise RuntimeError("CHECKPOINT_REQUIRED")

        def validate(value):
            result = TextResult.model_validate(value)
            if not result.text.strip():
                raise ValueError("Empty model output")
            return result.model_dump()

        return TextResult.model_validate(
            self.stage_runner.run(
                request.task,
                request.model_dump(),
                lambda observer: self._invoke(request, observer),
                validate,
            )
        )

    @staticmethod
    def accept(session: Session, job_id: str, *, confirmed=False):
        begin_version_write(session)
        job = session.get(AIJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND"})
        if job.status != "succeeded":
            raise HTTPException(status_code=409, detail={"code": "JOB_NOT_SUCCEEDED"})
        if job.accepted_version_id:
            version = session.get(ChapterVersion, job.accepted_version_id)
            if version is None:
                raise HTTPException(409, detail={"code": "ACCEPTED_VERSION_UNAVAILABLE"})
            return version
        from novel_harness.db.job_models import AIJobControl
        from novel_harness.services.job_context import assert_source

        control = session.get(AIJobControl, job_id)
        candidate = job.result.get("candidate_text")
        if not candidate or not job.chapter_id:
            raise HTTPException(status_code=409, detail={"code": "JOB_HAS_NO_ACCEPTABLE_TEXT"})
        document = get_or_create_document(session, job.chapter_id)
        if (
            job.result.get("source_revision") is not None
            and job.result["source_revision"] != document.revision
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "CANDIDATE_SOURCE_CHANGED",
                    "message": "生成后正文已修改，未覆盖你的改动。请基于最新正文重新生成。",
                },
            )
        if control:
            assert_source(session, control.source_snapshot)
        from novel_harness.services.drift import open_blocking_conflict_ids

        blocking_conflict_ids = open_blocking_conflict_ids(
            session,
            job.chapter_id,
            job.id,
            job.result.get("source_revision"),
        )
        severe = [
            phrase
            for phrase in document.contract.get("forbidden_revelations", [])
            if phrase and phrase in candidate
        ]
        if (severe or blocking_conflict_ids) and not confirmed:
            if blocking_conflict_ids:
                detail = {
                    "code": "SEVERE_CONFIRMATION_REQUIRED",
                    "message": "候选仍有未处理的严重内容问题，接受前需要作者明确确认。",
                    "conflict_ids": blocking_conflict_ids,
                }
            else:
                detail = {
                    "code": "SEVERE_CONFIRMATION_REQUIRED",
                    "message": "候选包含禁止提前揭示的内容："
                    + "、".join(severe)
                    + "。仍要接受并创建版本吗？",
                }
            raise HTTPException(
                409,
                detail=detail,
            )
        if severe or blocking_conflict_ids:
            session.add(
                ActivityEvent(
                    project_id=job.project_id,
                    kind="severe_override",
                    entity_id=job.id,
                    details={
                        "evidence": severe,
                        "conflict_ids": blocking_conflict_ids,
                        "confirmed": True,
                    },
                )
            )
        version = create_version(
            session,
            job.chapter_id,
            content=candidate,
            source="ai",
            summary="接受 AI 生成候选",
            generation_job_id=job.id,
        )
        proposals = (job.result.get("final_review") or {}).get("memory_candidates") or []
        if proposals:
            from novel_harness.services.memory_candidates import (
                persist_candidates,
                serialize_candidate,
            )

            candidates = persist_candidates(
                session,
                source_version=version,
                proposals=proposals,
                source_job=job,
            )
            job.result = {
                **job.result,
                "pending_canon_changes": [
                    serialize_candidate(candidate) for candidate in candidates
                ],
            }
        job.accepted_version_id = version.id
        session.flush()
        return version

    def build_snapshot(
        self,
        session,
        project,
        chapter_id,
        contract,
        token_budget,
        task_type,
        instructions,
        source_revision,
        *,
        conversation_state=None,
    ):
        if chapter_id:
            assert_project_entity_states_resolved(session, project.id, chapter_id)
        packet = self._context_packet(
            session, project, chapter_id, contract, token_budget, task_type, instructions
        )
        snapshot = {
            "format_version": 2,
            "execution_limits": getattr(self, "execution_limits", ExecutionLimits().model_dump()),
            "source_revision": source_revision,
            "task": packet.task,
            "token_budget": packet.token_budget,
            "total_estimated_tokens": packet.total_estimated_tokens,
            "over_budget": packet.over_budget,
            "dropped_source_ids": packet.dropped_source_ids,
            "fragments": [asdict(fragment) for fragment in packet.fragments],
        }
        from novel_harness.services.conversation import attach, collect_history

        state = conversation_state or {
            "version": 1, "scope": {"project_id": project.id, "chapter_id": chapter_id},
            "turns": collect_history(session, project.id, chapter_id, source_revision),
        }
        return attach(snapshot, state, state["turns"])

    @staticmethod
    def _persist_entity_state_conflict(session, chapter_id, conflicts):
        from novel_harness.services.entity_states import entity_state_conflict_evidence

        evidence = sorted(entity_state_conflict_evidence(item) for item in conflicts)
        related = sorted({item["entity_id"] for item in conflicts})
        existing = next(
            (
                item
                for item in session.scalars(
                    select(Conflict)
                    .where(
                        Conflict.chapter_id == chapter_id,
                        Conflict.code == "ENTITY_STATE_CONFLICT",
                        Conflict.status == "open",
                    )
                    .order_by(Conflict.created_at, Conflict.id)
                ).all()
                if sorted(item.evidence) == evidence
            ),
            None,
        )
        if existing is not None:
            suggest_options(session, existing.id)
            return existing
        conflict = create_conflict(
            session,
            chapter_id,
            {
                "code": "ENTITY_STATE_CONFLICT",
                "severity": "error",
                "message": "当前章节的人物状态记录互相矛盾，需要作者确认后才能继续。",
                "evidence": evidence,
                "related_entity_ids": related,
            },
        )
        suggest_options(session, conflict.id)
        return conflict

    def _context_packet(
        self,
        session: Session,
        project: Project,
        chapter_id: str,
        contract: dict[str, Any],
        token_budget: int,
        task_type: str,
        query: str = "",
    ):
        from novel_harness.services.writing_context import build_context

        def can_retrieve(hard):
            try:
                self._initial_request(
                    task_type,
                    query,
                    contract,
                    {
                        "token_budget": token_budget,
                        "fragments": [asdict(fragment) for fragment in hard],
                    },
                )
                return True
            except ContextBudgetError:
                return False

        return build_context(
            session,
            project,
            chapter_id,
            contract,
            token_budget,
            task_type,
            query,
            can_retrieve=can_retrieve,
        )

    @staticmethod
    def _initial_stage(task, prompt):
        if task == "full_chapter":
            task, prompt = "plan", prompt or "规划章节场景。"
        elif task == "continue":
            prompt = "只输出接在现有正文之后的新段落，不重复已有正文。\n" + prompt
        return task, prompt

    def _initial_request(self, task, prompt, contract, snapshot):
        task, prompt = self._initial_stage(task, prompt)
        return self._request(task, _TASK_INSTRUCTIONS[task], prompt, contract, snapshot)

    def estimate_initial_input(self, task, prompt, contract, snapshot):
        """Read-only estimate of the exact first-stage wrapper, including output schema."""
        task, prompt = self._initial_stage(task, prompt)
        request = self._build_request(task, _TASK_INSTRUCTIONS[task], prompt, contract, snapshot)
        output_type = _STAGE_OUTPUTS.get(task)
        return estimate_input_tokens(
            request,
            _compact_output_schema(output_type.model_json_schema())
            if output_type
            else None,
        )

    def _build_request(
        self,
        task: str,
        instruction: str,
        prompt: str,
        contract: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> AITextRequest:
        if snapshot.get("conversation", {}).get("version") == 1:
            from novel_harness.services.conversation import CONTINUATION_INSTRUCTION

            instruction += CONTINUATION_INSTRUCTION
        return AITextRequest(
            task=task,
            developer_instruction=instruction + REFERENCE_POLICY,
            user_prompt=prompt,
            context={
                "contract": contract,
                "context_packet": {
                    "fragments": [
                        {
                            **{
                                key: fragment[key]
                                for key in ("source_type", "source_id", "content", "hard")
                            },
                            "title": (fragment.get("citation") or {}).get(
                                "title", fragment.get("reason", "")
                            ),
                        }
                        for fragment in snapshot["fragments"]
                        if fragment["source_type"] != "chapter_contract"
                    ]
                },
            },
            token_budget=snapshot["token_budget"],
            **snapshot.get("execution_limits", getattr(self, "execution_limits", {})),
        )

    def _request(self, task, instruction, prompt, contract, snapshot) -> AITextRequest:
        request = self._build_request(task, instruction, prompt, contract, snapshot)
        output_type = _STAGE_OUTPUTS.get(task)
        schema = (
            _compact_output_schema(output_type.model_json_schema())
            if output_type
            else None
        )
        fragments = request.context["context_packet"]["fragments"]
        dropped = []
        while True:
            try:
                tokens = check_input_budget(request, schema)
                break
            except ContextBudgetError:
                protected = {"conversation", "conversation_summary"} if (
                    snapshot.get("conversation", {}).get("version") == 1
                ) else set()
                removable = next(
                    (i for i in range(len(fragments) - 1, -1, -1)
                     if not fragments[i]["hard"] and fragments[i]["source_type"] not in protected),
                    None,
                )
                if removable is None:
                    raise
                dropped.append(fragments.pop(removable)["source_id"])
        snapshot.setdefault("stage_inputs", {})[task] = {
            "estimated_input_tokens": tokens,
            "included_sources": [
                {"source_type": f["source_type"], "source_id": f["source_id"]} for f in fragments
            ],
            "dropped_source_ids": dropped,
        }
        return request

    def _structured(self, request, _schema=None):
        output_type = _STAGE_OUTPUTS[request.task]
        schema = _compact_output_schema(output_type.model_json_schema())
        check_input_budget(request, schema)
        if self.stage_runner is None:
            raise RuntimeError("CHECKPOINT_REQUIRED")

        def validate(value):
            result = StructuredResult.model_validate(value)
            result.data = output_type.model_validate(result.data).model_dump()
            if result.data.get("memory_candidates") is None:
                result.data.pop("memory_candidates", None)
            if output_type is ReviewOutput:
                from novel_harness.services.review_validation import validate_review

                fragments = request.context.get("context_packet", {}).get("fragments", [])
                draft = (
                    next(
                        (f["content"] for f in fragments if f["source_type"] == "current_draft"), ""
                    )
                    if request.task == "review"
                    else request.user_prompt
                )
                references = [
                    value for f in fragments for value in (f["content"], f.get("title", ""))
                ] + [
                    json.dumps(
                        request.context.get("contract", {}),
                        ensure_ascii=False,
                    )
                ]
                valid_entities = {
                    f["source_id"]
                    for f in fragments
                    if f["source_type"] in {"entity", "story_entity"}
                }
                validate_review(result.data, draft, references, valid_entities)
            return result.model_dump()

        repair_codes = {
            "INVALID_STAGE_OUTPUT", "INVALID_STRUCTURED_OUTPUT",
            "INVALID_REVIEW_EVIDENCE", "INVALID_REVIEW_ENTITY",
            "INVALID_REVIEW_OBSERVATION", "INVALID_MEMORY_EVIDENCE",
        } if output_type is ReviewOutput else set()

        def execute(req, key, *, repairable=False):
            check_input_budget(req, schema)
            return StructuredResult.model_validate(self.stage_runner.run(
                key,
                {"request": req.model_dump(), "schema": schema},
                lambda observer: self._invoke(req, observer, schema),
                validate,
                nonterminal_known_codes=repair_codes if repairable else None,
            ))

        try:
            return execute(request, request.task, repairable=bool(repair_codes))
        except (ValidationError, ProviderExecutionError) as exc:
            code = exc.code if isinstance(exc, ProviderExecutionError) else "INVALID_STAGE_OUTPUT"
            if code not in repair_codes or getattr(exc, "outcome", "known") != "known":
                raise
            repaired = request.model_copy(update={
                "developer_instruction": request.developer_instruction
                + "\n这是该审校阶段唯一一次格式与证据修复。上次返回未通过校验：" + code
                + "。只输出符合 schema 的对象。predicate 必须是字符串，不适用时为空字符串；"
                "不要用 null 替代字符串或列表。所有证据必须是输入正文或允许参考中的逐字子串。"
                "entity_id 只能使用参考中真实存在的实体 ID，否则留 null；"
                "related_entity_ids 无可用实体时为空列表。memory_candidates 是可选项，"
                "无法准确核对字符位置和有效资源 ID 时必须省略，不猜测。"
                "仅报告正文中实际存在且可举证的问题，不把缺失可选设定当作正文错误。",
            })
            return execute(repaired, request.task + ".repair")

    def _run_full_pipeline(
        self, session: Session, job: AIJob, contract: dict[str, Any], snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        plan = self._structured(
            self._initial_request("full_chapter", job.instructions, contract, snapshot),
        ).data
        draft = self._text(
            self._request(
                "draft",
                AUTHOR_INSTRUCTION,
                "根据场景计划写出连续初稿。\n"
                + json.dumps(plan, ensure_ascii=False)
                + "\n"
                + job.instructions,
                contract,
                snapshot,
            )
        ).text
        continuity = self._structured(
            self._request("continuity_review", CONTINUITY_INSTRUCTION, draft, contract, snapshot),
        ).data
        style = self._structured(
            self._request("style_review", STYLE_INSTRUCTION, draft, contract, snapshot),
        ).data
        needs_rewrite = bool(continuity["issues"] or style["issues"])
        resolution = (
            self._structured(
                self._request(
                    "resolve",
                    RESOLVER_INSTRUCTION,
                    json.dumps({"continuity": continuity, "style": style}, ensure_ascii=False),
                    contract,
                    snapshot,
                ),
            ).data
            if needs_rewrite
            else {"options": [], "skipped": "no_issues"}
        )
        if needs_rewrite:
            conservative = next(
                option for option in resolution["options"] if option["mode"] == "conservative"
            )
            rewritten = self._text(
                self._request(
                    "rewrite",
                    AUTHOR_INSTRUCTION,
                    f"作者要求：{job.instructions}\n统一重写初稿。仅采用以下保守方案；不得采用平衡或激进方案，重大改动保留给作者决定。"
                    f"\n初稿：\n{draft}\n审校：{continuity}\n{style}\n保守方案：{conservative}",
                    contract,
                    snapshot,
                )
            ).text
            final_review = self._structured(
                self._request(
                    "final_review", FINAL_REVIEW_INSTRUCTION, rewritten, contract, snapshot
                ),
            ).data
        else:
            rewritten = draft
            final_review = continuity
            if self.stage_runner:
                self.stage_runner.skip("resolve", "no_issues")
                self.stage_runner.skip("rewrite", "no_issues")
                self.stage_runner.skip("final_review", "unchanged_draft")
        job.context_snapshot = snapshot
        self._persist_review(
            session, job, continuity, draft, stage="初稿连续性", task="continuity_review"
        )
        self._persist_review(session, job, style, draft, stage="初稿文风", task="style_review")
        if needs_rewrite:
            self._persist_review(
                session, job, final_review, rewritten, stage="最终候选综合审校", task="final_review"
            )
        self._persist_rule_conflicts(
            session, job, rewritten, contract, final_review["observations"]
        )
        return {
            "stage_order": [
                "plan",
                "draft",
                "continuity_review",
                "style_review",
                *(["resolve", "rewrite", "final_review"] if needs_rewrite else []),
            ],
            "plan": plan,
            "continuity_review": continuity,
            "style_review": style,
            "resolution": resolution,
            "final_review": final_review,
            "candidate_text": rewritten,
            "pending_canon_changes": [],
        }

    def _run_single(
        self,
        session: Session,
        job: AIJob,
        task_type: str,
        contract: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        if task_type == "chat":
            reply = self._text(
                self._initial_request(task_type, job.instructions, contract, snapshot)
            ).text
            return {"reply": reply, "stage_order": ["chat"]}
        if task_type in {"draft", "rewrite", "scene_description", "continue"}:
            text = self._text(
                self._initial_request(task_type, job.instructions, contract, snapshot)
            ).text
            return {"stage_order": [task_type], "candidate_text": text}
        data = self._structured(
            self._initial_request(task_type, job.instructions, contract, snapshot),
        ).data
        if task_type == "review":
            job.context_snapshot = snapshot
            draft = next(
                (
                    f["content"]
                    for f in snapshot["fragments"]
                    if f["source_type"] == "current_draft"
                ),
                "",
            )
            self._persist_review(session, job, data, draft)
            self._persist_rule_conflicts(session, job, draft, contract, data["observations"])
        return {"stage_order": [task_type], "output": data}

    def _persist_rule_conflicts(
        self, session: Session, job: AIJob, draft: str, contract: dict[str, Any], observations=None
    ) -> None:
        if self.stage_runner is not None and session is None:
            self.pending_reviews.append(
                ("_persist_rule_conflicts", (draft, contract, observations), {})
            )
            return
        if not job.chapter_id:
            return
        facts = session.scalars(
            select(CanonFact).where(CanonFact.project_id == job.project_id)
        ).all()
        entities = session.scalars(select(Entity).where(Entity.project_id == job.project_id)).all()
        threads = session.scalars(
            select(PlotThread).where(PlotThread.project_id == job.project_id)
        ).all()
        from novel_harness.services.retrieval import fact_applies, ordered_nodes

        positions = {node.id: i for i, node in enumerate(ordered_nodes(session))}
        observed = observations or []
        entity_ids = {entity.id for entity in entities}
        for item in observed:
            if item["evidence"] not in draft or (
                item["entity_id"] and item["entity_id"] not in entity_ids
            ):
                raise ProviderExecutionError("审校观察缺少当前正文证据或引用了不可用人物。")
        timelines = session.scalars(select(TimelineEvent)).all()
        previous_times = [
            event.sort_key
            for event in timelines
            if event.chapter_id in positions
            and positions[event.chapter_id] < positions[job.chapter_id]
        ]
        findings = self.continuity_checker.check(
            ContinuityInput(
                draft=draft,
                chapter_id=job.chapter_id,
                current_node_order=positions.get(job.chapter_id, 0),
                actual_pov_entity_id=next(
                    (item["entity_id"] for item in observed if item["kind"] == "pov"), None
                ),
                acting_entity_ids=[
                    item["entity_id"]
                    for item in observed
                    if item["kind"] == "action" and item["entity_id"]
                ],
                draft_fact_candidates=[
                    {
                        "subject_entity_id": item["entity_id"],
                        "predicate": item["predicate"],
                        "value": item["value"],
                    }
                    for item in observed
                    if item["kind"] == "fact"
                ],
                current_timeline_sort_key=next(
                    (
                        item["value"]
                        for item in observed
                        if item["kind"] == "timeline" and type(item["value"]) is int
                    ),
                    None,
                ),
                previous_timeline_sort_key=max(previous_times, default=None),
                contract=contract,
                canon_facts=[
                    serialize(item)
                    for item in facts
                    if fact_applies(serialize(item), job.chapter_id, positions)
                ],
                entities=[
                    serialize_entity_for_chapter(session, item, job.chapter_id)
                    for item in entities
                ],
                plot_threads=[
                    {**serialize(item), "due_order": positions.get(item.due_node_id)}
                    for item in threads
                ],
            )
        )
        for finding in findings:
            conflict = create_conflict(
                session,
                job.chapter_id,
                {
                    "code": finding.code,
                    "severity": finding.severity,
                    "message": ("AI 观察辅助检查（待作者判断）：" if observed else "")
                    + finding.message,
                    "evidence": [
                        f"ai_job:{job.id}",
                        f"document_revision:{job.context_snapshot.get('source_revision')}",
                    ]
                    + finding.evidence,
                    "related_entity_ids": finding.related_ids,
                },
            )
            suggest_options(session, conflict.id)

    def _persist_review(self, session, job, review, draft, stage="当前正文", task="review"):
        if self.stage_runner is not None and session is None:
            self.pending_reviews.append(
                ("_persist_review", (review, draft), {"stage": stage, "task": task})
            )
            return
        actual = job.context_snapshot.get("stage_inputs", {}).get(task)
        included = (
            {(item["source_type"], item["source_id"]) for item in actual["included_sources"]}
            if actual
            else None
        )
        references = [
            f["content"]
            for f in job.context_snapshot["fragments"]
            if included is None
            or f["source_type"] == "chapter_contract"
            or (f["source_type"], f["source_id"]) in included
        ]
        references.extend(
            (f.get("citation") or {}).get("title", f.get("reason", ""))
            for f in job.context_snapshot["fragments"]
            if f["source_type"] != "chapter_contract"
            and (included is None or (f["source_type"], f["source_id"]) in included)
        )
        valid_entities = {
            f["source_id"]
            for f in job.context_snapshot["fragments"]
            if f["source_type"] in {"entity", "story_entity"}
            and (included is None or (f["source_type"], f["source_id"]) in included)
        }
        from novel_harness.services.review_validation import validate_review

        validate_review(review, draft, references, valid_entities)
        for issue in review["issues"]:
            conflict = create_conflict(
                session,
                job.chapter_id,
                {
                    **issue,
                    "message": f"AI 审校（{stage}，待作者判断）：" + issue["message"],
                    "evidence": [
                        f"ai_job:{job.id}",
                        f"document_revision:{job.context_snapshot.get('source_revision')}",
                    ]
                    + issue["evidence"],
                },
            )
            suggest_options(session, conflict.id)
