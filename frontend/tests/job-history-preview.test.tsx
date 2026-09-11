import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { useProjectJobs } from '../src/features/ai/useProjectJobs';
import { ChatWorkspace } from '../src/features/ai/ChatWorkspace';
import { App } from '../src/app/App';
import { emptyLibraryPage } from './library-fixtures';
import { ModelSettings } from '../src/features/settings/ModelSettings';

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });
function summary(id: string, status = 'succeeded') {
  return { id, project_id: 'p', chapter_id: null, task_type: 'chat', status, created_at: id,
    updated_at: id, accepted_version_id: null, prompt_version: '1', error_code: null,
    error_message: '', instructions: `request ${id}`, preview: `preview ${id}`, control_revision: 1,
    recovery_reason: null, effects: {}, allowed_actions: status === 'running' ? ['cancel'] : [],
    current_stage: 'chat', status_url: `/api/v1/projects/p/ai/jobs/${id}` };
}
const response = (data: unknown) => new Response(JSON.stringify(data));
function setup() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  return { cache, wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={cache}>{children}</QueryClientProvider> };
}

it('pages lightweight summaries and fetches a full historical result only when selected', async () => {
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input); calls.push(url);
    if (url.includes('active_only=true')) return response({ items: [], next_cursor: null });
    if (url.includes('/ai/jobs/page?')) return response(url.includes('before=2')
      ? { items: [summary('1')], next_cursor: null }
      : { items: [summary('3'), summary('2')], next_cursor: '2' });
    if (/\/ai\/jobs\/\d$/.test(url)) { const id = url.at(-1)!; return response({ ...summary(id),
      context_snapshot: { fragments: [] }, result: { reply: `full reply ${id}` } }); }
    const project = { id: 'p', title: 'Novel', premise: '', genre: '', target_words: 1000, daily_goal: 100, status: 'active' };
    if (url.endsWith('/projects')) return response([project]);
    if (url.endsWith('/workspace/navigation')) return response({ project, nodes: [] });
    const page = emptyLibraryPage(url); if (page) return response(page);
    if (url.endsWith('/settings/model')) return response({ mode: 'demo', base_url: '', model: '', has_api_key: false, external_consent: false });
    if (url.endsWith('/progress')) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 0, completed_chapters: 0, daily_goal: 100 });
    return response([]);
  }));
  const { cache, wrapper } = setup();
  const view = render(<App />, { wrapper });
  expect(await screen.findByText('full reply 3', { selector: 'p' })).toBeVisible();
  expect(calls.some(url => url.endsWith('/ai/jobs/2'))).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '加载更早对话' }));
  expect(await screen.findByText('preview 1')).toBeVisible();
  expect(calls.some(url => url.endsWith('/ai/jobs/1'))).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: '查看完整回复 1' }));
  expect(await screen.findByText('full reply 1', { selector: 'p' })).toBeVisible();
  const count = calls.filter(url => /\/ai\/jobs\/\d$/.test(url)).length;
  await act(async () => { await cache.invalidateQueries({ queryKey: ['jobs', 'p'] }); });
  expect(calls.filter(url => /\/ai\/jobs\/\d$/.test(url))).toHaveLength(count);
  expect(calls.some(url => url.includes('/ai/jobs?'))).toBe(false);
  view.unmount(); cache.clear();
});

it('requests page cursor with summary kind and never pretends summaries have result payloads', async () => {
  const fetcher = vi.fn(async (input: RequestInfo | URL) => response(String(input).includes('active_only=true')
    ? { items: [], next_cursor: null } : !String(input).includes('/page?') ? [] : String(input).includes('before=2')
    ? { items: [summary('1')], next_cursor: null }
    : { items: [summary('2')], next_cursor: '2' }));
  vi.stubGlobal('fetch', fetcher);
  const { wrapper, cache } = setup();
  const { result, unmount } = renderHook(() => useProjectJobs('p', 'chapter', 'summary'), { wrapper });
  await waitFor(() => expect(result.current.data?.[0]?.id).toBe('2'));
  expect(result.current.data?.[0]).not.toHaveProperty('result');
  await act(async () => { await result.current.fetchNextPage(); });
  await waitFor(() => expect(result.current.data?.map(job => job.id)).toEqual(['1', '2']));
  expect(fetcher.mock.calls.some(call => String(call[0]).includes('before=2') && String(call[0]).includes('kind=summary'))).toBe(true);
  unmount(); cache.clear();
});

