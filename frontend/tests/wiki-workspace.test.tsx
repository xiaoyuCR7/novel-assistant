import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { WikiWorkspace } from '../src/features/wiki/WikiWorkspace';
import { refreshLibrary } from '../src/features/library/refreshLibrary';
import { pendingSubmissions } from '../src/features/ai/pendingSubmission';
import type { AIJob } from '../src/lib/api';
import type { WikiDetail, WikiEntry } from '../src/lib/wikiApi';

const clients: QueryClient[] = [];
afterEach(() => {
  clients.forEach(client => client.clear()); clients.length = 0;
  vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear();
});
const entry: WikiEntry = { id: 'e1', type: 'entity', title: '林渡', preview: '雾城邮差', aliases: ['小渡'] };
const second: WikiEntry = { id: 'e2', type: 'entity', title: '钟守', preview: '守塔人', aliases: [] };
const evidence = { id: 'c1', type: 'canon', title: '投递约定', content: '<img src=x onerror=alert(1)>信必须送达',
  revision: 2, source_hash: 'source-hash', reason: '关联记录' };
const job = (status: string, patch: Partial<AIJob> = {}): AIJob => ({ id: 'job-wiki', task_type: 'wiki_summary', status,
  control_revision: 1, context_snapshot: {}, result: {}, allowed_actions: ['cancel'], ...patch });
const article = (patch: Partial<WikiDetail> = {}): WikiDetail => ({ id: entry.id, type: entry.type, title: entry.title,
  aliases: entry.aliases, scope_chapter_id: null, sources: [evidence], links: [], state_history: [], fingerprint: 'a'.repeat(64),
  truncated: false, summary: null, job: null, ...patch });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
type Handler = (url: URL, init?: RequestInit) => Response | Promise<Response> | undefined;

function setup(handler?: Handler, options: { entries?: WikiEntry[]; detail?: WikiDetail } = {}) {
  const requests: Array<{ url: URL; init?: RequestInit }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local');
    requests.push({ url, init });
    const handled = handler?.(url, init);
    if (handled !== undefined) return handled;
    if (url.pathname.endsWith('/wiki')) return response({ items: options.entries ?? [entry], total: options.entries?.length ?? 1, has_more: false });
    if (url.pathname.includes('/wiki/entity/')) return response(options.detail ?? article());
    throw Error(`Unexpected request ${url}`);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  clients.push(client);
  const onOpen = vi.fn(), onOpenChapter = vi.fn();
  const tree = (projectId = 'p', instance = 1) => <QueryClientProvider client={client}>
    <WikiWorkspace key={`${projectId}:${instance}`} projectId={projectId} chapters={[{ id: 'chapter/1', title: '第一封信' }]}
      onOpen={onOpen} onOpenChapter={onOpenChapter} />
  </QueryClientProvider>;
  const rendered = render(tree());
  return { client, requests, onOpen, onOpenChapter, rendered,
    remount: () => rendered.rerender(tree('p', 2)), switchProject: () => rendered.rerender(tree('other', 2)),
    posts: () => requests.filter(request => request.init?.method === 'POST'),
    detailReads: () => requests.filter(request => request.url.pathname.includes('/wiki/entity/') && !request.init?.method) };
}

it('opens original evidence, linked entries, and dated chapters while treating source content as text', async () => {
  const app = setup(undefined, { detail: article({ truncated: true, links: [second],
    state_history: [{ id: 'state1', chapter_id: 'chapter/1', chapter_title: '第一封信', data: { location: '雾城' }, revision: 3 }] }) });
  expect(await screen.findByText(evidence.content)).toBeVisible();
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  expect(screen.getByText(/本页仅展示部分来源或内容/)).toBeVisible();
  expect(screen.getByText(/全书作者视角/)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '打开原始资料：投递约定' }));
  expect(app.onOpen).toHaveBeenCalledWith({ type: 'canon', id: 'c1' });
  await userEvent.click(screen.getByRole('button', { name: '第一封信' }));
  expect(app.onOpenChapter).toHaveBeenCalledWith('chapter/1');
  await userEvent.click(screen.getByRole('button', { name: /钟守\s*人物与世界/ }));
  await waitFor(() => expect(app.requests.some(request => request.url.pathname.endsWith('/wiki/entity/e2'))).toBe(true));
  expect(app.posts()).toHaveLength(0);
});

