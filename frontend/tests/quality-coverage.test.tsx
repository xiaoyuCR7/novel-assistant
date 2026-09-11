import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { QualityCoverage } from '../src/features/quality/QualityCoverage';

const clients: QueryClient[] = [];
afterEach(() => { clients.forEach(client => client.clear()); clients.length = 0; vi.unstubAllGlobals(); });
const item = { chapter_id: 'c2', title: '第二章', status: 'stale', reason: '正文已改变',
  job_id: 'q2', checked_at: '2026-09-11T01:00:00', can_review: true };
const page = { items: [item], total: 31, chapter_count: 31,
  counts: { unchecked: 4, stale: 10, pending: 15, confirmed: 2 }, next_offset: 30 };
function setup(onOpen = vi.fn()) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(cache);
  const tree = (projectId: string) => <QueryClientProvider client={cache}><QualityCoverage projectId={projectId} onOpen={onOpen} /></QueryClientProvider>;
  const rendered = render(tree('p1'));
  return { onOpen, switchProject: () => rendered.rerender(tree('p2')) };
}

it('shows full-book counts and opens a single review without generating', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify(page))); vi.stubGlobal('fetch', fetcher);
  const app = setup(); expect(await screen.findByText('第二章')).toBeVisible();
  expect(screen.getByText(/全书 31 章/)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '复核本章' }));
  expect(app.onOpen).toHaveBeenCalledWith({ action: 'review', chapter_id: 'c2', job_id: 'q2' });
  expect(fetcher).toHaveBeenCalledOnce();
});

it('filters and pages with isolated project queries', async () => {
  const paths: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string) => { paths.push(path);
    return new Response(JSON.stringify({ ...page, items: path.includes('/p2/') ? [] : [item] })); }));
  const app = setup(); await screen.findByText('第二章');
  await userEvent.click(screen.getByRole('button', { name: '下一页' }));
  await waitFor(() => expect(paths.at(-1)).toContain('offset=30'));
  fireEvent.change(screen.getByLabelText('审校状态'), { target: { value: 'stale' } });
  await waitFor(() => expect(paths.at(-1)).toContain('status=stale&limit=30&offset=0'));
  app.switchProject(); await waitFor(() => expect(paths.at(-1)).toContain('/p2/'));
  expect(screen.queryByText('第二章')).not.toBeInTheDocument();
});

it('keeps the report list on navigation errors and prevents empty-body review', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ...page,
    items: [{ ...item, can_review: false }] }))));
  setup(vi.fn(async () => { throw new Error('章节已删除'); }));
  expect(await screen.findByRole('button', { name: '复核本章' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '查看报告' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('章节已删除');
  expect(screen.getByText('第二章')).toBeVisible();
});
