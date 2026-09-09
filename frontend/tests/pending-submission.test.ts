import { beforeEach, expect, it, vi } from 'vitest';
import { pendingSubmissions, prepareSubmission, acknowledge } from '../src/features/ai/pendingSubmission';

beforeEach(() => { localStorage.clear(); });

it('persists the exact command and reuses the key after lost acknowledgment', () => {
  const first = prepareSubmission('a', 'chapter', { instructions: '原文 ', expected_revision: 1 });
  const second = prepareSubmission('a', 'chapter', { instructions: '原文 ', expected_revision: 1 });
  expect(second.idempotencyKey).toBe(first.idempotencyKey);
  expect(pendingSubmissions('a')[0].command.instructions).toBe('原文 ');
  prepareSubmission('a', 'chapter', { instructions: 'changed' });
  expect(pendingSubmissions('a')).toHaveLength(2);
  expect(pendingSubmissions('b')).toHaveLength(0);
  acknowledge(first);
  expect(pendingSubmissions('a')).toHaveLength(1);
});

it('rejects malformed storage and exposes storage failure before fetch', () => {
  localStorage.setItem('studio:submission:a:bad', '{not json');
  expect(pendingSubmissions('a')).toEqual([]);
  const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw Error('quota'); });
  const first = prepareSubmission('quota-project', null, { instructions: 'test' });
  expect(first.persisted).toBe(false);
  expect(prepareSubmission('quota-project', null, { instructions: 'test' }).idempotencyKey)
    .toBe(first.idempotencyKey);
  spy.mockRestore();
});
