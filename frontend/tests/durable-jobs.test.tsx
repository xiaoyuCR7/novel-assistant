import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { JobStatus } from '../src/features/ai/JobStatus';
import { pollInterval, useProjectJobs } from '../src/features/ai/useProjectJobs';
import { ChatWorkspace } from '../src/features/ai/ChatWorkspace';

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it('shows the final review stage with an author-facing Chinese label', () => {
  render(<JobStatus job={{ id: 'job', status: 'running', task_type: 'rewrite', current_stage: 'final_review' }} />);
  expect(screen.getByRole('status')).toHaveTextContent('正在处理 · 最终连续性复核');
  expect(screen.getByRole('status')).not.toHaveTextContent('final_review');
});

it('explains conversation compaction while a chat is running', () => {
  render(<JobStatus job={{ id: 'memory-job', status: 'running', task_type: 'chat',
    current_stage: 'conversation.compact.1' }} />);
  expect(screen.getByRole('status')).toHaveTextContent('正在处理 · 整理较早对话（第2部分）');
  expect(screen.getByRole('status')).not.toHaveTextContent('conversation.compact');
});

it.each([
  ['chapter_summary.chunk.0', '分段内容检查（第1段）'],
  ['chapter_summary.merge.1.2', '汇总内容检查（第2轮/第3组）'],
  ['future_stage', 'future_stage'],
])('shows readable summary progress and preserves unknown stage fallback (%s)', (stage, label) => {
  render(<JobStatus job={{ id: 'job', status: 'running', task_type: 'chapter_summary', current_stage: stage }} />);
  expect(screen.getByRole('status')).toHaveTextContent(`正在处理 · ${label}`);
});

it('uses controlled task responses and keeps a late project response in its own cache', async () => {
  const responses: Array<(value: Response) => void> = [];
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).includes('active_only=true')
    ? Promise.resolve(new Response(JSON.stringify({ items: [], next_cursor: null })))
    : new Promise<Response>(resolve => responses.push(resolve))));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result, rerender, unmount } = renderHook(({ project }) => useProjectJobs(project, 'chapter'), {
    wrapper, initialProps: { project: 'first' },
  });
  await waitFor(() => expect(responses).toHaveLength(1));
  rerender({ project: 'second' });
  await waitFor(() => expect(responses).toHaveLength(2));
  const job = (id: string, status: string) => new Response(JSON.stringify({
    items: [{ id, status, task_type: 'chat', preview: '' }], next_cursor: null,
  }));
  await act(async () => { responses[0](job('first-task', 'succeeded')); });
  expect(result.current.data ?? []).toEqual([]);
  await act(async () => { responses[1](job('second-task', 'queued')); });
  await waitFor(() => expect(result.current.data?.[0].status).toBe('queued'));
  for (const status of ['running', 'recovery_required', 'succeeded']) {
    const count = responses.length;
    act(() => { void result.current.refetch(); });
    await waitFor(() => expect(responses.length).toBe(count + 1));
    await act(async () => { responses[count](job('second-task', status)); });
    await waitFor(() => expect(result.current.data?.[0].status).toBe(status));
  }
  expect(result.current.data?.[0].id).toBe('second-task');
  unmount(); client.clear();
});

it('stops terminal polling and bounds active polling', () => {
  expect(pollInterval(['succeeded', 'recovery_required'], 5)).toBe(false);
  expect(pollInterval(['queued'], 0)).toBe(1000);
  expect(pollInterval(['running'], 9)).toBe(8000);
});

function pollingStudio() {
  vi.useFakeTimers();
  let finished = false;
  const calls: string[] = [];
  const running = { id: 'poll-job', project_id: 'poll-project', chapter_id: 'chapter',
    task_type: 'chapter_summary', status: 'running', current_stage: 'chapter_summary.chunk.0',
    control_revision: 1, created_at: '2026-08-28', updated_at: '2026-08-28', allowed_actions: [] };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input); calls.push(url);
    const data = url.includes('active_only=true') ? { items: finished ? [] : [running], next_cursor: null }
      : url.endsWith('/ai/jobs/poll-job') ? { ...running, status: 'succeeded', result: {}, context_snapshot: {} }
      : { items: [], next_cursor: null };
    return new Response(JSON.stringify(data));
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook((_noise: number) => useProjectJobs('poll-project', 'chapter', 'summary'), { wrapper, initialProps: 0 });
  return { ...hook, calls, client,
    advance: async (milliseconds: number) => { await act(async () => vi.advanceTimersByTimeAsync(milliseconds)); },
    finish: () => { finished = true; },
    pollCount: () => calls.filter(url => url.includes('active_only=true')).length,
    dispose: () => { hook.unmount(); client.clear(); },
  };
}

