"""Continuous scope history and durable compaction, using an offline provider."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from novel_harness.ai.base import (
    ContextBudgetError,
    ProviderExecutionError,
    TextResult,
    check_input_budget,
)
from novel_harness.ai.demo import DemoProvider
from novel_harness.ai.prompts import PROMPT_VERSION
from novel_harness.db.job_models import AIJobControl, AIJobStageAttempt
from novel_harness.db.models import AIJob
from novel_harness.services.job_context import capture_source
from novel_harness.services.job_store import JobStore
from novel_harness.services.pipeline import CreationPipeline


class Crash(BaseException):
    pass


def history(client, project, count=12, *, chapter_id=None, size=1800):
    database = client.app.state.vault_registry.require(project['id']).database
    turns = []
    with database.session_scope() as session:
        for index in range(count):
            author = f'作者完整开头{index}:' + '旧约定' * size + f':完整结尾{index}'
            reply = f'回复完整开头{index}:' + '讨论细节' * size + f':回复结尾{index}'
            row = AIJob(project_id=project['id'], chapter_id=chapter_id, task_type='chat',
                        status='succeeded', prompt_version=PROMPT_VERSION,
                        instructions=author, result={'reply': reply},
                        created_at=datetime.now(UTC) - timedelta(days=10, minutes=count-index))
            session.add(row)
            session.flush()
            turns.append({'id': row.id, 'author': author, 'assistant': reply})
    return turns


def enqueue(client, project, *, chapter_id=None, budget=6000, capacity=8192, output=512):
    database = client.app.state.vault_registry.require(project['id']).database
    store = JobStore(database, project['id'])
    command = dict(project_id=project['id'], chapter_id=chapter_id, task_type='chat',
                   instructions='继续刚才的讨论，不要另起话题。', token_budget=budget,
                   expected_revision=1 if chapter_id else None)
    job = store.enqueue(command, uuid4().hex, lambda session: {
        'source_snapshot': capture_source(session, project['id'], chapter_id),
        'provider_identity': {'mode': 'demo', 'context_capacity': capacity,
                              'output_token_budget': output},
        'embedding_identity': None,
    })
    return store, job['id']


class Recording(DemoProvider):
    def __init__(self, calls):
        self.calls = calls

    def generate_text(self, request):
        check_input_budget(request)
        self.calls.append(deepcopy(request))
        if request.task == 'conversation_summary':
            return TextResult(text='作者希望保留旧约定，仍在讨论细节；尚未确认新的剧情事实。',
                              provider=self.name, model=self.model)
        return super().generate_text(request)


def execute(store, job_id, calls, factory=None, epoch='owner'):
    CreationPipeline(None).run_durable(store, store.claim(job_id, epoch),
                                      factory or (lambda observer: Recording(calls)))
    return store.read(job_id)


def test_full_history_fits_model_window_without_fixed_turn_or_text_truncation(client, project):
    turns = history(client, project, size=800)
    assert len(turns[0]['author']) > 2000 and len(turns[0]['assistant']) > 3000
    store, job_id = enqueue(client, project, budget=180000, capacity=200000)
    calls = []
    result = execute(store, job_id, calls)
    assert [request.task for request in calls] == ['chat']
    fragment = next(f for f in result['context_snapshot']['fragments']
                    if f['source_type'] == 'conversation')
    actual = json.loads(fragment['content'])
    assert [(t['author'], t['assistant']) for t in actual] == [
        (t['author'], t['assistant']) for t in turns]


def test_overflow_compacts_old_turns_retains_latest_full_and_preserves_originals(client, project):
    turns = history(client, project, count=12, size=120)
    store, job_id = enqueue(client, project)
    calls = []
    result = execute(store, job_id, calls)
    assert any(request.task == 'conversation_summary' for request in calls)
    packet = calls[-1].context['context_packet']['fragments']
    content = next(f['content'] for f in packet if f['source_type'] == 'conversation')
    latest = json.loads(content)[-1]
    assert (latest['author'], latest['assistant']) == (turns[-1]['author'], turns[-1]['assistant'])
    assert any(f['source_type'] == 'conversation_summary' for f in packet)
    assert result['context_snapshot']['conversation']['mode'] == 'compressed'
    with store.database.job_session_scope() as session:
        for turn in turns:
            row = session.get(AIJob, turn['id'])
            assert (row.instructions, row.result['reply']) == (turn['author'], turn['assistant'])


def test_compaction_plan_is_committed_before_send_and_replayed_after_crash(client, project):
    history(client, project, count=12, size=120)
    store, job_id = enqueue(client, project)
    calls = []

    def interrupted(observer):
        with store.database.job_session_scope() as session:
            persisted = session.get(AIJob, job_id).context_snapshot
            assert persisted['conversation']['plan']
        if observer.stage_key == 'chat':
            raise Crash()
        return Recording(calls)

    with pytest.raises(Crash):
        execute(store, job_id, calls, interrupted)
    paid_before = len(calls)
    assert paid_before > 0
    store.recover('restarted')
    result = execute(store, job_id, calls, epoch='restarted')
    assert result['status'] == 'succeeded'
    assert len(calls) == paid_before + 1
    assert calls[-1].task == 'chat'


def test_latest_whole_turn_cannot_fit_fails_before_any_model_send(client, project):
    history(client, project, count=2, size=2000)
    store, job_id = enqueue(client, project)
    calls = []
    with pytest.raises(ContextBudgetError):
        execute(store, job_id, calls)
    assert calls == []


def test_scope_isolation_excludes_other_chapter_and_internal_jobs(client, project, seeded_chapter):
    own = history(client, project, count=2, size=20)
    history(client, project, count=2, chapter_id=seeded_chapter, size=25)
    store, job_id = enqueue(client, project)
    with store.database.session_scope() as session:
        session.add(AIJob(project_id=project['id'], task_type='preparation_analysis',
                          status='succeeded', prompt_version='project-preparation-v1',
                          instructions='内部分析不可混入', result={'reply': '内部分析回复'}))
    calls = []
    result = execute(store, job_id, calls)
    fragment = next(f for f in result['context_snapshot']['fragments']
                    if f['source_type'] == 'conversation')
    assert [turn['author'] for turn in json.loads(fragment['content'])] == [
        turn['author'] for turn in own]


def test_unknown_compaction_send_is_not_automatically_repeated(client, project):
    history(client, project, count=12, size=120)
    store, job_id = enqueue(client, project)
    calls = []

    class Unknown(Recording):
        def generate_text(self, request):
            self.calls.append(request)
            raise Crash()

    with pytest.raises(Crash):
        execute(store, job_id, calls, lambda observer: Unknown(calls))
    store.recover('restart')
    assert store.read(job_id)['status'] == 'recovery_required'
    with pytest.raises(HTTPException) as caught:
        store.resume(job_id, uuid4().hex, store.read(job_id)['control_revision'], False)
    assert caught.value.detail['code'] == 'UNKNOWN_RESULT_CONFIRMATION_REQUIRED'
    assert len(calls) == 1


def test_frozen_legacy_snapshot_keeps_exact_request_on_resume(client, project):
    store, job_id = enqueue(client, project, budget=12000, capacity=32768)
    calls = []
    snapshot = {'format_version': 2, 'source_revision': None, 'task': 'chat',
                'token_budget': 12000, 'total_estimated_tokens': 0, 'over_budget': False,
                'dropped_source_ids': [], 'fragments': [],
                'execution_limits': {'context_capacity': 32768, 'output_token_budget': 512,
                                     'deadline_seconds': 180, 'output_parameter': 'max_tokens',
                                     'thinking_mode': 'provider_default'}}
    with store.write() as session:
        session.get(AIJob, job_id).context_snapshot = snapshot
        session.get(AIJobControl, job_id).context_ready = True
    result = execute(store, job_id, calls)
    assert 'conversation' not in result['context_snapshot']
    with store.database.job_session_scope() as session:
        attempt = session.scalar(select(AIJobStageAttempt).where(
            AIJobStageAttempt.job_id == job_id, AIJobStageAttempt.stage_key == 'chat'))
        expected = CreationPipeline(None)._initial_request('chat',
            '继续刚才的讨论，不要另起话题。', {}, snapshot)
        expected.stream_preview = True
        assert attempt.input_payload == expected.model_dump()


def test_mid_compaction_restart_reuses_first_part_and_frozen_history(client, project):
    original = history(client, project, count=16, size=120)
    store, job_id = enqueue(client, project)
    calls = []

    def interrupted(observer):
        if observer.stage_key == 'conversation.compact.1':
            raise Crash()
        return Recording(calls)

    with pytest.raises(Crash):
        execute(store, job_id, calls, interrupted)
    assert [call.task for call in calls] == ['conversation_summary']
    with store.database.job_session_scope() as session:
        assert not session.get(AIJobControl, job_id).context_ready
        frozen = deepcopy(session.get(AIJob, job_id).context_snapshot['conversation'])
    # A different job finishing during recovery cannot mutate this task's input.
    with store.write() as session:
        session.add(AIJob(project_id=project['id'], task_type='chat', status='succeeded',
                          prompt_version=PROMPT_VERSION, instructions='重启时新增的消息',
                          result={'reply': '新回复'}))
    store.recover('restart')
    result = execute(store, job_id, calls, epoch='restart')
    assert len([call for call in calls if call.task == 'conversation_summary']) == len(
        frozen['plan']['segments'])
    memory = result['context_snapshot']['conversation']
    assert memory['history_hash'] == frozen['history_hash']
    assert memory['total_turns'] == len(original)
    assert '重启时新增的消息' not in json.dumps(calls[-1].context, ensure_ascii=False)


def fail_after_compaction(client, project, *, chapter_id=None):
    store, job_id = enqueue(client, project, chapter_id=chapter_id)
    calls = []

    class Failed(Recording):
        def generate_text(self, request):
            if request.task == 'chat':
                raise ProviderExecutionError('known test failure', outcome='known')
            return super().generate_text(request)

    with pytest.raises(ProviderExecutionError):
        execute(store, job_id, calls, lambda observer: Failed(calls))
    assert calls and all(call.task == 'conversation_summary' for call in calls)
    return store, job_id


def test_new_turn_reuses_completed_memory_even_if_previous_chat_failed(client, project):
    history(client, project, count=12, size=120)
    _, prior_id = fail_after_compaction(client, project)
    store, job_id = enqueue(client, project)
    calls = []
    result = execute(store, job_id, calls)
    assert [call.task for call in calls] == ['chat']
    assert result['context_snapshot']['conversation']['reused_from_job_id'] == prior_id


@pytest.mark.parametrize('changed', ['content', 'identity', 'budget', 'artifact'])
def test_memory_cache_requires_same_content_model_budget_and_intact_checkpoint(
    client, project, changed,
):
    turns = history(client, project, count=12, size=120)
    store, prior_id = fail_after_compaction(client, project)
    with store.write() as session:
        if changed == 'content':
            session.get(AIJob, turns[0]['id']).instructions += '旧消息修订'
        if changed == 'artifact':
            prior = session.get(AIJob, prior_id)
            snapshot = deepcopy(prior.context_snapshot)
            snapshot['conversation']['summary'] = '伪造缓存正文，不对应模型检查点'
            prior.context_snapshot = snapshot
    store, job_id = enqueue(client, project, budget=5900 if changed == 'budget' else 6000)
    if changed == 'identity':
        with store.write() as session:
            control = session.get(AIJobControl, job_id)
            control.provider_identity = {**control.provider_identity, 'model': 'different'}
    calls = []
    execute(store, job_id, calls)
    assert any(call.task == 'conversation_summary' for call in calls)


def test_larger_window_returns_to_full_original_history_without_compression(client, project):
    turns = history(client, project, count=12, size=120)
    fail_after_compaction(client, project)
    store, job_id = enqueue(client, project, budget=180000, capacity=200000)
    calls = []
    result = execute(store, job_id, calls)
    assert [call.task for call in calls] == ['chat']
    state = result['context_snapshot']['conversation']
    assert state['mode'] == 'full' and not state['summary']
    actual = json.loads(next(f['content'] for f in calls[0].context['context_packet']['fragments']
                             if f['source_type'] == 'conversation'))
    assert [turn['assistant'] for turn in actual] == [turn['assistant'] for turn in turns]


def test_accepting_a_previously_summarized_candidate_invalidates_cached_status(
    client, project, seeded_chapter,
):
    from novel_harness.services.versions import create_version

    turns = history(client, project, count=12, size=120, chapter_id=seeded_chapter)
    database = client.app.state.vault_registry.require(project['id']).database
    with database.session_scope() as session:
        candidate = session.get(AIJob, turns[0]['id'])
        candidate.task_type = 'draft'
        candidate.result = {'candidate_text': '故事的第一段候选正文。', 'source_revision': 1}
    store, prior_id = fail_after_compaction(client, project, chapter_id=seeded_chapter)
    with store.write() as session:
        candidate = session.get(AIJob, turns[0]['id'])
        version = create_version(
            session, seeded_chapter, content=candidate.result['candidate_text'],
            source='ai', generation_job_id=candidate.id, expected_revision=1,
        )
        candidate.accepted_version_id = version.id
    store, job_id = enqueue(client, project, chapter_id=seeded_chapter)
    calls = []
    result = execute(store, job_id, calls)
    assert any(call.task == 'conversation_summary' for call in calls)
    assert result['context_snapshot']['conversation']['history_hash'] != (
        store.read(prior_id)['context_snapshot']['conversation']['history_hash'])


@pytest.mark.parametrize('invalid', ['', '\x00bad', '不可接受的超长记忆' * 1000],
                         ids=['blank', 'nul', 'oversized'])
def test_invalid_compaction_output_never_continues_chat(client, project, invalid):
    history(client, project, count=12, size=120)
    store, job_id = enqueue(client, project)
    calls = []

    class Invalid(Recording):
        def generate_text(self, request):
            self.calls.append(request)
            return TextResult(text=invalid, provider=self.name, model=self.model)

    with pytest.raises(ValueError):
        execute(store, job_id, calls, lambda observer: Invalid(calls))
    assert [call.task for call in calls] == ['conversation_summary']
    assert store.read(job_id)['status'] == 'failed'
