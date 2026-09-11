"""Project-local durable commands and short transaction boundaries."""

import re
from contextlib import contextmanager
from copy import deepcopy

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text

from novel_harness.ai.prompts import prompt_version_for
from novel_harness.db.job_models import (
    AIJobActionReceipt,
    AIJobControl,
    AIJobStageAttempt,
    JobSchemaVersion,
)
from novel_harness.db.models import AIJob, GenerationArtifact
from novel_harness.services.job_state import (
    ACTIVE,
    REPLACEABLE,
    JobFence,
    command_hash,
    recovery_action,
    replacement_requires_confirmation,
)
from novel_harness.services.serialization import serialize


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise HTTPException(422, detail={"code": "INVALID_IDEMPOTENCY_KEY"})


class JobStore:
    def __init__(self, database, project_id):
        self.database = database
        self.project_id = project_id

    def enqueue(self, command, key, prepare, *, reuse=None):
        validate_key(key)
        command = deepcopy(command)
        if command["project_id"] != self.project_id:
            raise HTTPException(404, detail={"code": "PROJECT_NOT_FOUND"})
        if command["task_type"] == "quality_workflow":
            operation = "quality"
        elif command["task_type"] == "wiki_summary":
            operation = "wiki"
        elif command["task_type"] == "chapter_summary":
            operation = "chapter_summary"
        elif command["task_type"] in {"preparation_analysis", "preparation_followup"}:
            operation = "preparation"
        else:
            operation = "writing"
        digest = command_hash(command)
        with self.write() as session:
            # Reserved ':' namespace cannot collide with public resume keys.
            receipt_key = f"enqueue:{operation}:{command_hash({'key': key})}"
            receipt = session.scalar(select(AIJobActionReceipt).where(
                AIJobActionReceipt.idempotency_key == receipt_key
            )) if reuse else None
            if receipt:
                if receipt.request_hash != digest:
                    raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"})
                return self.serialize_in_session(session, receipt.job_id)
            prior = session.scalar(
                select(AIJobControl).where(
                    AIJobControl.operation == operation,
                    AIJobControl.idempotency_key == key,
                )
            )
            if prior is not None:
                if prior.request_hash != digest:
                    raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"})
                response = self.serialize_in_session(session, prior.job_id)
            elif reuse and (reused_id := reuse(session)):
                session.add(AIJobActionReceipt(
                    job_id=reused_id, idempotency_key=receipt_key,
                    request_hash=digest, response={"id": reused_id},
                ))
                response = self.serialize_in_session(session, reused_id)
            else:
                from novel_harness.services import conversation_threads
                thread = None
                if operation == "writing":
                    thread = conversation_threads.assert_writable(
                        session, self.project_id, command["chapter_id"],
                        command.get("conversation_id"),
                    )
                if (
                    command["task_type"] == "chapter_summary"
                    and command.get("replaces_job_id") is None
                ):
                    blocker = self._summary_blocker(session, command["chapter_id"])
                    if blocker:
                        raise HTTPException(
                            409,
                            detail={"code": "SUMMARY_TASK_ACTIVE", "job_id": blocker},
                        )
                self._validate_replacement(session, command)
                prepared = deepcopy(prepare(session))
                job = AIJob(
                    project_id=self.project_id,
                    chapter_id=command["chapter_id"],
                    task_type=command["task_type"],
                    status="queued",
                    instructions=command["instructions"],
                    token_budget=command["token_budget"],
                    prompt_version=prompt_version_for(command["task_type"]),
                    context_snapshot={},
                    result={},
                )
                session.add(job)
                session.flush()
                if thread is not None and command.get("conversation_id") is not None:
                    from novel_harness.db.models import ConversationJob
                    conversation_threads.materialize(session, thread)
                    session.add(ConversationJob(job_id=job.id, conversation_id=thread.id))
                session.add(
                    AIJobControl(
                        job_id=job.id,
                        operation=operation,
                        idempotency_key=key,
                        request_hash=digest,
                        command=command,
                        **prepared,
                    )
                )
                session.flush()
                response = self.serialize_in_session(session, job.id)
        return response

    def _summary_blocker(self, session, chapter_id, exclude_job_id=None):
        statement = (
            select(AIJob.id, AIJobControl.effects)
            .outerjoin(AIJobControl, AIJobControl.job_id == AIJob.id)
            .where(
                AIJob.project_id == self.project_id,
                AIJob.chapter_id == chapter_id,
                AIJob.task_type == "chapter_summary",
                AIJob.status.in_(ACTIVE | {"recovery_required", "failed"}),
            )
        )
        if exclude_job_id is not None:
            statement = statement.where(AIJob.id != exclude_job_id)
        for job_id, effects in session.execute(statement.order_by(AIJob.created_at, AIJob.id)):
            summary_id = effects.get("summary_id") if isinstance(effects, dict) else None
            if isinstance(summary_id, str) and summary_id.strip():
                continue
            return job_id
        return None

    def _validate_replacement(self, session, command):
        source_id = command.get("replaces_job_id")
        if source_id is None:
            return
        source = self._job(session, source_id)
        if source.chapter_id != command["chapter_id"] or source.task_type != command["task_type"]:
            raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
        from novel_harness.services import conversation_threads
        if source.task_type in conversation_threads.TASKS:
            expected_thread = command.get("conversation_id") or conversation_threads.default_id(
                self.project_id, command["chapter_id"],
            )
            if conversation_threads.job_conversation_id(session, source) != expected_thread:
                raise HTTPException(409, detail={"code": "CONVERSATION_REPLACEMENT_MISMATCH"})
        control = session.get(AIJobControl, source_id)
        if control and control.effects.get("summary_id"):
            raise HTTPException(409, detail={"code": "SUMMARY_ALREADY_PUBLISHED"})
        if source.status not in REPLACEABLE:
            raise HTTPException(409, detail={"code": "JOB_NOT_REPLACEABLE"})
        if source.task_type == "chapter_summary":
            if source.status != "cancelled":
                raise HTTPException(409, detail={"code": "SUMMARY_REPLACEMENT_CANCEL_REQUIRED"})
            other = self._summary_blocker(session, source.chapter_id, exclude_job_id=source_id)
            if other:
                raise HTTPException(
                    409,
                    detail={"code": "SUMMARY_TASK_ACTIVE", "job_id": other},
                )
        latest = session.scalar(select(AIJobStageAttempt).where(
            AIJobStageAttempt.job_id == source_id,
        ).order_by(AIJobStageAttempt.created_at.desc(), AIJobStageAttempt.attempt_no.desc()))
        if (replacement_requires_confirmation(control, latest)
                and not command.get("confirm_unknown")):
            raise HTTPException(409, detail={"code": "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"})

    def _job(self, session, job_id):
        job = session.get(AIJob, job_id)
        if job is None or job.project_id != self.project_id:
            raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
        return job

    @contextmanager
    def write(self):
        with self.database.job_session_scope() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            version = session.get(JobSchemaVersion, "durable_jobs")
            if version is None or version.version != 1:
                raise HTTPException(503, detail={"code": "JOB_SCHEMA_UNSUPPORTED"})
            yield session

    def assert_running(self, session, fence):
        job = self._job(session, fence.job_id)
        control = session.get(AIJobControl, job.id)
        if (
            control is None
            or control.format_version != 1
            or job.prompt_version != prompt_version_for(job.task_type)
            or control.worker_epoch != fence.epoch
            or control.control_revision != fence.control_revision
            or control.cancel_requested
            or job.status != "running"
        ):
            raise HTTPException(409, detail={"code": "JOB_FENCE_CHANGED"})
        from novel_harness.services.conversation_threads import assert_job_writable
        assert_job_writable(session, job)
        return job, control

    def claim(self, job_id, epoch):
        with self.write() as session:
            job = self._job(session, job_id)
            control = session.get(AIJobControl, job_id)
            if (
                job.status != "queued"
                or control is None
                or control.cancel_requested
                or control.format_version != 1
                or job.prompt_version != prompt_version_for(job.task_type)
            ):
                return None
            job.status = "running"
            control.control_revision += 1
            control.worker_epoch = epoch
            return JobFence(job_id, epoch, control.control_revision)

    @staticmethod
    def _latest(session, job_id, stage_key):
        return session.scalar(
            select(AIJobStageAttempt)
            .where(
                AIJobStageAttempt.job_id == job_id,
                AIJobStageAttempt.stage_key == stage_key,
            )
            .order_by(AIJobStageAttempt.attempt_no.desc())
        )

    def begin_attempt(self, fence, stage_key, payload):
        digest = command_hash(payload)
        with self.write() as session:
            _, control = self.assert_running(session, fence)
            previous = self._latest(session, fence.job_id, stage_key)
            number = previous.attempt_no + 1 if previous else 1
            if previous:
                if previous.input_hash != digest:
                    raise HTTPException(409, detail={"code": "STAGE_INPUT_CHANGED"})
                if previous.status == "prepared":
                    return previous.attempt_no
                safe_retry = previous.outcome == "not_sent" and previous.attempt_no == 1
                authorized = (
                    control.authorized_stage == stage_key
                    and control.authorized_attempt_no == number
                )
                if previous.status not in {"failed", "result_unknown"} or not (
                    safe_retry or authorized
                ):
                    raise HTTPException(409, detail={"code": "STAGE_REPLAY_NOT_AUTHORIZED"})
                if authorized:
                    control.authorized_stage = None
                    control.authorized_attempt_no = None
            session.add(
                AIJobStageAttempt(
                    job_id=fence.job_id,
                    stage_key=stage_key,
                    attempt_no=number,
                    status="prepared",
                    input_hash=digest,
                    input_payload=deepcopy(payload),
                )
            )
            return number

    def dispatch(self, fence, stage_key, attempt_no):
        with self.write() as session:
            self.assert_running(session, fence)
            attempt = session.get(AIJobStageAttempt, (fence.job_id, stage_key, attempt_no))
            if attempt is None or attempt.status != "prepared":
                raise HTTPException(409, detail={"code": "INVALID_STAGE_DISPATCH"})
            attempt.status = "dispatched"

    def finish_attempt(self, fence, stage_key, attempt_no, output, *, skipped=False):
        with self.write() as session:
            self.assert_running(session, fence)
            attempt = session.get(AIJobStageAttempt, (fence.job_id, stage_key, attempt_no))
            expected = "prepared" if skipped else "dispatched"
            if attempt is None or attempt.status != expected:
                raise HTTPException(409, detail={"code": "INVALID_STAGE_COMPLETION"})
            artifact = GenerationArtifact(
                job_id=fence.job_id,
                stage=stage_key,
                kind="checkpoint",
                content="",
                data=deepcopy(output),
            )
            session.add(artifact)
            session.flush()
            attempt.artifact_id = artifact.id
            attempt.status = "skipped" if skipped else "succeeded"
            attempt.outcome = "known"

    def fail_attempt(
        self,
        fence,
        stage_key,
        attempt_no,
        code,
        outcome,
        *,
        terminal=True,
        metrics=None,
    ):
        with self.write() as session:
            job, control = self.assert_running(session, fence)
            attempt = session.get(AIJobStageAttempt, (fence.job_id, stage_key, attempt_no))
            if attempt is None or attempt.status not in {"prepared", "dispatched", "failed"}:
                raise HTTPException(409, detail={"code": "INVALID_STAGE_FAILURE"})
            attempt.status = "result_unknown" if outcome == "unknown" else "failed"
            attempt.error_code = code
            attempt.outcome = outcome
            if metrics:
                execution = deepcopy(job.result.get("execution", {}))
                stages = deepcopy(execution.get("stages", {}))
                # Checkpoints survive even when a later call has no usable response.
                completed = session.execute(select(AIJobStageAttempt, GenerationArtifact).join(
                    GenerationArtifact, AIJobStageAttempt.artifact_id == GenerationArtifact.id,
                ).where(AIJobStageAttempt.job_id == fence.job_id,
                        AIJobStageAttempt.status == "succeeded"))
                for done, artifact in completed:
                    value = artifact.data
                    if "execution" in value:
                        stages[done.stage_key] = {
                            **value["execution"], "cached": False, "status": "succeeded",
                            "error_code": None, "usage": value.get("usage", {}),
                            "usage_source": value.get("usage_source", "unavailable"),
                        }
                stages[stage_key] = deepcopy(metrics)
                job.result = {
                    **job.result,
                    "execution": {**execution, "stages": stages},
                }
            if terminal:
                job.status = "recovery_required" if outcome == "unknown" else "failed"
                job.error_code = code
                from novel_harness.ai.base import PROVIDER_FAILURE_MESSAGES

                job.error_message = PROVIDER_FAILURE_MESSAGES.get(
                    code, "阶段执行未完成，请查看任务状态后决定是否恢复。"
                )
                control.recovery_reason = (
                    "result_unknown" if outcome == "unknown" else "stage_failed"
                )

    def completed(self, job_id, stage_key, input_hash):
        with self.database.job_session_scope() as session:
            self._job(session, job_id)
            attempt = self._latest(session, job_id, stage_key)
            if attempt is None:
                return None
            if attempt.input_hash != input_hash:
                raise HTTPException(409, detail={"code": "STAGE_INPUT_CHANGED"})
            if attempt.status not in {"succeeded", "skipped"}:
                return None
            artifact = session.get(GenerationArtifact, attempt.artifact_id)
            if artifact is None:
                raise HTTPException(409, detail={"code": "CHECKPOINT_MISSING"})
            return deepcopy(artifact.data)

    def known_failure(self, job_id, stage_key, input_hash, codes):
        """Reuse a rejected response when resuming its separate repair checkpoint."""
        with self.database.job_session_scope() as session:
            job = self._job(session, job_id)
            attempt = self._latest(session, job_id, stage_key)
            if attempt is None:
                return None
            if attempt.input_hash != input_hash:
                raise HTTPException(409, detail={"code": "STAGE_INPUT_CHANGED"})
            if (attempt.status == "failed" and attempt.outcome == "known"
                    and attempt.error_code in codes):
                metrics = job.result.get("execution", {}).get("stages", {}).get(stage_key, {})
                return attempt.error_code, deepcopy(metrics)
            return None

    def publish(self, fence, apply, *, terminal=True):
        with self.write() as session:
            job, _ = self.assert_running(session, fence)
            apply(session, job)
            if terminal:
                job.status = "succeeded"
                job.error_code = None
                job.error_message = None

    def pause(self, fence, reason, *, failed=False):
        with self.write() as session:
            job, control = self.assert_running(session, fence)
            job.status = "failed" if failed else "recovery_required"
            job.error_code = reason
            job.error_message = {
                "QUALITY_REVIEW_REQUIRED": (
                    "本章优化后仍需作者确认，写作会话已暂停。请查看质量报告后决定是否继续。"
                ),
                "QUALITY_SOURCE_CHANGED": (
                    "正文、章节顺序或参考资料已变化。原候选仍保留，"
                    "请取消任务并基于最新内容重新开始。"
                ),
            }.get(reason, "任务已暂停，正文和已保存结果未被覆盖。")
            control.recovery_reason = reason

    def cancel(self, job_id):
        with self.write() as session:
            job = self._job(session, job_id)
            control = session.get(AIJobControl, job_id)
            if control and job.status in {"queued", "running", "recovery_required", "failed"}:
                control.cancel_requested = True
                control.control_revision += 1
                job.status = "cancel_requested" if job.status == "running" else "cancelled"
            elif control is None and job.status in {"recovery_required", "failed"}:
                job.status = "cancelled"
            session.flush()
            return self.serialize_in_session(session, job_id)

    def finish_cancellation(self, job_id):
        with self.write() as session:
            job = self._job(session, job_id)
            if job.status == "cancel_requested":
                job.status = "cancelled"

    def resume(self, job_id, key, expected_revision, confirm_unknown, validate=None):
        validate_key(key)
        digest = command_hash(
            {"expected_control_revision": expected_revision, "confirm_unknown": confirm_unknown}
        )
        with self.write() as session:
            job = self._job(session, job_id)
            prior = session.get(AIJobActionReceipt, (job_id, key))
            if prior:
                if prior.request_hash != digest:
                    raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"})
                return deepcopy(prior.response)
            control = session.get(AIJobControl, job_id)
            if control is None or job.status not in {"recovery_required", "failed"}:
                raise HTTPException(409, detail={"code": "JOB_NOT_RESUMABLE"})
            from novel_harness.services.conversation_threads import assert_job_writable
            assert_job_writable(session, job)
            if (
                control.format_version != 1
                or job.prompt_version != prompt_version_for(job.task_type)
            ):
                raise HTTPException(409, detail={"code": "JOB_FORMAT_UNSUPPORTED"})
            if control.control_revision != expected_revision:
                raise HTTPException(409, detail={"code": "JOB_CONTROL_CHANGED"})
            attempts = session.scalars(
                select(AIJobStageAttempt)
                .where(
                    AIJobStageAttempt.job_id == job_id,
                )
                .order_by(AIJobStageAttempt.created_at.desc(), AIJobStageAttempt.attempt_no.desc())
            ).all()
            latest = {}
            for attempt in attempts:
                if attempt.stage_key not in latest:
                    latest[attempt.stage_key] = attempt
            requires_confirmation = replacement_requires_confirmation(control) or any(
                replacement_requires_confirmation(control, attempt) for attempt in latest.values()
            )
            if requires_confirmation and not confirm_unknown:
                raise HTTPException(409, detail={"code": "UNKNOWN_RESULT_CONFIRMATION_REQUIRED"})
            if validate:
                validate(session, control)
            blocked = next(
                (
                    a
                    for a in latest.values()
                    if a.status in {"failed", "result_unknown", "dispatched"}
                ),
                None,
            )
            if blocked:
                # Legacy guard pauses may retain only dispatch evidence. Normalize
                # under this write lock, after confirmation and source validation;
                # begin_attempt still requires the exact authorized next attempt.
                if blocked.status == "dispatched":
                    blocked.status = "result_unknown"
                    blocked.outcome = "unknown"
                control.authorized_stage = blocked.stage_key
                control.authorized_attempt_no = blocked.attempt_no + 1
            job.status = "queued"
            job.error_code = None
            job.error_message = None
            control.worker_epoch = None
            control.control_revision += 1
            control.recovery_reason = None
            control.cancel_requested = False
            session.flush()
            response = jsonable_encoder(self.serialize_in_session(session, job_id))
            session.add(
                AIJobActionReceipt(
                    job_id=job_id, idempotency_key=key, request_hash=digest, response=response
                )
            )
            return response

    @staticmethod
    def local_checkpoint(session, job, stage, payload):
        if JobStore._latest(session, job.id, stage):
            return
        artifact = GenerationArtifact(
            job_id=job.id, stage=stage, kind="local", content="", data=deepcopy(payload)
        )
        session.add(artifact)
        session.flush()
        session.add(
            AIJobStageAttempt(
                job_id=job.id,
                stage_key=stage,
                attempt_no=1,
                status="succeeded",
                input_hash=command_hash(payload),
                input_payload=payload,
                artifact_id=artifact.id,
                outcome="known",
            )
        )

    @staticmethod
    def _recover_in_session(session, job, control):
        """Classify one candidate while the caller holds ``self.write()``."""
        if control is None:
            job.status = "recovery_required"
            job.error_code = "LEGACY_JOB_UNRECOVERABLE"
            job.error_message = "旧任务缺少发送检查点，不能自动恢复；请由作者决定是否新建任务。"
            return
        if control.cancel_requested or job.status == "cancel_requested":
            job.status = "cancelled"
        elif (
            control.format_version != 1
            or job.prompt_version != prompt_version_for(job.task_type)
        ):
            job.status = "recovery_required"
            control.recovery_reason = "unsupported_format"
        else:
            attempts = session.scalars(
                select(AIJobStageAttempt).where(AIJobStageAttempt.job_id == job.id)
            ).all()
            latest = {}
            for attempt in attempts:
                if (
                    attempt.stage_key not in latest
                    or attempt.attempt_no > latest[attempt.stage_key].attempt_no
                ):
                    latest[attempt.stage_key] = attempt
            unknown = [
                attempt
                for attempt in latest.values()
                if recovery_action(attempt.status, False) == "pause_unknown"
                and not (
                    control.authorized_stage == attempt.stage_key
                    and control.authorized_attempt_no == attempt.attempt_no + 1
                )
            ]
            failed = [
                attempt
                for attempt in latest.values()
                if attempt.status == "failed"
                and not (
                    attempt.outcome == "known"
                    and (
                        attempt.stage_key in {"review", "continuity_review", "style_review",
                                              "final_review"}
                        and attempt.stage_key + ".repair" in latest
                        or attempt.stage_key.startswith("chapter_summary")
                        and attempt.stage_key + ".evidence_repair" in latest
                    )
                )
                and not (
                    control.context_ready
                    and attempt.stage_key == "context_embedding"
                    and attempt.outcome == "known"
                )
                and not (
                    control.authorized_stage == attempt.stage_key
                    and control.authorized_attempt_no == attempt.attempt_no + 1
                )
            ]
            if unknown:
                for attempt in unknown:
                    attempt.status = "result_unknown"
                    attempt.outcome = "unknown"
                job.status = "recovery_required"
                control.recovery_reason = "result_unknown"
            elif failed and not all(
                attempt.outcome == "not_sent" and attempt.attempt_no == 1
                for attempt in failed
            ):
                job.status = "failed"
                control.recovery_reason = "stage_failed"
            else:
                job.status = "queued"
        control.control_revision += 1
        control.worker_epoch = None

    def recover(self, epoch):
        """Only the exclusive executor owner may call this on startup."""
        with self.write() as session:
            jobs = session.scalars(
                select(AIJob).where(
                    AIJob.project_id == self.project_id,
                    AIJob.status.in_(["running", "cancel_requested", "queued"]),
                )
            ).all()
            for job in jobs:
                control = session.get(AIJobControl, job.id)
                if control is not None and control.worker_epoch == epoch:
                    continue
                self._recover_in_session(session, job, control)

    def reconcile_owned(self, epoch):
        """Converge work abandoned by this live executor between scan iterations."""
        with self.write() as session:
            if not epoch:
                return
            jobs = session.scalars(
                select(AIJob).where(
                    AIJob.project_id == self.project_id,
                    AIJob.status.in_(["running", "cancel_requested"]),
                )
            ).all()
            for job in jobs:
                control = session.get(AIJobControl, job.id)
                if control is not None and control.worker_epoch != epoch:
                    continue
                self._recover_in_session(session, job, control)

    def read(self, job_id):
        with self.database.job_session_scope() as session:
            return self.serialize_in_session(session, job_id)

    def serialize_in_session(self, session, job_id):
        job = self._job(session, job_id)
        control = session.get(AIJobControl, job_id)
        latest = session.scalar(
            select(AIJobStageAttempt)
            .where(
                AIJobStageAttempt.job_id == job_id,
            )
            .order_by(AIJobStageAttempt.created_at.desc(), AIJobStageAttempt.attempt_no.desc())
        )
        actions = []
        if control:
            if job.status in {
                "queued",
                "running",
                "cancel_requested",
                "recovery_required",
                "failed",
            }:
                actions.append("cancel")
            if (
                job.status in {"recovery_required", "failed"}
                and control.format_version == 1
                and job.prompt_version == prompt_version_for(job.task_type)
                and not (
                    job.task_type == "quality_workflow" and control.effects.get("accepted_chapters")
                )
            ):
                actions.append("resume")
        elif job.status in {"recovery_required", "failed"}:
            actions.append("cancel")
        if (
            job.status in REPLACEABLE
            and job.task_type != "quality_workflow"
            and not (control and control.effects.get("summary_id"))
        ):
            actions.append("replace")
        from novel_harness.services import conversation_threads
        conversation_id = conversation_threads.job_conversation_id(session, job)
        if conversation_id:
            try:
                conversation_threads.assert_job_writable(session, job)
            except HTTPException:
                actions = [action for action in actions if action not in {"resume", "replace"}]
        return {
            **serialize(job),
            "conversation_id": conversation_id,
            "control_revision": control.control_revision if control else 0,
            "status_url": f"/api/v1/projects/{self.project_id}/ai/jobs/{job_id}",
            "current_stage": latest.stage_key if latest else None,
            "recovery_reason": control.recovery_reason if control else None,
            "allowed_actions": actions,
            "effects": deepcopy(control.effects) if control else {},
            "replaces_job_id": control.command.get("replaces_job_id") if control else None,
            "replacement_requires_confirmation": replacement_requires_confirmation(control, latest),
        }
