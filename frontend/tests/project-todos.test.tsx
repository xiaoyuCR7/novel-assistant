import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { ProjectTodos } from '../src/features/projects/ProjectTodos';

const clients: QueryClient[] = [];
afterEach(() => { clients.forEach(client => client.clear()); clients.length = 0; vi.unstubAllGlobals(); });
const item = { id: 'candidate:j1', kind: 'writing_candidate', title: '写作候选待查看',
  detail: '请查看候选', destination: 'ai', chapter_id: 'chapter-2', chapter_title: '第二章',
  job_id: 'j1', conversation_id: 'thread-1', action_label: '查看候选' };
function setup(open?: () => Promise<void>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  const onOpen = vi.fn(open);
  const tree = (projectId: string) => <QueryClientProvider client={client}><ProjectTodos projectId={projectId} onOpen={onOpen} /></QueryClientProvider>;
  const view = render(tree('p1')); return { onOpen, switchProject: () => view.rerender(tree('p2')) };
}

it('shows full counts, pages without generation, and opens the exact chapter conversation', async () => {
  const fetcher = vi.fn(async (path: string) => new Response(JSON.stringify(path.includes('offset=30')
    ? { items: [{ ...item, id: 'candidate:j2', chapter_title: '第三章' }], total: 31, counts: { writing_candidate: 31 }, next_offset: null }
    : { items: [item], total: 31, counts: { writing_candidate: 31 }, next_offset: 30 })));
  vi.stubGlobal('fetch', fetcher); const app = setup();
  expect(await screen.findByText('第二章')).toBeVisible();
  expect(screen.getByText(/共 31 项/)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '查看候选' }));
  expect(app.onOpen).toHaveBeenCalledWith(item);
  await userEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(await screen.findByText('第三章')).toBeVisible();
  expect(fetcher.mock.calls.every(([path]) => path.startsWith('/api/v1/projects/p1/todos'))).toBe(true);
});

it('clears stale project rows when switching scope and refreshes after actions', async () => {
  let cleared = false;
  vi.stubGlobal('fetch', vi.fn(async (path: string) => new Response(JSON.stringify({
    items: path.includes('/p2/') || cleared ? [] : [item], total: path.includes('/p2/') || cleared ? 0 : 1,
    counts: {}, next_offset: null,
  }))));
  const app = setup(); await screen.findByText('第二章');
  cleared = true; await userEvent.click(screen.getByRole('button', { name: '刷新待办' }));
  await waitFor(() => expect(screen.queryByText('第二章')).not.toBeInTheDocument());
  app.switchProject();
  expect(await screen.findByText('当前没有待处理事项。')).toBeVisible();
});

it('keeps the list and exposes errors when asynchronous navigation fails', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [item], total: 1,
    counts: { writing_candidate: 1 }, next_offset: null }))));
  const app = setup(async () => { throw new Error('会话已归档，请刷新后重试。'); });
  await userEvent.click(await screen.findByRole('button', { name: '查看候选' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('会话已归档');
  expect(screen.getByText('第二章')).toBeVisible();
  expect(app.onOpen).toHaveBeenCalledOnce();
});