it('sends bounded pagination, alias search and chapter/type scope through separate query keys', async () => {
  const app = setup(url => url.pathname.endsWith('/wiki') ? response({ items: [entry], total: 31, has_more: url.searchParams.get('offset') === '0' }) : undefined);
  await screen.findByText(evidence.content);
  await userEvent.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(app.requests.some(request => request.url.searchParams.get('offset') === '30')).toBe(true));
  await userEvent.selectOptions(screen.getByLabelText('Wiki 章节范围'), 'chapter/1');
  await waitFor(() => expect(app.requests.some(request => request.url.pathname.includes('/wiki/entity/') && request.url.searchParams.get('chapter_id') === 'chapter/1')).toBe(true));
  expect(screen.getByText(/不代表角色已知/)).toBeVisible();
  await userEvent.selectOptions(screen.getByLabelText('Wiki 条目类型'), 'entity');
  fireEvent.change(screen.getByLabelText('搜索 Wiki 条目'), { target: { value: '小渡' } });
  await waitFor(() => expect(app.requests.some(({ url }) => url.pathname.endsWith('/wiki') && url.searchParams.get('q') === '小渡'
    && url.searchParams.get('chapter_id') === 'chapter/1' && url.searchParams.get('kind') === 'entity'
    && url.searchParams.get('limit') === '30' && url.searchParams.get('offset') === '0')).toBe(true));
  expect(app.posts()).toHaveLength(0);
});

it('does not replace a new selection with a late old-entry response', async () => {
  let release: ((value: Response) => void) | undefined;
  const app = setup(url => {
    if (url.pathname.endsWith('/wiki/entity/e1')) return new Promise<Response>(resolve => { release = resolve; });
    if (url.pathname.endsWith('/wiki/entity/e2')) return response(article({ id: 'e2', title: '钟守', sources: [{ ...evidence, content: '新条目的正文' }] }));
  }, { entries: [entry, second] });
  await userEvent.click(await screen.findByRole('button', { name: /钟守/ }));
  expect(await screen.findByText('新条目的正文')).toBeVisible();
  await act(async () => release?.(response(article())));
  expect(screen.queryByText(evidence.content)).not.toBeInTheDocument();
  app.switchProject();
  await waitFor(() => expect(app.requests.some(({ url }) => url.pathname.startsWith('/api/v1/projects/other/wiki'))).toBe(true));
  expect(screen.queryByText('新条目的正文')).not.toBeInTheDocument();
});

