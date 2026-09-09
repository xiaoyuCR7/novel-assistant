import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../src/app/App';
import { emptyLibraryPage } from './library-fixtures';
import { pendingSubmissions } from '../src/features/ai/pendingSubmission';
import { inputBudgetValue, readInputBudget, saveInputBudget } from '../src/features/ai/inputBudget';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); });

function studio(options: { canFit?: boolean; defer?: boolean; malformed?: boolean; lostReceipt?: boolean; summary?: boolean; staleRevision?: boolean } = {}) {
  const project = { id: 'budget-p', title: 'Novel', premise: '', genre: '', target_words: 1000, daily_goal: 100, status: 'active' };
  const chapter = { id: 'budget-c', kind: 'chapter', title: 'Chapter', status: 'drafting', order_index: 1, parent_id: null };
  const checks: Record<string, unknown>[] = [], posts: { data: Record<string, unknown>; key: string | null }[] = [];
  let release!: () => void;
  let lost = false;
  const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const page = emptyLibraryPage(url); if (page) return response(page);
    if (url.endsWith('/preflight')) {
      const command = JSON.parse(String(init?.body)); checks.push(command);
      if (options.staleRevision) return new Response(JSON.stringify({ detail: { code: 'SOURCE_CHANGED' } }), { status: 409 });
      const result = options.malformed ? {} : { required_input_tokens: options.canFit === false ? 15000 : 1000,
        token_budget: command.token_budget, output_token_budget: 4096, context_capacity: 131072,
        effective_input_limit: Math.min(command.token_budget, 126976), can_fit: options.canFit !== false,
        estimated: true, scope: 'first-stage-hard-only', message: '仅估算首阶段硬输入，后续生成阶段不保证可容纳。' };
      if (options.defer) return new Promise<Response>(resolve => { release = () => resolve(response(result)); });
      return response(result);
    }
    if (init?.method === 'POST' && url.endsWith('/ai/jobs')) {
      posts.push({ data: JSON.parse(String(init.body)), key: new Headers(init.headers).get('Idempotency-Key') });
      if (options.lostReceipt && !lost) { lost = true; throw Error('lost receipt'); }
      return response({ id: 'budget-job', status: 'queued', task_type: 'chat', result: {}, context_snapshot: {} });
    }
    if (url.endsWith('/projects')) return response([project, { ...project, id: 'budget-other', title: 'Other' }]);
    if (url.endsWith('/workspace/navigation')) return response({ project, nodes: [chapter] });
    if (url.endsWith('/chapters/budget-c')) return response({ content: 'saved draft', contract: {}, revision: 2, status: 'drafting' });
    if (url.includes('/versions/page?')) return response({ items: [], next_cursor: null });
    if (url.includes('/ai/jobs/page?')) return response({ items: [], next_cursor: null });
    if (url.endsWith('/summary')) return response(options.summary ? { id: 'budget-summary', chapter_id: chapter.id,
      version_id: 'v1', title: 'Chapter', recap: 'saved recap', details: {}, origin: 'ai_generated', provider: 'demo',
      status: 'valid', revision: 1, content_hash: 'hash' } : null);
    if (url.endsWith('/settings/model')) return response({ mode: 'demo', model: '', base_url: '', has_api_key: false,
      external_consent: false, output_token_budget: 4096, context_capacity: 131072 });
    if (url.endsWith('/rag/health')) return response({ vectors: 'disabled', documents: 0 });
    if (url.endsWith('/progress')) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 1, completed_chapters: 0, daily_goal: 100 });
    return response([]);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const rendered = render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return { checks, posts, client,
    release: async () => { await waitFor(() => expect(release).toBeTypeOf('function')); await act(async () => release()); },
    switchProject: async () => {
      rendered.unmount(); localStorage.setItem('studio:active-project', 'budget-other');
      render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
      await screen.findByLabelText('给 AI 的消息');
    },
  };
}

it('uses the chosen per-project input budget for both preflight and submission', async () => {
  const app = studio();
  const budget = await screen.findByLabelText('输入预算 Token');
  expect(budget).toHaveValue(12000);
  fireEvent.change(budget, { target: { value: '20000' } });
  expect(app.checks).toHaveLength(0);
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: 'continue with care' } });
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(app.posts).toHaveLength(1));
  expect(app.checks).toHaveLength(1);
  expect(app.checks[0]).toEqual(app.posts[0].data);
  expect(app.posts[0].data).toMatchObject({ token_budget: 20000, expected_revision: 2 });
  expect(localStorage.getItem('studio:input-budget:budget-p')).toBe('20000');
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('');
});

it.each(['', '255', '200001', '12000.5'])('blocks invalid input budget %s even with Enter and form submit', async value => {
  const app = studio();
  fireEvent.change(await screen.findByLabelText('输入预算 Token'), { target: { value } });
  const message = screen.getByLabelText('给 AI 的消息');
  fireEvent.change(message, { target: { value: 'retain me' } });
  fireEvent.keyDown(message, { key: 'Enter', code: 'Enter' });
  fireEvent.submit(message.closest('form')!);
  expect(await screen.findByText(/256.*200000.*整数/)).toBeVisible();
  expect(message).toHaveValue('retain me');
  expect(app.checks).toHaveLength(0); expect(app.posts).toHaveLength(0);
});

