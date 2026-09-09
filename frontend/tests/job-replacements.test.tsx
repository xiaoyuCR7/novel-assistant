import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../src/app/App';
import { emptyLibraryPage } from './library-fixtures';
import type { AIJob } from '../src/lib/api';

afterEach(() => {
  vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear();
});

function studio(options: { summary?: boolean; unknown?: boolean; published?: boolean;
  deferDetail?: boolean; deferSubmit?: boolean; lostReceipt?: boolean } = {}) {
  const project = { id: 'replace-p', title: 'Novel', premise: '', genre: '', target_words: 1000, daily_goal: 100, status: 'active' };
  const chapter = { id: 'replace-c', kind: 'chapter', title: 'Chapter', status: options.summary ? 'summary_pending' : 'drafting', order_index: 1, parent_id: null };
  const instructions = 'retain the whole instruction '.repeat(100);
  const source = { id: 'source-job', project_id: project.id, chapter_id: chapter.id,
    task_type: options.summary ? 'chapter_summary' : 'rewrite', status: options.unknown ? 'recovery_required' : 'failed',
    instructions, token_budget: 8192, control_revision: 1, context_snapshot: {}, result: {},
    allowed_actions: ['cancel', 'resume', 'replace'], recovery_reason: options.unknown ? 'result_unknown' : null,
    replacement_requires_confirmation: !!options.unknown,
    effects: options.published ? { summary_id: 'published-summary', ledger_pending: true } : {},
  };
  if (options.published) source.allowed_actions = ['cancel', 'resume'];
  const jobs: Array<AIJob & { replaces_job_id?: string }> = [source];
  const posts: Array<{ url: string; data: Record<string, unknown>; key: string | null }> = [];
  const preflights: Record<string, unknown>[] = [];
  let releaseDetail!: () => void;
  let releaseSubmit!: () => void;
  let didLoseReceipt = false;
  const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/preflight')) {
      const command = JSON.parse(String(init?.body)); preflights.push(command);
      return response({ required_input_tokens: 2000, token_budget: command.token_budget,
        output_token_budget: 4096, context_capacity: 32768, effective_input_limit: Math.min(command.token_budget, 28672),
        can_fit: true, estimated: true, scope: 'first-stage-hard-only', message: '仅首阶段估算。' });
    }
    if (init?.method === 'POST') {
      posts.push({ url, data: init.body ? JSON.parse(String(init.body)) : {}, key: new Headers(init.headers).get('Idempotency-Key') });
      if (url.endsWith('/cancel')) { source.status = 'cancelled'; source.allowed_actions = ['replace']; return response(source); }
      const next = { ...source, id: 'linked-job', status: 'queued', instructions, recovery_reason: null,
        replaces_job_id: source.id, allowed_actions: ['cancel'], effects: {} };
      if (options.lostReceipt && !didLoseReceipt) { didLoseReceipt = true; throw new TypeError('receipt lost'); }
      if (!jobs.some(job => job.id === next.id)) jobs.push(next);
      if (options.deferSubmit) return new Promise<Response>(resolve => { releaseSubmit = () => resolve(response(next)); });
      return response(next);
    }
    if (url.endsWith('/projects')) return response([project, { ...project, id: 'other-p', title: 'Other novel' }]);
    if (url.endsWith('/workspace/navigation')) return response({ project, nodes: [chapter] });
    const page = emptyLibraryPage(url); if (page) return response(page);
    if (url.endsWith('/chapters/replace-c')) return response({ content: 'current saved draft', contract: {}, current_version_id: 'current-version', revision: 9, status: chapter.status });
    if (url.includes('/versions/page?')) return response({ items: [], next_cursor: null });
    if (url.includes('/ai/jobs/page?')) {
      let items = url.includes(`kind=${options.summary ? 'summary' : 'writing'}`) ? jobs : [];
      if (url.includes('active_only=true')) items = items.filter(job => ['queued', 'running', 'cancel_requested', 'recovery_required'].includes(job.status));
      return response({ items: items.map(({ result, context_snapshot, ...job }) => ({ ...job, instructions: job.instructions?.slice(0, 1000), preview: '' })), next_cursor: null });
    }
    if (url.endsWith('/ai/jobs/source-job')) {
      if (options.deferDetail) return new Promise<Response>(resolve => { releaseDetail = () => resolve(response(source)); });
      return response(source);
    }
    if (url.endsWith('/ai/jobs/linked-job')) return response(jobs.at(-1));
    if (url.endsWith('/summary')) return response(null);
    if (url.endsWith('/settings/model')) return response({ mode: 'demo', model: '', base_url: '', has_api_key: false, external_consent: false });
    if (url.endsWith('/rag/health')) return response({ vectors: 'disabled', documents: 0 });
    if (url.endsWith('/progress')) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 1, completed_chapters: 0, daily_goal: 100 });
    return response([]);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const rendered = render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return { instructions, posts, source, preflights,
    releaseDetail: async () => { await waitFor(() => expect(releaseDetail).toBeTypeOf('function')); await act(async () => releaseDetail()); },
    releaseSubmit: async () => { await waitFor(() => expect(releaseSubmit).toBeTypeOf('function')); await act(async () => releaseSubmit()); },
    remountOther: async () => {
      rendered.unmount(); localStorage.setItem('studio:active-project', 'other-p');
      render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
      await screen.findByLabelText('给 AI 的消息');
    },
  };
}