it('shows independent retry controls for list and detail failures', async () => {
  let failIndex = true, failDetail = true;
  setup(url => {
    if (url.pathname.endsWith('/wiki') && failIndex) return response({ detail: { message: '目录暂不可用' } }, 503);
    if (url.pathname.includes('/wiki/entity/') && failDetail) return response({ detail: { message: '详情暂不可用' } }, 503);
  });
  expect(await screen.findByRole('alert')).toHaveTextContent('目录暂不可用');
  failIndex = false;
  await userEvent.click(screen.getByRole('button', { name: '重试 Wiki 条目' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('详情暂不可用');
  failDetail = false;
  await userEvent.click(screen.getByRole('button', { name: '重试 Wiki 详情' }));
  expect(await screen.findByText(evidence.content)).toBeVisible();
});

it('uses a fresh fingerprint and preserves the same submission after uncertain transport and remount', async () => {
  let reads = 0, attempts = 0, accepted = false;
  const app = setup((url, init) => {
    if (init?.method === 'POST') {
      attempts++;
      if (attempts === 1) throw Error('连接中断');
      accepted = true;
      return response(job('running'), 202);
    }
    if (url.pathname.includes('/wiki/entity/')) return response(article({ fingerprint: (++reads === 1 ? 'a' : 'b').repeat(64), job: accepted ? job('running') : null }));
  });
  await screen.findByText(evidence.content);
  expect(app.posts()).toHaveLength(0);
  await userEvent.click(screen.getByRole('button', { name: 'AI摘要' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('连接中断');
  const first = app.posts()[0].init!;
  expect(JSON.parse(String(first.body))).toEqual({ chapter_id: null, fingerprint: 'b'.repeat(64) });
  expect(pendingSubmissions('p').filter(item => item.operation.startsWith('wiki:'))).toHaveLength(1);
  app.remount();
  await screen.findByText(/上次提交尚未收到回执/);
  await userEvent.click(screen.getByRole('button', { name: 'AI摘要' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(app.posts()[1].init?.body).toBe(first.body);
  expect(new Headers(app.posts()[1].init?.headers).get('Idempotency-Key')).toBe(new Headers(first.headers).get('Idempotency-Key'));
  await waitFor(() => expect(pendingSubmissions('p').filter(item => item.operation.startsWith('wiki:'))).toHaveLength(0));
  expect(screen.getByRole('button', { name: 'AI摘要' })).toBeDisabled();
});

it('refreshes rejected source fingerprints and starts a new identity only after explicit retry', async () => {
  let changed = false;
  const app = setup((url, init) => {
    if (init?.method === 'POST') {
      if (!changed) { changed = true; return response({ detail: { code: 'WIKI_SOURCE_CHANGED' } }, 409); }
      return response(job('queued'), 202);
    }
    if (url.pathname.includes('/wiki/entity/')) return response(article({ fingerprint: (changed ? 'b' : 'a').repeat(64) }));
  });
  await screen.findByText(evidence.content);
  await userEvent.click(screen.getByRole('button', { name: 'AI摘要' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('原始资料已变化');
  expect(app.posts()).toHaveLength(1);
  expect(pendingSubmissions('p')).toHaveLength(0);
  await userEvent.click(screen.getByRole('button', { name: 'AI摘要' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(new Headers(app.posts()[0].init?.headers).get('Idempotency-Key')).not.toBe(new Headers(app.posts()[1].init?.headers).get('Idempotency-Key'));
  expect(JSON.parse(String(app.posts()[1].init?.body)).fingerprint).toBe('b'.repeat(64));
});

it('marks stale summaries after library refresh and removes cached evidence after deletion', async () => {
  let stale = false, deleted = false;
  const app = setup(url => url.pathname.includes('/wiki/entity/') ? deleted
    ? response({ detail: { message: '条目已删除' } }, 404)
    : response(article({ summary: { job_id: 'summary', stale, claims: [{ text: '林渡承诺送达信件', source_ids: ['canon:c1'] }], created_at: '2026-09-07' } })) : undefined);
  await screen.findByText('林渡承诺送达信件');
  expect(screen.getByRole('button', { name: 'AI摘要' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '查看依据：投递约定' }));
  expect(app.onOpen).toHaveBeenCalledWith({ id: 'c1', type: 'canon' });
  stale = true;
  await act(async () => refreshLibrary(app.client, 'p'));
  expect(await screen.findByText(/旧摘要已过期/)).toBeVisible();
  expect(screen.getByRole('button', { name: 'AI摘要' })).toBeEnabled();
  expect(app.posts()).toHaveLength(0);
  deleted = true;
  await act(async () => refreshLibrary(app.client, 'p'));
  expect(await screen.findByRole('alert')).toHaveTextContent('条目已删除');
  expect(screen.queryByText('林渡承诺送达信件')).not.toBeInTheDocument();
  expect(screen.queryByText(evidence.content)).not.toBeInTheDocument();
});

it.each(['cancelled', 'failed'])('requires confirmation for new generation after a %s unknown result', async status => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const app = setup((url, init) => init?.method === 'POST' ? response(job('queued'), 202) : undefined,
    { detail: article({ job: job(status, { recovery_reason: status === 'cancelled' ? 'result_unknown' : null,
      replacement_requires_confirmation: status === 'failed', allowed_actions: [] }) }) });
  await screen.findByText(evidence.content);
  await userEvent.click(screen.getByRole('button', { name: 'AI摘要' }));
  expect(confirm).toHaveBeenCalled();
  expect(app.posts()).toHaveLength(0);
  confirm.mockReturnValue(true);
  await userEvent.click(screen.getByRole('button', { name: 'AI摘要' }));
  await waitFor(() => expect(app.posts()).toHaveLength(1));
  expect(JSON.parse(String(app.posts()[0].init?.body)).confirm_unknown).toBe(true);
});

it('reuses existing durable resume and cancel endpoints with recovery revision and a stable retry key', async () => {
  let current = job('recovery_required', { recovery_reason: 'result_unknown', allowed_actions: ['resume', 'cancel'] });
  let attempts = 0;
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/resume')) {
      if (++attempts === 1) return response({ detail: '暂时无法恢复' }, 503);
      current = job('running'); return response(current, 202);
    }
    if (url.pathname.endsWith('/cancel')) { current = job('cancelled', { allowed_actions: [] }); return response(current); }
    if (url.pathname.includes('/wiki/entity/') && !init?.method) return response(article({ job: current }));
  });
  await userEvent.click(await screen.findByRole('button', { name: '恢复任务' }));
  expect(screen.getByRole('button', { name: 'AI摘要' })).toBeDisabled();
  expect(await screen.findByRole('alert')).toHaveTextContent('暂时无法恢复');
  expect(app.detailReads()).toHaveLength(1);
  expect(pendingSubmissions('p').filter(item => item.operation.startsWith('wiki-resume:'))).toHaveLength(1);
  await userEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(JSON.parse(String(app.posts()[0].init?.body))).toEqual({ expected_control_revision: 1, confirm_unknown: true });
  expect(new Headers(app.posts()[0].init?.headers).get('Idempotency-Key')).toBe(new Headers(app.posts()[1].init?.headers).get('Idempotency-Key'));
  await userEvent.click(await screen.findByRole('button', { name: '取消任务' }));
  await waitFor(() => expect(app.posts().some(({ url }) => url.pathname.endsWith('/ai/jobs/job-wiki/cancel'))).toBe(true));
  expect(await screen.findByText('已取消')).toBeVisible();
});

it('refreshes a conflicting resume revision and confirms the latest unknown outcome before retrying', async () => {
  let current = job('recovery_required', { allowed_actions: ['resume', 'cancel'] });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/resume')) {
      const command = JSON.parse(String(init?.body));
      if (command.expected_control_revision !== current.control_revision)
        return response({ detail: { code: 'JOB_CONTROL_CHANGED' } }, 409);
      current = job('running', { control_revision: 4 });
      return response(current, 202);
    }
    if (url.pathname.includes('/wiki/entity/') && !init?.method) return response(article({ job: current }));
  });
  await screen.findByRole('button', { name: '恢复任务' });
  current = job('recovery_required', { control_revision: 3, recovery_reason: 'result_unknown', allowed_actions: ['resume', 'cancel'] });
  await userEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('摘要任务状态已变化');
  expect(app.detailReads()).toHaveLength(2);
  expect(pendingSubmissions('p')).toHaveLength(0);
  await userEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  expect(confirm).toHaveBeenCalled();
  expect(app.posts()).toHaveLength(1);
  confirm.mockReturnValue(true);
  await userEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(app.posts().map(request => JSON.parse(String(request.init?.body)))).toEqual([
    { expected_control_revision: 1, confirm_unknown: false },
    { expected_control_revision: 3, confirm_unknown: true },
  ]);
  expect(new Headers(app.posts()[0].init?.headers).get('Idempotency-Key')).not.toBe(new Headers(app.posts()[1].init?.headers).get('Idempotency-Key'));
  expect(await screen.findByText('正在处理')).toBeVisible();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it.each(['WIKI_JOB_ACTIVE', 'JOB_NOT_RESUMABLE'])('refreshes task controls after a %s resume rejection', async code => {
  let current = job('recovery_required', { allowed_actions: ['resume', 'cancel'] });
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/resume')) {
      current = code === 'WIKI_JOB_ACTIVE'
        ? job('running', { id: 'new-wiki-job', control_revision: 3 })
        : job('cancelled', { control_revision: 3, allowed_actions: [] });
      return response({ detail: { code } }, 409);
    }
    if (url.pathname.includes('/wiki/entity/') && !init?.method) return response(article({ job: current }));
  });
  await userEvent.click(await screen.findByRole('button', { name: '恢复任务' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('摘要任务状态已变化');
  expect(app.detailReads()).toHaveLength(2);
  expect(screen.queryByRole('button', { name: '恢复任务' })).not.toBeInTheDocument();
  expect(screen.getByText(code === 'WIKI_JOB_ACTIVE' ? '正在处理' : '已取消')).toBeVisible();
  expect(pendingSubmissions('p')).toHaveLength(0);
  expect(app.posts()).toHaveLength(1);
});

it('polls detail at 1500ms only while a job is active and stops after completion', async () => {
  vi.useFakeTimers();
  let reads = 0;
  const app = setup(url => url.pathname.includes('/wiki/entity/')
    ? response(article({ job: ++reads < 3 ? job('running') : job('succeeded', { allowed_actions: [] }) })) : undefined);
  await act(async () => { await vi.advanceTimersByTimeAsync(20); });
  expect(within(screen.getByRole('article', { name: 'Wiki 条目详情' })).getByText('正在处理')).toBeVisible();
  const initial = app.detailReads().length;
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(app.detailReads()).toHaveLength(initial + 1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(screen.getByText('已完成')).toBeVisible();
  const complete = app.detailReads().length;
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(app.detailReads()).toHaveLength(complete);
});

it('stops active-job polling when the Wiki entry is deleted', async () => {
  vi.useFakeTimers();
  let reads = 0;
  const app = setup(url => url.pathname.includes('/wiki/entity/') ? ++reads === 1
    ? response(article({ job: job('running') })) : response({ detail: '条目已删除' }, 404) : undefined);
  await act(async () => { await vi.advanceTimersByTimeAsync(20); });
  expect(screen.getByText('正在处理')).toBeVisible();
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(screen.getByRole('alert')).toHaveTextContent('条目已删除');
  expect(screen.queryByText(evidence.content)).not.toBeInTheDocument();
  const deleted = app.detailReads().length;
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(app.detailReads()).toHaveLength(deleted);
});