it('shows partial preview read-only, clears sequence zero, and reconnects without POST', async () => {
  let preview: unknown = { job_id: '3', status: 'running', stage: 'chat', text: 'partial words', sequence: 2, truncated: false };
  const fetcher = vi.fn(async (_input: RequestInfo | URL) => {
    if (preview instanceof Error) throw preview;
    return response(preview);
  });
  vi.stubGlobal('fetch', fetcher);
  const { wrapper, cache } = setup();
  const view = render(<ChatWorkspace projectId="p" selectedJobId="3" chapterTitle="Novel" hasChapter
    jobs={[summary('3', 'running')]} running={false} onSend={vi.fn()} onAccept={vi.fn()} onOpenManuscript={vi.fn()} />, { wrapper });
  expect(await screen.findByText('partial words')).toBeVisible();
  expect(screen.queryByRole('button', { name: '写入正文' })).not.toBeInTheDocument();
  preview = { job_id: '3', status: 'running', stage: 'chat', text: '', sequence: 0, truncated: false };
  await act(async () => { await cache.invalidateQueries({ queryKey: ['job-preview'] }); });
  await waitFor(() => expect(screen.queryByText('partial words')).not.toBeInTheDocument());
  preview = new Error('stream disconnected');
  await act(async () => { await cache.invalidateQueries({ queryKey: ['job-preview'] }); });
  expect(await screen.findByText(/预览连接中断/)).toBeVisible();
  expect(fetcher.mock.calls.every(call => String(call[0]).endsWith('/preview'))).toBe(true);
  expect(fetcher.mock.calls.every(call => !(call as unknown as [unknown, RequestInit?])[1]?.method)).toBe(true);
  view.unmount(); cache.clear();
});

it('ignores a late preview from an unselected job', async () => {
  let release!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => String(input).includes('/3/preview')
    ? new Promise<Response>(resolve => { release = resolve; })
    : response({ job_id: '4', status: 'running', stage: 'chat', text: 'current preview', sequence: 1, truncated: false })));
  const { wrapper, cache } = setup();
  const props = { projectId: 'p', chapterTitle: 'Novel', hasChapter: true, jobs: [summary('3', 'running'), summary('4', 'running')],
    running: false, onSend: vi.fn(), onAccept: vi.fn(), onOpenManuscript: vi.fn() };
  const view = render(<ChatWorkspace {...props} selectedJobId="3" />, { wrapper });
  await waitFor(() => expect(release).toBeTypeOf('function'));
  view.rerender(<ChatWorkspace {...props} selectedJobId="4" />);
  expect(await screen.findByText('current preview')).toBeVisible();
  await act(async () => { release(response({ job_id: '3', status: 'running', stage: 'chat', text: 'stale preview', sequence: 1, truncated: false })); });
  expect(screen.queryByText('stale preview')).not.toBeInTheDocument();
  view.unmount(); cache.clear();
});

it('never offers an old completed candidate while that job is running again', () => {
  const running = summary('3', 'running');
  render(<ChatWorkspace selectedJobId="3" selectedJob={{ ...running, status: 'succeeded',
    context_snapshot: {}, result: { candidate_text: 'old completed candidate' } }}
    chapterTitle="Novel" hasChapter jobs={[running]} running={false}
    onSend={vi.fn()} onAccept={vi.fn()} onOpenManuscript={vi.fn()} />);
  expect(screen.queryByRole('button', { name: '写入正文' })).not.toBeInTheDocument();
  expect(screen.queryByText('old completed candidate')).not.toBeInTheDocument();
});

it('includes active jobs outside history and observes their terminal state without polling history', async () => {
  let finished = false;
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input); calls.push(url);
    if (url.includes('active_only=true')) return response({ items: finished ? [] : [summary('1', 'running')], next_cursor: null });
    if (url.endsWith('/ai/jobs/1')) return response({ ...summary('1'), result: { reply: 'completed' }, context_snapshot: {} });
    return response({ items: [summary('9')], next_cursor: '9' });
  }));
  const { wrapper, cache } = setup();
  const { result, unmount } = renderHook(() => useProjectJobs('p'), { wrapper });
  await waitFor(() => expect(result.current.data?.some(job => job.id === '1' && job.status === 'running')).toBe(true));
  const historyCount = calls.filter(url => url.includes('active_only=false')).length;
  finished = true;
  await act(async () => { await cache.invalidateQueries({ queryKey: ['jobs', 'p', undefined, 'writing', 'active'] }); });
  await waitFor(() => expect(result.current.data?.find(job => job.id === '1')?.status).toBe('succeeded'));
  expect(calls.filter(url => url.includes('active_only=false'))).toHaveLength(historyCount);
  expect(result.current.data?.find(job => job.id === '1')).not.toHaveProperty('result');
  unmount(); cache.clear();
});