async function replacementButton(summary = false) {
  await screen.findByLabelText('给 AI 的消息');
  if (summary) await userEvent.click(screen.getByRole('button', { name: '查看正文' }));
  return screen.findByRole('button', { name: '按当前设置新建' });
}

it('explicitly creates a linked new job from full instructions and current saved revision', async () => {
  const app = studio();
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  await userEvent.click(await replacementButton());
  await waitFor(() => expect(app.posts).toHaveLength(1));
  expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/当前.*设置|当前.*模型/));
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('8192'));
  expect(app.posts[0]).toMatchObject({ url: '/api/v1/projects/replace-p/ai/jobs', data: {
    project_id: 'replace-p', chapter_id: 'replace-c', task_type: 'rewrite', instructions: app.instructions,
    expected_revision: 9, token_budget: 8192, replaces_job_id: 'source-job', confirm_unknown: false,
  } });
  expect(app.posts[0].key).toMatch(/^[a-f\d-]{36}$/);
  expect(app.source.status).toBe('failed');
  expect(await screen.findByText('原任务：source-job')).toBeVisible();
});

it('uses explicitly selected project budget for a linked replacement and preflights that exact command', async () => {
  const app = studio();
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  const button = await replacementButton();
  fireEvent.change(screen.getByLabelText('输入预算 Token'), { target: { value: '20000' } });
  await userEvent.click(button);
  await waitFor(() => expect(app.posts).toHaveLength(1));
  expect(app.posts[0].data.token_budget).toBe(20000);
  expect(app.preflights).toEqual([app.posts[0].data]);
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('20000'));
});

it('warns about unknown outcomes and sends zero POSTs when confirmation is cancelled', async () => {
  const app = studio({ unknown: true });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(await replacementButton());
  await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/结果未知.*重复费用/)));
  expect(app.posts).toHaveLength(0);
  confirm.mockReturnValue(true);
  await userEvent.click(await replacementButton());
  await waitFor(() => expect(app.posts[0]?.data.confirm_unknown).toBe(true));
});

it('blocks replacement with a dirty manuscript and while the source detail is pending', async () => {
  const app = studio({ deferDetail: true });
  const button = await replacementButton();
  await userEvent.click(screen.getByRole('button', { name: '查看正文' }));
  const editor = screen.getByLabelText('章节正文');
  fireEvent.change(editor, { target: { value: 'UNSAVED' } });
  expect(button).toBeDisabled();
  fireEvent.change(editor, { target: { value: 'current saved draft' } });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  await userEvent.click(button);
  expect(button).toBeDisabled();
  fireEvent.change(editor, { target: { value: 'LATE UNSAVED' } });
  await app.releaseDetail();
  expect(app.posts).toHaveLength(0);
  expect(editor).toHaveValue('LATE UNSAVED');
});

it('does not submit when an old project detail arrives after leaving that project', async () => {
  const app = studio({ deferDetail: true });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  await userEvent.click(await replacementButton());
  await app.remountOther();
  await app.releaseDetail();
  expect(app.posts).toHaveLength(0);
  expect(confirm).not.toHaveBeenCalled();
});

it('does not show an old project replacement receipt in the new project', async () => {
  const app = studio({ deferSubmit: true });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  await userEvent.click(await replacementButton());
  await waitFor(() => expect(app.posts).toHaveLength(1));
  await app.remountOther();
  await app.releaseSubmit();
  expect(screen.queryByText(/已按当前设置新建任务/)).not.toBeInTheDocument();
  expect(screen.getByLabelText('当前小说项目')).toHaveValue('other-p');
});

it('reuses an unacknowledged replacement key when explicitly confirming the lost receipt', async () => {
  const app = studio({ lostReceipt: true });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  await userEvent.click(await replacementButton());
  await screen.findByText('receipt lost');
  await userEvent.click(screen.getByRole('button', { name: '用原请求确认提交' }));
  await waitFor(() => expect(app.posts).toHaveLength(2));
  expect(app.posts[1].key).toBe(app.posts[0].key);
  expect(app.posts[1].data).toEqual(app.posts[0].data);
  expect(app.preflights).toHaveLength(1);
});

it('requires separately cancelling the failed summary before linked fresh completion', async () => {
  const app = studio({ summary: true, unknown: true });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  const button = await replacementButton(true);
  expect(button).toBeDisabled();
  expect(screen.getByText(/先取消原总结任务/)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '取消任务' }));
  await waitFor(() => expect(button).toBeEnabled());
  await userEvent.click(button);
  await waitFor(() => expect(app.posts).toHaveLength(2));
  expect(app.posts.map(post => post.url)).toEqual(['/api/v1/projects/replace-p/ai/jobs/source-job/cancel', '/api/v1/projects/replace-p/chapters/replace-c/complete']);
  expect(app.posts[1].data).toEqual({ expected_revision: 9, replaces_job_id: 'source-job', confirm_unknown: true });
  expect(app.preflights).toHaveLength(0);
  expect(app.source.status).toBe('cancelled');
  expect(await screen.findByText('原任务：source-job')).toBeVisible();
});

it('only offers local repair once summary publication already exists', async () => {
  const app = studio({ summary: true, published: true });
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '查看正文' }));
  expect(screen.queryByRole('button', { name: '按当前设置新建' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '仅修复本地账本' })).toBeVisible();
  expect(app.posts).toHaveLength(0);
});
