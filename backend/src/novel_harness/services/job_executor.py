"""One local, serial executor. SQLite is the queue; wakeups are only hints."""

import logging
from threading import Event, Lock, Thread
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from novel_harness.ai.base import ContextBudgetError
from novel_harness.db.job_models import AIJobControl
from novel_harness.db.models import AIJob, ImportBatch
from novel_harness.services.chapter_summaries import generate_summary
from novel_harness.services.import_analysis import (
    claim_import_unit,
    peek_import_unit,
    recover_current_epoch_orphans,
    recover_import_units,
    run_import_unit,
)
from novel_harness.services.job_store import JobStore
from novel_harness.services.pipeline import CreationPipeline
from novel_harness.services.preparation_generation import generate_preparation

logger = logging.getLogger(__name__)


class LocalJobExecutor:
    def __init__(self, app):
        self.app = app
        self.epoch = str(uuid4())
        self.stopping = Event()
        self.notification = Event()
        self.execution = Lock()
        self.thread = None
        self.initialized = set()

    def wake(self):
        self.notification.set()

    def prepare_vault(self, vault):
        store = JobStore(vault.database, vault.project_id)
        if vault.project_id not in self.initialized:
            vault.database.create_schema()
            store.recover(self.epoch)
            recover_import_units(vault.database, vault.project_id, self.epoch)
            self.initialized.add(vault.project_id)
        return store

    def initialize(self):
        """Prepare metadata before serving requests, without inference or RAG rebuilds."""
        registry = self.app.state.vault_registry
        for record in registry.list_records():
            try:
                self.prepare_vault(registry.require(record["id"], job_only=True))
            except Exception as exc:
                logger.warning(
                    "Vault initialization failed vault_id=%s error=%s",
                    record["id"],
                    exc,
                    exc_info=True,
                )

    def start(self):
        self.thread = Thread(target=self._loop, name="novel-local-executor", daemon=False)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake()
        if self.thread:
            self.thread.join()  # Ownership must outlive every possible publication.

    def _loop(self):
        idle_seconds = 1
        while not self.stopping.is_set():
            self.notification.clear()
            try:
                if self.run_once():
                    idle_seconds = 1
                    continue
            except Exception:
                logger.warning("Task scan failed; will retry locally.")
            notified = self.notification.wait(idle_seconds)
            idle_seconds = 1 if notified else min(30, idle_seconds * 2)

    def run_once(self):
        if self.stopping.is_set() or not self.execution.acquire(blocking=False):
            return False
        try:
            candidates = []
            blocked_projects: set[str] = set()
            registry = self.app.state.vault_registry
            for record in registry.list_records():
                try:
                    vault = registry.require(record["id"])
                    store = self.prepare_vault(vault)
                    try:
                        recover_current_epoch_orphans(
                            vault.database, vault.project_id, self.epoch
                        )
                    except OperationalError:
                        blocked_projects.add(record["id"])
                        logger.warning(
                            "Import recovery is busy vault_id=%s; "
                            "skipping this vault for the tick.",
                            record["id"],
                        )
                        continue
                    store.reconcile_owned(self.epoch)
                    with store.database.job_session_scope() as session:
                        job = session.scalar(
                            select(AIJob)
                            .where(AIJob.status == "queued")
                            .order_by(AIJob.created_at, AIJob.id)
                            .limit(1)
                        )
                        if job:
                            candidates.append((job.created_at, job.id, "job", store, vault))
                    import_unit = peek_import_unit(vault.database, vault.project_id)
                    if import_unit:
                        candidates.append((import_unit[0], import_unit[1], "import", store, vault))
                except Exception as exc:
                    logger.warning(
                        "Vault scan failed vault_id=%s error=%s; other projects remain available.",
                        record["id"],
                        exc,
                        exc_info=True,
                    )
            if not candidates or self.stopping.is_set():
                return False
            _, work_id, work_kind, store, vault = min(candidates, key=lambda item: item[:2])
            if work_kind == "import":
                fence = claim_import_unit(vault.database, vault.project_id, self.epoch, work_id)
                if fence is None:
                    return False
                with vault.database.job_session_scope() as session:
                    batch = session.get(ImportBatch, fence.import_batch_id)
                    identity = dict(batch.analysis_provider_identity) if batch else {}
                settings = self.app.state.model_settings

                def validate_import_provider():
                    if not identity or settings.identity() != identity:
                        raise HTTPException(409, detail={"code": "PROVIDER_CHANGED"})

                def import_factory():
                    return settings.provider_for_identity(identity, self.app.state.ai_provider)

                run_import_unit(vault, fence, import_factory, validate_import_provider)
                return True

            job_id = work_id
            fence = store.claim(job_id, self.epoch)
            if fence is None:
                return False
            try:
                with store.database.job_session_scope() as session:
                    job = store._job(session, job_id)
                    control = session.get(AIJobControl, job_id)
                    identity = control.provider_identity
                    embedding_identity = control.embedding_identity
                settings = self.app.state.model_settings

                def validate_provider():
                    if settings.identity() != identity:
                        raise HTTPException(409, detail={"code": "PROVIDER_CHANGED"})
                    embedding = self.app.state.embedding_provider
                    current = (
                        {"endpoint": embedding.endpoint, "model": embedding.model}
                        if embedding
                        else None
                    )
                    if (
                        job.task_type
                        not in {"chapter_summary", "preparation_analysis", "preparation_followup", "wiki_summary"}
                        and current != embedding_identity
                    ):
                        raise HTTPException(409, detail={"code": "EMBEDDING_CHANGED"})

                def factory(observer):
                    return settings.provider_for_identity(identity, self.app.state.ai_provider)

                if job.task_type == "wiki_summary":
                    from novel_harness.services.wiki_generation import generate_wiki
                    generate_wiki(store, fence, factory, validate_provider=validate_provider)
                elif job.task_type == "chapter_summary":
                    generate_summary(store, fence, factory, validate_provider=validate_provider)
                elif job.task_type in {"preparation_analysis", "preparation_followup"}:
                    generate_preparation(
                        store,
                        fence,
                        factory,
                        validate_provider=validate_provider,
                    )
                else:
                    CreationPipeline(None).run_durable(
                        store,
                        fence,
                        factory,
                        validate_provider=validate_provider,
                        vectors=self._vectors(vault),
                    )
            except Exception as exc:
                current = store.read(job_id)
                if current["status"] == "cancel_requested":
                    store.finish_cancellation(job_id)
                elif current["status"] == "running":
                    code = (
                        "CONTEXT_BUDGET_EXCEEDED"
                        if isinstance(exc, ContextBudgetError)
                        else exc.detail.get("code", "JOB_EXECUTION_FAILED")
                        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict)
                        else "JOB_EXECUTION_FAILED"
                    )
                    store.pause(fence, code, failed=not isinstance(exc, HTTPException))
            return True
        finally:
            self.execution.release()

    def _vectors(self, vault):
        embedding = self.app.state.embedding_provider
        if not embedding:
            return None
        from novel_harness.services.local_vectors import LocalVectorIndex

        def vectors(runner):
            class EmbeddingStage:
                model = embedding.model
                endpoint = embedding.endpoint

                def embed(self, query):
                    from novel_harness.ai.local_provider import LocalProvider

                    def invoke(observer):
                        provider = LocalProvider(
                            embedding.endpoint,
                            embedding.model,
                            attempt_observer=observer,
                            transport=embedding.transport,
                        )
                        return {"vector": provider.embed(query)}

                    return runner.run(
                        "context_embedding",
                        {"query": query, "model": self.model},
                        invoke,
                        dict,
                        allow_known_failure=True,
                    )["vector"]

            return LocalVectorIndex(vault.root, EmbeddingStage())

        return vectors