it('reconciles a first history running item missing from the first active snapshot only once', async () => {
  let releaseActive!: (value: Response) => void;
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input); calls.push(url);
    if (url.includes('active_only=true')) return new Promise<Response>(resolve => { releaseActive = resolve; });
    if (url.endsWith('/ai/jobs/1')) return response({ ...summary('1'), updated_at: '2',
      result: { reply: 'finished between snapshots' }, context_snapshot: {} });
    return response({ items: [summary('1', 'running')], next_cursor: null });
  }));
  const { wrapper, cache } = setup();
  const { result, unmount } = renderHook(() => useProjectJobs('p'), { wrapper });
  await waitFor(() => expect(result.current.data?.[0]?.status).toBe('running'));
  await act(async () => { releaseActive(response({ items: [], next_cursor: null })); });
  await waitFor(() => expect(result.current.data?.[0]?.status).toBe('succeeded'));
  expect(calls.filter(url => url.endsWith('/ai/jobs/1'))).toHaveLength(1);
  expect(result.current.data?.[0]).not.toHaveProperty('result');
  const count = calls.length;
  act(() => { void cache.invalidateQueries({ queryKey: ['jobs', 'p', undefined, 'writing', 'active'] }); });
  await waitFor(() => expect(calls.length).toBe(count + 1));
  await act(async () => { releaseActive(response({ items: [], next_cursor: null })); });
  expect(calls.filter(url => url.endsWith('/ai/jobs/1'))).toHaveLength(1);
  expect(calls.filter(url => url.includes('active_only=false'))).toHaveLength(1);
  unmount(); cache.clear();
});

it('bounds retained completed summaries when a history-only running item completes', async () => {
  const { wrapper, cache } = setup();
  const activeKey = ['jobs', 'p', undefined, 'writing', 'active'];
  cache.setQueryData(activeKey, Array.from({ length: 100 }, (_, i) => summary(`old-${i}`)));
  cache.setQueryData(['jobs', 'p', undefined, 'writing'], {
    pages: [{ items: [summary('new', 'running')], next_cursor: null }], pageParams: [undefined],
  });
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input); calls.push(url);
    return response(url.endsWith('/ai/jobs/new')
      ? { ...summary('new'), updated_at: 'z', result: {}, context_snapshot: {} }
      : { items: [], next_cursor: null });
  }));
  const { result, unmount } = renderHook(() => useProjectJobs('p'), { wrapper });
  await act(async () => { await cache.invalidateQueries({ queryKey: activeKey }); });
  await waitFor(() => expect(result.current.data?.find(job => job.id === 'new')?.status).toBe('succeeded'));
  const retained = cache.getQueryData<Array<{ id: string; status: string }>>(activeKey)!;
  expect(retained.filter(job => job.status === 'succeeded')).toHaveLength(100);
  expect(retained.some(job => job.id === 'new')).toBe(true);
  expect(calls.some(url => url.includes('active_only=false'))).toBe(false);
  unmount(); cache.clear();
});

it('keeps late history reconciliation inside its original project cache', async () => {
  const { wrapper, cache } = setup();
  cache.setQueryData(['jobs', 'p', undefined, 'writing'], {
    pages: [{ items: [summary('1', 'running')], next_cursor: null }], pageParams: [undefined],
  });
  let releaseDetail!: (value: Response) => void;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/projects/p/ai/jobs/1')) return new Promise<Response>(resolve => { releaseDetail = resolve; });
    return response({ items: [], next_cursor: null });
  }));
  const { result, rerender, unmount } = renderHook(({ project }) => useProjectJobs(project), {
    wrapper, initialProps: { project: 'p' },
  });
  await waitFor(() => expect(releaseDetail).toBeTypeOf('function'));
  rerender({ project: 'other' });
  await act(async () => { releaseDetail(response({ ...summary('1'), updated_at: '2',
    result: { reply: 'private original project' }, context_snapshot: {} })); });
  await waitFor(() => expect(result.current.data ?? []).toEqual([]));
  expect(cache.getQueryData(['job-detail', 'p', '1', 'succeeded', 1, null])).toBeDefined();
  expect(cache.getQueryData(['job-detail', 'other', '1', 'succeeded', 1, null])).toBeUndefined();
  unmount(); cache.clear();
});

it('preserves edited settings during a failed background refresh', async () => {
  let failed = false;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).endsWith('/settings/model/profiles')) {
      return response({ items: [], current_config_revision: 'a'.repeat(64) });
    }
    if (failed) throw new Error('offline');
    return response({ mode: 'demo', base_url: '', model: '', has_api_key: false, external_consent: false });
  }));
  const { wrapper, cache } = setup();
  const view = render(<ModelSettings />, { wrapper });
  fireEvent.change(await screen.findByLabelText('最大输出 Token'), { target: { value: '8192' } });
  failed = true;
  await act(async () => { await cache.invalidateQueries({ queryKey: ['model-settings'] }); });
  expect(await screen.findByRole('alert')).toHaveTextContent('offline');
  expect(screen.getByLabelText('最大输出 Token')).toHaveValue(8192);
  view.unmount(); cache.clear();
});
