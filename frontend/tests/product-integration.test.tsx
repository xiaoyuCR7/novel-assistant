import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../src/app/App';
import { SpendingPanel } from '../src/features/settings/SpendingPanel';
import { emptyLibraryPage, emptyWorkspaceView } from './library-fixtures';

const caches: QueryClient[] = [];
afterEach(() => { cleanup(); caches.forEach(c => c.clear()); caches.length = 0; vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const project = { id: 'p', title: '雾城', premise: '', genre: '', target_words: 1000, daily_goal: 100, status: 'active' };
const chapter = { id: 'c', title: '钟楼', kind: 'chapter', status: 'drafting', parent_id: null, order_index: 0 };
const model = { mode: 'api', base_url: 'https://example.com/v1', model: 'test', has_api_key: true, external_consent: true, context_capacity: 32768, output_token_budget: 4096 };
const thread = (id: string) => ({ id, project_id: 'p', chapter_id: 'c', title: id === 'default' ? '默认会话' : '钟楼另一方案', status: 'active', revision: 1, is_default: id === 'default', parent_conversation_id: null, branch_from_job_id: null, created_at: '', updated_at: '', job_count: 1 });
const job = { id: 'job-a', project_id: 'p', chapter_id: 'c', conversation_id: 'a', status: 'succeeded', task_type: 'chat', instructions: '另一方案的问题', preview: '另一方案回复', result: { reply: '独立会话的完整回复' }, context_snapshot: {}, control_revision: 1, allowed_actions: [] };
function mount(element = <App />) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } }); caches.push(cache);
  return render(<QueryClientProvider client={cache}>{element}</QueryClientProvider>);
}
function studio(handler?: (url: URL, init?: RequestInit) => Response | Promise<Response> | undefined) {
  const posts: Record<string, unknown>[] = [], reads: URL[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local'); reads.push(url);
    const handled = handler?.(url, init); if (handled !== undefined) return handled;
    if (url.pathname.endsWith('/projects')) return response([project]);
    if (url.pathname.endsWith('/workspace/navigation')) return response({ project, nodes: [chapter] });
    const library = emptyLibraryPage(String(url)); if (library) return response(library);
    const view = emptyWorkspaceView(String(url), [chapter]); if (view) return response(view);
    if (url.pathname.endsWith('/settings/model')) return response(model);
    if (url.pathname.endsWith('/chapters/c')) return response({ chapter_id: 'c', content: '已保存正文', revision: 1, contract: {}, status: 'drafting' });
    if (url.pathname.endsWith('/versions/page')) return response({ items: [], next_cursor: null });
    if (url.pathname.endsWith('/summary')) return response(null);
    if (url.pathname.endsWith('/progress')) return response({ current_words: 5, target_words: 1000, completion_ratio: 0, chapter_count: 1, completed_chapters: 0, daily_goal: 100 });
    if (url.pathname.endsWith('/conversations')) return response({ items: [thread('default'), thread('a')], next_cursor: null, default_conversation_id: 'default' });
    if (url.pathname.includes('/conversations/')) return response(thread(url.pathname.split('/').at(-1)!));
    if (url.pathname.endsWith('/ai/jobs/page')) return response({ items: url.searchParams.get('kind') === 'writing' && url.searchParams.get('conversation_id') === 'a' && url.searchParams.get('active_only') !== 'true' ? [job] : [], next_cursor: null });
    if (url.pathname.endsWith('/ai/jobs/job-a')) return response(job);
    if (url.pathname.endsWith('/preflight')) return response({ required_input_tokens: 100, token_budget: 28672, output_token_budget: 4096, context_capacity: 32768, effective_input_limit: 28672, can_fit: true, estimated: true, scope: 'first-stage-hard-only', message: '输入预算足够。' });
    if (url.pathname.endsWith('/ai/jobs') && init?.method === 'POST') { posts.push(JSON.parse(String(init.body))); return response({ ...job, id: 'new', status: 'queued' }); }
    return response([]);
  }));
  return { posts, reads };
}

it('switches independent conversation, scopes submissions and restores selection with a fresh cache', async () => {
  const requests = studio(); let page = mount();
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByText('会话与历史'));
  await userEvent.click(await screen.findByRole('button', { name: /钟楼另一方案.*条消息/ }));
  expect(await screen.findByText('独立会话的完整回复', { selector: 'p' })).toBeVisible();
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '继续这个方案' } });
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(requests.posts).toHaveLength(1));
  expect(requests.posts[0]).toMatchObject({ conversation_id: 'a', chapter_id: 'c', instructions: '继续这个方案' });
  page.unmount(); page = mount();
  expect(await screen.findByText('独立会话的完整回复', { selector: 'p' })).toBeVisible();
  expect(requests.reads.some(url => url.pathname.endsWith('/ai/jobs/job-a') && url.searchParams.get('conversation_id') === 'a')).toBe(true);
  expect(requests.posts).toHaveLength(1);
});

