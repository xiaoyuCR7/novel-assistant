"""Deterministic task identities and conservative recovery decisions."""

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class JobFence:
    job_id: str
    epoch: str
    control_revision: int


ACTIVE = frozenset({"queued", "running", "cancel_requested"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
REPLACEABLE = frozenset({"failed", "recovery_required", "cancelled"})


def replacement_requires_confirmation(control, latest=None):
    return bool(
        control is None
        or control.recovery_reason in {"result_unknown", "unsupported_format"}
        or latest and (
            latest.status in {"dispatched", "result_unknown"} or latest.outcome == "unknown"
        )
    )


def command_hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def recovery_action(stage: str | None, cancel: bool) -> str:
    if cancel:
        return "cancel"
    if stage in {"succeeded", "skipped"}:
        return "reuse"
    if stage == "prepared":
        return "continue"
    if stage == "failed":
        return "pause_failed"
    return "pause_unknown"