it.each([{ canFit: false }, { malformed: true }])('retains prompt and creates no job on rejected or malformed preflight %j', async options => {
  const app = studio(options);
  const message = await screen.findByLabelText('给 AI 的消息');
  fireEvent.change(message, { target: { value: 'retain me' } });
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  expect(await screen.findByRole('alert')).toHaveTextContent(options.canFit === false ? /15000.*12000.*4096.*131072/ : /预检/);
  expect(message).toHaveValue('retain me'); expect(app.posts).toHaveLength(0);
    expect(pendingSubmissions('budget-p')).toEqual([]);
});

it('preserves late chat input when the earlier preflight is accepted', async () => {
  const app = studio({ defer: true });
  const message = await screen.findByLabelText('给 AI 的消息');
  fireEvent.change(message, { target: { value: 'original request' } });
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(app.checks).toHaveLength(1));
  fireEvent.change(message, { target: { value: 'late new request' } });
  await app.release();
  await waitFor(() => expect(app.posts).toHaveLength(1));
  expect(app.posts[0].data.instructions).toBe('original request'); expect(message).toHaveValue('late new request');
});

it('rejects preflight completion after late manuscript input and retains both drafts', async () => {
  const app = studio({ defer: true });
  const message = await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '查看正文' }));
  fireEvent.change(message, { target: { value: 'my instruction' } });
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(app.checks).toHaveLength(1));
  fireEvent.change(screen.getByLabelText('章节正文'), { target: { value: 'late draft' } });
  await app.release();
  expect(app.posts).toHaveLength(0);
  expect(screen.getByLabelText('章节正文')).toHaveValue('late draft'); expect(message).toHaveValue('my instruction');
});

it('does not submit an old scope after the project changes during preflight', async () => {
  const app = studio({ defer: true });
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(app.checks).toHaveLength(1));
  await app.switchProject(); await app.release();
  expect(app.posts).toHaveLength(0);
});

it('replays the immutable pending command without another preflight or new budget', async () => {
  const app = studio({ lostReceipt: true });
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await screen.findByText('lost receipt');
  fireEvent.change(screen.getByLabelText('输入预算 Token'), { target: { value: '20000' } });
  await userEvent.click(screen.getByRole('button', { name: '用原请求确认提交' }));
  await waitFor(() => expect(app.posts).toHaveLength(2));
  expect(app.checks).toHaveLength(1); expect(app.posts[1]).toEqual(app.posts[0]);
});

it('rejects a stale observed chapter revision and retains the chat instruction', async () => {
  const app = studio({ staleRevision: true });
  const message = await screen.findByLabelText('给 AI 的消息');
  fireEvent.change(message, { target: { value: 'keep instruction' } });
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('SOURCE_CHANGED');
  expect(app.posts).toHaveLength(0); expect(message).toHaveValue('keep instruction');
  expect(pendingSubmissions('budget-p')).toEqual([]);
});

it('rechecks summary dirty state after deferred preflight', async () => {
  const app = studio({ defer: true, summary: true });
  const message = await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '查看正文' }));
  await userEvent.click(screen.getByText('章节总结与连续性'));
  await userEvent.click(screen.getByRole('button', { name: '编辑总结' }));
  const summary = screen.getByLabelText('章节总结');
  fireEvent.change(message, { target: { value: 'instruction' } });
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(app.checks).toHaveLength(1));
  fireEvent.change(summary, { target: { value: 'late recap' } });
  await app.release();
  expect(app.posts).toHaveLength(0); expect(summary).toHaveValue('late recap');
  expect(message).toHaveValue('instruction'); expect(pendingSubmissions('budget-p')).toEqual([]);
});

it('persists only valid per-project budgets and survives unavailable browser storage in this session', () => {
  saveInputBudget('storage-p', '20000');
  expect(readInputBudget('storage-p')).toBe('20000');
  expect(readInputBudget('storage-other')).toBeUndefined();
  saveInputBudget('storage-p', '255');
  expect(readInputBudget('storage-p')).toBe('20000');
  localStorage.setItem('studio:input-budget:invalid-storage-p', 'garbage');
  expect(readInputBudget('invalid-storage-p')).toBeUndefined();
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw Error('blocked'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw Error('blocked'); });
  saveInputBudget('session-p', '24000');
  expect(readInputBudget('session-p')).toBe('24000');
  expect(readInputBudget('session-other')).toBeUndefined();
  for (const value of ['NaN', 'Infinity', '256.5', '255', '200001', '']) expect(() => inputBudgetValue(value)).toThrow();
  expect(inputBudgetValue('256')).toBe(256); expect(inputBudgetValue('200000')).toBe(200000);
});