it('backs off only after completed unchanged polls, never from unrelated hook rerenders', async () => {
  const app = pollingStudio();
  try {
    await app.advance(0);
    expect(app.pollCount()).toBe(1);
    for (let sample = 0; sample < 4; sample++) {
      for (let noise = 0; noise < 20; noise++) app.rerender(sample * 20 + noise);
      await app.advance(1000 * 2 ** sample - 1);
      expect(app.pollCount()).toBe(sample + 1);
      await app.advance(1);
      expect(app.pollCount()).toBe(sample + 2);
    }
    await app.advance(8000);
    expect(app.pollCount()).toBe(6);
    expect(app.calls.filter(url => url.includes('active_only=false'))).toHaveLength(1);
  } finally { app.dispose(); }
});

it('discovers a finished summary at the next one-second poll despite UI rerenders and reconciles once', async () => {
  const app = pollingStudio();
  try {
    await app.advance(0);
    await app.advance(220);
    app.finish();
    for (let noise = 0; noise < 25; noise++) app.rerender(noise);
    await app.advance(780);
    expect(app.pollCount()).toBe(2);
    // Flush React Query's batched render notification after the completed fetch.
    await app.advance(1);
    expect(app.result.current.data?.find(job => job.id === 'poll-job')?.status).toBe('succeeded');
    expect(app.calls.filter(url => url.endsWith('/ai/jobs/poll-job'))).toHaveLength(1);
    for (let noise = 25; noise < 50; noise++) app.rerender(noise);
    await app.advance(14998);
    expect(app.pollCount()).toBe(2);
    await app.advance(1);
    expect(app.pollCount()).toBe(3);
    expect(app.calls.filter(url => url.endsWith('/ai/jobs/poll-job'))).toHaveLength(1);
    expect(app.calls.filter(url => url.includes('active_only=false'))).toHaveLength(1);
  } finally { app.dispose(); }
});

it('keeps newly typed text when the earlier submission is acknowledged', async () => {
  let acknowledge!: () => void;
  const onSend = vi.fn(() => new Promise<void>(resolve => { acknowledge = resolve; }));
  render(<ChatWorkspace chapterTitle="Chapter" hasChapter jobs={[]} running={false}
    onSend={onSend} onAccept={async () => {}} onOpenManuscript={() => {}} />);
  const input = screen.getByLabelText('给 AI 的消息');
  fireEvent.change(input, { target: { value: 'first request' } });
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }));
  fireEvent.change(input, { target: { value: 'next request' } });
  acknowledge();
  await waitFor(() => expect(input).toHaveValue('next request'));
  expect(onSend).toHaveBeenCalledWith('chat', 'first request');
});

it('requires an explicit warning before unknown-result resubmission', async () => {
  const resume = vi.fn();
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<JobStatus job={{ id: 'job', status: 'recovery_required', task_type: 'chat',
    recovery_reason: 'result_unknown', allowed_actions: ['resume'] }}
    onResume={resume} />);
  fireEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  expect(resume).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  await waitFor(() => expect(resume).toHaveBeenCalledWith(true));
  confirm.mockRestore();
});

it.each([
  ['inherited', Object.assign(Object.create({ summary_id: 'inherited-summary' }), { ledger_pending: false })],
  ['null', null],
  ['array', Object.assign([], { summary_id: 'array-summary' })],
  ['blank', { summary_id: '   ' }],
])('does not present invalid %s summary effects as published', (_label, effects) => {
  const replace = vi.fn(async () => {});
  render(<JobStatus job={{ id: 'job', status: 'cancelled', task_type: 'chapter_summary',
    effects: effects as never, allowed_actions: ['replace'] }} onReplace={replace} />);
  expect(screen.queryByText(/AI 总结已保存/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '按当前设置新建' })).toBeEnabled();
});

it('presents only an own non-blank string summary identity as published', () => {
  render(<JobStatus job={{ id: 'job', status: 'failed', task_type: 'chapter_summary',
    effects: { summary_id: '  published-summary  ' }, allowed_actions: ['replace'] }} onReplace={async () => {}} />);
  expect(screen.getByText(/AI 总结已保存/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '按当前设置新建' })).not.toBeInTheDocument();
});
