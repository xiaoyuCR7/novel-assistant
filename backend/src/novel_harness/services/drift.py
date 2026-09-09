"""Evidence-based content checks; alerts never change manuscript or confirmed facts."""

import json

from fastapi import HTTPException
from sqlalchemy import select

from novel_harness.db.models import Conflict, Project
from novel_harness.services.feedback import create_conflict
from novel_harness.services.search_index import tokenize
from novel_harness.services.serialization import serialize
from novel_harness.services.versions import get_or_create_document


def collect_local_findings(session, chapter_id):
    document = get_or_create_document(session, chapter_id)
    findings = []
    for field, severity, code, label, dimension, suggestion in (
        (
            "forbidden_revelations",
            "severe",
            "EARLY_REVELATION",
            "正文包含禁止提前揭示的内容",
            "continuity",
            "删除、延后或由作者明确接受这次提前揭示。",
        ),
        (
            "forbidden_phrases",
            "warning",
            "FORBIDDEN_PHRASE",
            "正文包含禁用表达",
            "pacing_and_focus",
            "替换禁用表达，保持作者设定的语言边界。",
        ),
    ):
        for phrase in document.contract.get(field, []):
            if phrase and phrase in document.content:
                start = document.content.index(phrase)
                findings.append(
                    {
                        "code": code,
                        "severity": severity,
                        "message": label,
                        "dimension": dimension,
                        "evidence_quote": phrase,
                        "suggestion": suggestion,
                        "reference_ids": [f"chapter:{chapter_id}"],
                        "start": start,
                        "end": start + len(phrase),
                    }
                )
    project = session.get(Project, document.project_id)
    theme = set(tokenize(project.premise))
    if len(document.content) > 120 and theme and not theme.intersection(tokenize(document.content)):
        findings.append(
            {
                "code": "THEME_DISTANCE",
                "severity": "info",
                "message": "本章与核心前提没有词语交集，请作者判断是否为有意铺垫。",
                "dimension": "theme_alignment",
                "evidence_quote": document.content[:1000],
                "suggestion": "仅作词面提醒；确认本章是否仍服务于核心前提。",
                "reference_ids": ["project:core"],
                "start": 0,
                "end": min(1000, len(document.content)),
            }
        )
    purpose = str(document.contract.get("purpose", "")).strip()
    purpose_tokens = set(tokenize(purpose))
    if (
        len(document.content) > 120
        and purpose_tokens
        and not purpose_tokens.intersection(tokenize(document.content))
    ):
        findings.append(
            {
                "code": "PURPOSE_DISTANCE",
                "severity": "warning",
                "message": "本章与章节目的没有词语交集，请核对是否完成预定推进。",
                "dimension": "chapter_purpose",
                "evidence_quote": document.content[:1000],
                "suggestion": "仅作词面提醒；补足与章节目的的因果联系或更新契约。",
                "reference_ids": [f"chapter:{chapter_id}"],
                "start": 0,
                "end": min(1000, len(document.content)),
            }
        )
    return findings


def _hard_fact_conflicts(observations, context):
    facts = []
    sources = [
        *context.get("hard_sources", []),
        *context.get("confirmed_fact_sources", []),
    ]
    seen = set()
    for source in sources:
        if (
            source.get("type") != "canon_fact"
            or source.get("constraint") not in {"hard", "confirmed"}
            or source.get("id") in seen
        ):
            continue
        seen.add(source.get("id"))
        try:
            fact = source["content"]
            if isinstance(fact, str):
                fact = json.loads(fact)
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if fact.get("status") == "confirmed":
            facts.append(fact)
    conflicts = []
    for observation in observations:
        value = observation.get("value")
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            continue
        for fact in facts:
            expected = fact.get("value")
            if (
                observation.get("entity_id") == fact.get("subject_entity_id")
                and observation.get("predicate") == fact.get("predicate")
                and type(value) is type(expected)
                and value != expected
            ):
                conflicts.append(
                    {
                        "code": "CONFIRMED_FACT_CONFLICT",
                        "severity": "severe",
                        "dimension": "continuity",
                        "message": "正文中的明确陈述与当前有效的作者确认事实冲突。",
                        "evidence_quote": observation["evidence_quote"],
                        "suggestion": "修改正文，或由作者明确决定保留这次偏离。",
                        "reference_ids": observation.get("reference_ids", []),
                        "related_entity_ids": [observation["entity_id"]]
                        if observation.get("entity_id")
                        else [],
                        "start": observation["start"],
                        "end": observation["end"],
                    }
                )
    return conflicts


