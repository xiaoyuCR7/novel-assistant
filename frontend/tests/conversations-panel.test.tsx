import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { ConversationsPanel } from '../src/features/ai/ConversationsPanel';
import type { ConversationThread } from '../src/lib/conversationApi';

const clients: QueryClient[] = [];
afterEach(() => { clients.forEach(c => c.clear()); clients.length = 0; localStorage.clear(); vi.unstubAllGlobals(); });
const thread = (id = 't1', patch: Partial<ConversationThread> = {}): ConversationThread => ({
  id, project_id: 'p', chapter_id: null, title: id === 'default' ? '默认会话' : '悬念方案',
  status: 'active', revision: 1, is_default: id === 'default', parent_conversation_id: null,
  branch_from_job_id: null, created_at: '2026-09-01', updated_at: '2026-09-01', job_count: 3, ...patch,
});
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
function setup(handler?: (url: URL, init?: RequestInit) => Response | Promise<Response> | undefined,
               props: Partial<Parameters<typeof ConversationsPanel>[0]> = {}) {
  const requests: { url: URL; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local'); requests.push({ url, init });
    const handled = handler?.(url, init); if (handled !== undefined) return handled;
    if (url.pathname.endsWith('/conversations') && !init?.method) return response({ items: [thread(), thread('default')], next_cursor: null, default_conversation_id: 'default' });
    if (url.pathname.endsWith('/conversations/t1') && !init?.method) return response(thread());
    if (url.pathname.endsWith('/conversations/default') && !init?.method) return response(thread('default'));
    throw Error(`Unexpected ${url} ${init?.method}`);
  }));
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }); clients.push(cache);
  const onSelect = vi.fn();
  const tree = (projectId = 'p') => <QueryClientProvider client={cache}><ConversationsPanel projectId={projectId} selectedId="t1" onSelect={onSelect} {...props} /></QueryClientProvider>;
  const app = render(tree());
  return { onSelect, requests, switchProject: () => app.rerender(tree('other')), remount: () => { app.unmount(); return render(tree()); } };
}

it('lists independent conversations and searches without generating text', async () => {
  const app = setup();
  expect(await screen.findByRole('button', { name: /悬念方案.*3/ })).toHaveAttribute('aria-current', 'true');
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索会话' }), { target: { value: '旧线索' } });
  await waitFor(() => expect(app.requests.some(r => r.url.searchParams.get('q') === '旧线索')).toBe(true));
  expect(app.requests.every(r => !r.init?.method)).toBe(true);
});

it('creates a thread and preserves an uncertain request id across remount', async () => {
  const bodies: Record<string, unknown>[] = [];
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/conversations') && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)); bodies.push(body);
      if (bodies.length === 1) return Promise.reject(Error('连接中断'));
      return response(thread(String(body.id), { title: String(body.title) }), 201);
    }
  });
  await screen.findByText('悬念方案');
  await userEvent.click(screen.getByRole('button', { name: '新建会话' }));
  await userEvent.clear(screen.getByLabelText('会话名称'));
  await userEvent.type(screen.getByLabelText('会话名称'), '方案二');
  await userEvent.click(screen.getByRole('button', { name: '创建会话' }));
  await screen.findByText('连接中断');
  app.remount();
  await userEvent.click(await screen.findByRole('button', { name: '核对并重试原操作' }));
  await waitFor(() => expect(app.onSelect).toHaveBeenCalledWith(bodies[0].id));
  expect(bodies).toHaveLength(2);
  expect(bodies[1]).toEqual(bodies[0]);
});

it('branches only at the selected completed message', async () => {
  const app = setup((url, init) => url.pathname.endsWith('/t1/branches') && init?.method === 'POST'
    ? response(thread(JSON.parse(String(init.body)).id, { parent_conversation_id: 't1', branch_from_job_id: 'j1' }), 201) : undefined,
  { branchFromJobId: 'j1' });
  await screen.findByText('悬念方案');
  await userEvent.click(screen.getByRole('button', { name: '从选中消息分支' }));
  await userEvent.click(screen.getByRole('button', { name: '创建分支' }));
  await waitFor(() => expect(app.onSelect).toHaveBeenCalledOnce());
  const posted = app.requests.find(r => r.init?.method === 'POST');
  expect(JSON.parse(String(posted?.init?.body))).toMatchObject({ from_job_id: 'j1' });
});

