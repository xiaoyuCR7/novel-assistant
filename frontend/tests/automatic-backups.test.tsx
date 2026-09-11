import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { AutomaticBackups } from '../src/features/settings/AutomaticBackups';

const initial = { settings: { enabled: false, interval_hours: 24, retention_count: 7, revision: 0 },
  last_checked_at: null, last_success_at: null, last_error: null, backups: [] };
afterEach(() => vi.unstubAllGlobals());
function show() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><AutomaticBackups projectId="p1" /></QueryClientProvider>);
  return cache;
}
it('does not start backups on mount and saves an explicit opt-in policy', async () => {
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => new Response(JSON.stringify(init?.method === 'PUT'
    ? { ...initial, settings: { ...initial.settings, enabled: true, revision: 1 } } : initial)));
  vi.stubGlobal('fetch', fetcher); show();
  await screen.findByLabelText('启用定期备份');
  expect(fetcher.mock.calls.every(call => !call[1]?.method)).toBe(true);
  fireEvent.click(screen.getByLabelText('启用定期备份'));
  fireEvent.click(screen.getByRole('button', { name: '保存备份设置' }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/v1/projects/p1/automatic-backups', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ ...initial.settings, enabled: true }) })));
});
it('retains input and diagnostic evidence on conflict, and starts a snapshot only on click', async () => {
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => init?.method === 'PUT'
    ? new Response(JSON.stringify({ detail: { message: '设置已改变', code: 'BACKUP_SETTINGS_CHANGED', diagnostic_id: 'abc123' } }), { status: 409 })
    : new Response(JSON.stringify({ ...initial, result: 'unchanged' })));
  vi.stubGlobal('fetch', fetcher); show();
  fireEvent.change(await screen.findByLabelText('保留备份数量'), { target: { value: '3' } });
  fireEvent.click(screen.getByRole('button', { name: '保存备份设置' }));
  expect(await screen.findByText('设置已改变')).toBeVisible();
  expect(screen.getByLabelText('保留备份数量')).toHaveValue(3);
  fireEvent.click(screen.getByRole('button', { name: '立即备份' }));
  expect(await screen.findByText('项目内容未变化，已保留最近的有效备份。')).toBeVisible();
  expect(fetcher.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(1);
});

it.each(['POST', 'PUT'] as const)('keeps committed %s state when an earlier GET arrives late', async method => {
  let reads = 0, finishLate!: () => void;
  const updated = { ...initial, settings: { ...initial.settings, enabled: true, revision: 1 },
    last_success_at: '2026-09-11T10:00:00Z',
    backups: [{ id: 'new-backup', created_at: '2026-09-11T10:00:00Z', size_bytes: 2000 }], result: 'created' };
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method) return new Response(JSON.stringify(updated));
    if (++reads === 1) return new Response(JSON.stringify(initial));
    return new Promise<Response>(resolve => {
      // Deliberately allow the transport to finish even after cancellation.
      finishLate = () => resolve(new Response(JSON.stringify(initial)));
    });
  }));
  const cache = show();
  await screen.findByLabelText('启用定期备份');
  fireEvent.click(screen.getByRole('button', { name: '刷新备份记录' }));
  await waitFor(() => expect(reads).toBe(2));
  if (method === 'PUT') fireEvent.click(screen.getByLabelText('启用定期备份'));
  fireEvent.click(screen.getByRole('button', { name: method === 'POST' ? '立即备份' : '保存备份设置' }));
  await screen.findByText(method === 'POST' ? '备份已创建并通过完整性校验。' : '备份设置已保存。');
  expect(cache.getQueryData(['automatic-backups', 'p1'])).toEqual(updated);
  await act(async () => { finishLate(); });
  await waitFor(() => expect(cache.isFetching({ queryKey: ['automatic-backups', 'p1'] })).toBe(0));
  expect(cache.getQueryData(['automatic-backups', 'p1'])).toEqual(updated);
  expect(screen.getByText('1 份')).toBeVisible();
  if (method === 'PUT') expect(screen.queryByText('放弃输入并载入最新备份设置')).not.toBeInTheDocument();
  cache.clear();
});

it('disables competing actions while a policy save is pending and releases them afterward', async () => {
  let finishSave!: () => void;
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') return new Promise<Response>(resolve => {
      finishSave = () => resolve(new Response(JSON.stringify({ ...initial, settings: { ...initial.settings, revision: 1 } })));
    });
    return new Response(JSON.stringify(initial));
  });
  vi.stubGlobal('fetch', fetcher);
  const cache = show();
  await screen.findByLabelText('启用定期备份');
  const backup = screen.getByRole('button', { name: '立即备份' });
  const refresh = screen.getByRole('button', { name: '刷新备份记录' });
  fireEvent.click(screen.getByRole('button', { name: '保存备份设置' }));
  expect(backup).toBeDisabled();
  expect(refresh).toBeDisabled();
  fireEvent.click(backup);
  expect(fetcher.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(0);
  await act(async () => { finishSave(); });
  await screen.findByText('备份设置已保存。');
  expect(backup).toBeEnabled();
  expect(refresh).toBeEnabled();
  cache.clear();
});