def _persist_finding(session, chapter_id, job_id, revision, source, finding):
    start, end = finding["start"], finding["end"]
    quote = finding["evidence_quote"]
    if not (0 <= start < end <= len(source)) or source[start:end] != quote:
        raise ValueError("Content check evidence does not match frozen source")
    code = finding.get("code") or f"CONTENT_{finding['dimension'].upper()}"
    evidence = [
        "content_check:v1",
        f"ai_job:{job_id}",
        f"document_revision:{revision}",
        f"dimension:{finding['dimension']}",
        f"range:{start}:{end}",
        f"quote:{quote}",
        f"suggestion:{finding['suggestion']}",
        *(f"reference:{item}" for item in finding.get("reference_ids", [])),
    ]
    existing = session.scalar(
        select(Conflict).where(
            Conflict.chapter_id == chapter_id,
            Conflict.code == code,
            Conflict.message == finding["message"],
            Conflict.evidence == evidence,
        )
    )
    if existing:
        return existing
    return create_conflict(
        session,
        chapter_id,
        {
            "code": code,
            "severity": finding["severity"],
            "message": finding["message"],
            "evidence": evidence,
            "related_entity_ids": finding.get("related_entity_ids", []),
        },
    )


def persist_content_check(
    session,
    chapter_id,
    job_id,
    revision,
    source,
    findings,
    observations,
    context,
):
    combined = [*findings, *_hard_fact_conflicts(observations, context)]
    return [
        _persist_finding(session, chapter_id, job_id, revision, source, item)
        for item in combined
    ]


def has_open_severe_content_conflicts(session, job_id, revision):
    job_tag = f"ai_job:{job_id}"
    revision_tag = f"document_revision:{revision}"
    conflicts = session.scalars(
        select(Conflict).where(
            Conflict.status == "open",
            Conflict.severity.in_(("error", "severe")),
        )
    )
    return any(job_tag in item.evidence and revision_tag in item.evidence for item in conflicts)


def open_blocking_conflict_ids(session, chapter_id, job_id, revision):
    """Return stable task-bound open errors that require an acceptance decision."""
    job_tag = f"ai_job:{job_id}"
    revision_tag = f"document_revision:{revision}"
    conflicts = session.scalars(
        select(Conflict)
        .where(
            Conflict.chapter_id == chapter_id,
            Conflict.status == "open",
            Conflict.severity.in_(("error", "severe")),
        )
        .order_by(Conflict.created_at, Conflict.id)
    )
    return [
        item.id
        for item in conflicts
        if job_tag in item.evidence and revision_tag in item.evidence
    ]


def check_drift(session, chapter_id):
    document = get_or_create_document(session, chapter_id)
    findings = collect_local_findings(session, chapter_id)
    for finding in findings:
        quote = finding["evidence_quote"]
        evidence = [quote, f"document_revision:{document.revision}"]
        code, severity, message = (
            finding["code"],
            finding["severity"],
            finding["message"],
        )
        existing = session.scalar(
            select(Conflict).where(
                Conflict.chapter_id == chapter_id,
                Conflict.code == code,
                Conflict.message == message,
                Conflict.evidence == evidence,
            )
        )
        if not existing:
            create_conflict(
                session,
                chapter_id,
                {
                    "code": code,
                    "severity": severity,
                    "message": message,
                    "evidence": evidence,
                    "related_entity_ids": [],
                },
            )
    return [
        serialize(c)
        for c in session.scalars(
            select(Conflict)
            .where(Conflict.chapter_id == chapter_id)
            .order_by(Conflict.created_at.desc())
        )
    ]


def decide_alert(session, conflict_id, decision, confirmed):
    conflict = session.get(Conflict, conflict_id)
    if not conflict:
        raise HTTPException(404, detail={"code": "CONFLICT_NOT_FOUND"})
    if decision == "accept" and conflict.severity in {"error", "severe"} and not confirmed:
        raise HTTPException(409, detail={"code": "SEVERE_CONFIRMATION_REQUIRED"})
    conflict.status = "accepted" if decision == "accept" else "dismissed"
    conflict.decision_note = "作者决定继续" if decision == "accept" else "作者关闭提醒"
    session.flush()
    return serialize(conflict)