it('shows actionable task blocker and never claims deletion succeeded', async () => {
  const app = setup((url, init) => init?.method === 'PATCH' ? response({ detail: {
    code: 'CONVERSATION_TASK_ACTIVE', message: '此会话仍有未完成任务，请先取消任务。',
  } }, 409) : undefined);
  await screen.findByText('悬念方案');
  await userEvent.click(screen.getByRole('button', { name: '删除会话' }));
  await userEvent.click(screen.getByRole('button', { name: '移到回收站' }));
  await screen.findByText('此会话仍有未完成任务，请先取消任务。');
  expect(app.onSelect).not.toHaveBeenCalled();
});

it('checks unsaved work again before retrying a recovered mutation', async () => {
  const beforeChange = vi.fn().mockResolvedValueOnce(true).mockResolvedValue(false);
  const app = setup((url, init) => init?.method === 'POST' ? Promise.reject(Error('暂时断线')) : undefined,
    { beforeChange });
  await screen.findByText('悬念方案');
  await userEvent.click(screen.getByRole('button', { name: '新建会话' }));
  await userEvent.click(screen.getByRole('button', { name: '创建会话' }));
  await screen.findByText('暂时断线');
  app.remount();
  await userEvent.click(await screen.findByRole('button', { name: '核对并重试原操作' }));
  expect(beforeChange).toHaveBeenCalledTimes(2);
  expect(app.requests.filter(r => r.init?.method === 'POST')).toHaveLength(1);
});

it('does not navigate another project when a create response arrives late', async () => {
  let finish!: (response: Response) => void;
  const deferred = new Promise<Response>(resolve => { finish = resolve; });
  const app = setup((url, init) => init?.method === 'POST' ? deferred : undefined);
  await screen.findByText('悬念方案');
  await userEvent.click(screen.getByRole('button', { name: '新建会话' }));
  await userEvent.click(screen.getByRole('button', { name: '创建会话' }));
  await waitFor(() => expect(app.requests.some(r => r.init?.method === 'POST')).toBe(true));
  app.switchProject();
  finish(response(thread('created')));
  await screen.findByText('会话列表回执无效，请刷新后重试。');
  expect(app.onSelect).not.toHaveBeenCalled();
});

it('keeps the panel usable when a malformed list and missing selected detail are returned', async () => {
  const app = setup((url, init) => !init?.method ? url.pathname.endsWith('/conversations')
    ? response([]) : response({ detail: { code: 'CONVERSATION_NOT_FOUND', diagnostic_id: 'safe-404' } }, 404) : undefined);
  await screen.findByText('会话列表回执无效，请刷新后重试。');
  await screen.findByText('会话已不存在，请刷新会话列表。');
  expect(screen.getByText('诊断编号：safe-404')).toBeVisible();
  expect(screen.getByRole('button', { name: '新建会话' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: '删除会话' })).not.toBeInTheDocument();
  expect(app.requests.every(r => !r.init?.method)).toBe(true);
});

it('restores a deleted thread explicitly and then selects the restored receipt', async () => {
  const deleted = thread('deleted', { status: 'deleted', title: '旧方案' });
  const app = setup((url, init) => {
    if (init?.method === 'PATCH') return response({ ...deleted, status: 'active', revision: 2 });
    if (url.pathname.endsWith('/conversations') && url.searchParams.get('status') === 'deleted')
      return response({ items: [deleted], next_cursor: null, default_conversation_id: 'default' });
  });
  await screen.findByText('悬念方案');
  await userEvent.click(screen.getByRole('button', { name: '回收站' }));
  await userEvent.click(await screen.findByRole('button', { name: '恢复 旧方案' }));
  await waitFor(() => expect(app.onSelect).toHaveBeenCalledWith('deleted'));
  const patch = app.requests.find(r => r.init?.method === 'PATCH');
  expect(JSON.parse(String(patch?.init?.body))).toEqual({ expected_revision: 1, status: 'active' });
});
