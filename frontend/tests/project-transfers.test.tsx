import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { BackupRestore } from '../src/features/projects/BackupRestore';
import { ManuscriptExport } from '../src/features/projects/ManuscriptExport';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
});
const preview = { project_id: 'p1', title: '恢复的小说', format_version: 1, verified: true,
  counts: { chapters: 4, versions: 5, jobs: 6, assets: 2 }, conflict: false, warnings: [], archive_hash: 'a'.repeat(64) };

it('previews before restoring, binds confirmation to the selected archive and returns the project', async () => {
  const calls: RequestInit[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
    calls.push(init); return response(calls.length === 1 ? preview : { id: 'p1', title: '恢复的小说' });
  }));
  const onRestored = vi.fn(); render(<BackupRestore onRestored={onRestored} />);
  await userEvent.upload(screen.getByLabelText('项目备份 ZIP'), new File(['zip'], 'backup.zip'));
  await userEvent.click(screen.getByRole('button', { name: '检查备份' }));
  expect(await screen.findByText('恢复的小说')).toBeVisible();
  expect(screen.getByRole('button', { name: '恢复项目' })).toBeDisabled();
  await userEvent.click(screen.getByLabelText(/我已核对备份/));
  await userEvent.click(screen.getByRole('button', { name: '恢复项目' }));
  await waitFor(() => expect(onRestored).toHaveBeenCalledWith(expect.objectContaining({ id: 'p1' })));
  expect((calls[1].body as FormData).get('archive_hash')).toBe(preview.archive_hash);
  expect(calls[1].headers).toBeUndefined();
});

it('prevents overwriting existing projects and clears stale preview when the file changes', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ ...preview, conflict: true })));
  render(<BackupRestore onRestored={vi.fn()} />);
  await userEvent.upload(screen.getByLabelText('项目备份 ZIP'), new File(['zip'], 'backup.zip'));
  await userEvent.click(screen.getByRole('button', { name: '检查备份' }));
  expect(await screen.findByText(/同一项目已存在/)).toBeVisible();
  expect(screen.getByRole('button', { name: '恢复项目' })).toBeDisabled();
  await userEvent.upload(screen.getByLabelText('项目备份 ZIP'), new File(['next'], 'next.zip'));
  expect(screen.queryByText('恢复的小说')).not.toBeInTheDocument();
});

it('exports selected volumes and chapters in selection order without model requests', async () => {
  const fetcher = vi.fn(async () => new Response('正文', { headers: { 'Content-Type': 'text/plain' } }));
  vi.stubGlobal('fetch', fetcher);
  const create = vi.fn(() => 'blob:manuscript');
  Object.defineProperty(URL, 'createObjectURL', { value: create, configurable: true });
  Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true });
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  render(<ManuscriptExport projectId="p1" nodes={[
    { id: 'v1', title: '第一卷', kind: 'volume' },
    { id: 'c1', title: '第一章', kind: 'chapter', parent_id: 'v1' },
    { id: 'c2', title: '第二章', kind: 'chapter' },
  ]} />);
  await userEvent.click(screen.getByLabelText('选择卷章'));
  await userEvent.click(screen.getByRole('checkbox', { name: /第二章/ }));
  await userEvent.click(screen.getByRole('checkbox', { name: /第一卷/ }));
  fireEvent.change(screen.getByLabelText('导出顺序'), { target: { value: 'selection' } });
  fireEvent.change(screen.getByLabelText('正文来源'), { target: { value: 'working' } });
  await userEvent.click(screen.getByRole('button', { name: '下载正文' }));
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  expect(fetcher).toHaveBeenCalledOnce();
  const [url, init] = (fetcher.mock.calls as unknown as [string, RequestInit][])[0];
  expect(url).toBe('/api/v1/projects/p1/manuscript/export');
  expect(JSON.parse(String(init.body))).toEqual({ format: 'txt', source: 'working',
    node_ids: ['c2', 'v1'], order: 'selection' });
});

it('shows missing published versions without starting a download', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ detail: { code: 'MANUSCRIPT_VERSION_REQUIRED',
    message: '部分章节尚无正式版本，请先保存版本或选择工作副本。' } }, 409)));
  render(<ManuscriptExport projectId="p1" nodes={[{ id: 'c1', title: '第一章', kind: 'chapter' }]} />);
  await userEvent.click(screen.getByRole('button', { name: '下载正文' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('部分章节尚无正式版本');
});
