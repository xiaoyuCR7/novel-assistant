"""Persisted model-send evidence and validated stage checkpoints."""

import logging
import re
from time import monotonic
from uuid import uuid4

from fastapi import HTTPException

from novel_harness.ai.base import ProviderError, ProviderExecutionError
from novel_harness.services.job_preview import previews
from novel_harness.services.job_state import command_hash


class StageObserver:
    def __init__(self, store, fence, stage_key, payload, attempt_no, guard=None):
        self.store = store
        self.fence = fence
        self.stage_key = stage_key
        self.payload = payload
        self.attempt_no = attempt_no
        self.retry_pending = False
        self.sent = False
        self.guard = guard
        self.preview_token = None
        self.last_preview_guard = 0

    def preview(self, text):
        if self.guard and monotonic() - self.last_preview_guard >= 0.5:
            self.guard()
            self.last_preview_guard = monotonic()
        if self.preview_token:
            previews.append(
                str(self.store.database.path), self.fence.job_id, self.preview_token, text
            )

    def before_send(self):
        if self.guard:
            self.guard()
        if self.retry_pending:
            self.attempt_no = self.store.begin_attempt(self.fence, self.stage_key, self.payload)
            self.retry_pending = False
        self.store.dispatch(self.fence, self.stage_key, self.attempt_no)
        self.preview_token = previews.begin(
            str(self.store.database.path), self.fence.job_id, self.stage_key
        )
        self.sent = True

    def not_sent(self):
        self.store.fail_attempt(
            self.fence,
            self.stage_key,
            self.attempt_no,
            "CONNECT_FAILED",
            "not_sent",
            terminal=False,
        )
        self.retry_pending = True
        self.sent = False


class StageRunner:
    def __init__(self, store, fence, guard=None):
        self.store = store
        self.fence = fence
        self.guard = guard
        self.provider_metadata = {}

    def record_metrics(
        self, key, result, *, cached=False, status="succeeded", error_code=None
    ):
        self.provider_metadata.setdefault("stages", {})[key] = {
            **result.get("execution", {}),
            "cached": cached,
            "status": status,
            "error_code": error_code,
            "usage": result.get("usage", {}),
            "usage_source": result.get("usage_source", "estimated"),
        }

    def record_failure_metrics(self, key, result, code):
        if not isinstance(result, dict):
            return None
        self.provider_metadata.update(
            {name: result[name] for name in ("provider", "model") if name in result}
        )
        self.record_metrics(key, result, status="failed", error_code=code)
        return self.provider_metadata["stages"][key]

    def run(
        self,
        stage_key,
        payload,
        invoke,
        validate,
        *,
        allow_known_failure=False,
        nonterminal_known_codes=None,
    ):
        if self.guard:
            self.guard()
        cached = self.store.completed(self.fence.job_id, stage_key, command_hash(payload))
        if cached is not None:
            self.provider_metadata.update(
                {key: cached[key] for key in ("provider", "model") if key in cached}
            )
            result = validate(cached)
            self.record_metrics(stage_key, cached, cached=True)
            return result
        if nonterminal_known_codes:
            failure = self.store.known_failure(
                self.fence.job_id, stage_key, command_hash(payload), nonterminal_known_codes
            )
            if failure:
                code, metrics = failure
                self.provider_metadata.setdefault("stages", {})[stage_key] = metrics
                raise ProviderExecutionError(
                    "复用已知校验失败，继续独立修复检查点。", outcome="known", code=code
                )
        number = self.store.begin_attempt(self.fence, stage_key, payload)
        observer = StageObserver(self.store, self.fence, stage_key, payload, number, self.guard)
        received = False
        result = None
        started, diagnostic_id = monotonic(), uuid4().hex

        def failure_metrics(code, error):
            received_metadata = getattr(error, "metadata", None)
            metadata = (dict(result) if isinstance(result, dict) else dict(received_metadata)
                        if isinstance(received_metadata, dict) else {"usage_source": "unavailable"})
            metadata["execution"] = {
                "duration_ms": round((monotonic() - started) * 1000),
                "diagnostic_id": diagnostic_id,
            }
            return self.record_failure_metrics(stage_key, metadata, code)

        try:
            result = invoke(observer)
            received = True
            checked = validate(result)
            checked["execution"] = {
                "duration_ms": round((monotonic() - started) * 1000),
                "diagnostic_id": diagnostic_id,
            }
            self.provider_metadata.update(
                {key: checked[key] for key in ("provider", "model") if key in checked}
            )
            self.store.finish_attempt(self.fence, stage_key, observer.attempt_no, checked)
            self.record_metrics(stage_key, checked)
            logging.getLogger("novel_harness.stages").info(
                "stage_succeeded diagnostic=%s duration_ms=%s",
                diagnostic_id,
                checked["execution"]["duration_ms"],
            )
            previews.drop(str(self.store.database.path), self.fence.job_id)
            return checked
        except HTTPException as exc:
            previews.drop(str(self.store.database.path), self.fence.job_id)
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                code = "STAGE_INTERRUPTED"
            outcome = "known" if received else "unknown" if observer.sent else "not_sent"
            try:
                # Keep send evidence before the executor pauses for the original
                # guard error. A cancelled/replaced fence or saved checkpoint wins.
                self.store.fail_attempt(
                    self.fence,
                    stage_key,
                    observer.attempt_no,
                    code,
                    outcome,
                    terminal=False,
                    metrics=failure_metrics(code, exc),
                )
            except HTTPException:
                pass
            raise
        except Exception as exc:
            logging.getLogger("novel_harness.stages").warning(
                "stage_failed diagnostic=%s duration_ms=%s",
                diagnostic_id,
                round((monotonic() - started) * 1000),
            )
            previews.drop(str(self.store.database.path), self.fence.job_id)
            if isinstance(exc, ProviderError):
                outcome = getattr(exc, "outcome", "unknown" if observer.sent else "known")
                code = getattr(exc, "code", "PROVIDER_ERROR")
            else:
                outcome = "known" if received or not observer.sent else "unknown"
                code = "INVALID_STAGE_OUTPUT" if received else "STAGE_EXECUTION_ERROR"
            retryable_known_failure = outcome == "known" and (
                allow_known_failure or code in (nonterminal_known_codes or set())
            )
            self.store.fail_attempt(
                self.fence,
                stage_key,
                observer.attempt_no,
                code,
                outcome,
                terminal=not retryable_known_failure,
                metrics=failure_metrics(code, exc),
            )
            raise

    def skip(self, stage_key, reason):
        payload = {"reason": reason}
        cached = self.store.completed(self.fence.job_id, stage_key, command_hash(payload))
        if cached is None:
            number = self.store.begin_attempt(self.fence, stage_key, payload)
            self.store.finish_attempt(self.fence, stage_key, number, payload, skipped=True)