it('keeps explicit default conversation compatible with old request bodies', async () => {
  const requests = studio(); mount();
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByText('会话与历史'));
  await userEvent.click(await screen.findByRole('button', { name: /默认会话.*条消息/ }));
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '默认上下文' } });
  await userEvent.click(screen.getByRole('button', { name: '发送消息' }));
  await waitFor(() => expect(requests.posts).toHaveLength(1));
  expect(requests.posts[0]).not.toHaveProperty('conversation_id');
});

it('saves zero project budget as a stop while leaving global budget unrestricted', async () => {
  let settings = { revision: 0, global_limit: null, project_limits: {} as Record<string, string>, prices: [{ base_url: model.base_url, model: model.model, input_per_million: '1', output_per_million: '2' }] };
  const saved: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/settings/model')) return response(model);
    if (init?.method === 'PUT') { const value = JSON.parse(String(init.body)); saved.push(value); settings = { ...value, revision: 1 }; return response(settings); }
    return response({ settings, totals: { settled_cny: '0', reserved_cny: '0', committed_cny: '0', unpriced_count: 0, estimated_count: 0, unresolved_count: 0 }, entries: [] });
  }));
  mount(<SpendingPanel projectId="p" />);
  fireEvent.change(await screen.findByLabelText('本小说累计预算（元）'), { target: { value: '0' } });
  await userEvent.click(screen.getByRole('button', { name: '保存费用设置' }));
  await screen.findByText('费用设置已保存，下次发送立即生效。');
  expect(saved[0]).toMatchObject({ global_limit: null, project_limits: { p: '0' } });
});

it('closing a pending todo prevents its late response from navigating or clearing later input', async () => {
  let release: ((value: Response) => void) | undefined;
  studio(url => {
    if (url.pathname.endsWith('/todos')) return response({ items: [{ id: 'todo', kind: 'task_recovery', title: '另一个会话的任务', detail: '等待处理', destination: 'ai', chapter_id: 'c', chapter_title: '钟楼', job_id: 'job-a', conversation_id: 'a', action_label: '打开中断任务' }], total: 1, counts: { task_recovery: 1 }, next_offset: null });
    if (url.pathname.endsWith('/conversations/a')) return new Promise<Response>(resolve => { release = resolve; });
  });
  mount(); await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '创作待办' }));
  await userEvent.click(await screen.findByRole('button', { name: '打开中断任务' }));
  await waitFor(() => expect(release).toBeTypeOf('function'));
  await userEvent.click(screen.getByRole('button', { name: '关闭对话框' }));
  fireEvent.change(screen.getByLabelText('给 AI 的消息'), { target: { value: '关闭待办后的新想法' } });
  await act(async () => release!(response(thread('a'))));
  expect(screen.getByLabelText('给 AI 的消息')).toHaveValue('关闭待办后的新想法');
  expect(JSON.parse(localStorage.getItem('studio:workspace:p') ?? '{}').conversationSelections).toBeUndefined();
});

it('a quality result for a deleted chapter does not silently open the first live chapter', async () => {
  const quality = { id: 'quality-old', project_id: 'p', status: 'succeeded', task_type: 'quality_workflow', result: { mode: 'polish', chapters: [{ chapter_id: 'deleted', title: '已移除的章节', status: 'ready', source_revision: 1, draft: 'old', candidate_text: 'new' }], messages: [], completed_chapters: 1, total_chapters: 1 }, context_snapshot: {}, effects: {}, allowed_actions: [] };
  studio(url => {
    if (url.pathname.endsWith('/quality/runs')) return response({ items: url.searchParams.get('active_only') === 'true' ? [] : [quality], next_cursor: null });
    if (url.pathname.endsWith('/quality/runs/quality-old')) return response(quality);
  });
  mount(); await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '质量优化' }));
  await userEvent.click(await screen.findByRole('button', { name: '打开章节' }));
  expect(await screen.findByText(/此章节已删除或不在当前目录中/)).toBeVisible();
  expect(screen.getByRole('region', { name: '文章质量优化' })).toBeVisible();
  expect(screen.queryByLabelText('章节正文')).not.toBeInTheDocument();
});
