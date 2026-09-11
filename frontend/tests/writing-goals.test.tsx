import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { WritingGoals } from '../src/features/progress/WritingGoals';

const original = { goals: { revision: 0, target_words: 100000, daily_goal: 1500, weekly_chapters: 0, deadline: null as string | null },
  progress: { saved_words: 1200, saved_chapters: 2, draft_chapters: 1, pending_review_candidates: 3, author_completed_chapters: 1, chapter_count: 4 },
  week: { timezone: 'Asia/Shanghai (UTC+08:00)', start_date: '2026-09-07', end_date: '2026-09-13', today: '2026-09-11', completed_chapters: null } };
const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.forEach(client => client.clear()); clients.length = 0; vi.unstubAllGlobals(); });
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
function mount(onSaved = vi.fn()) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } }); clients.push(cache);
  const tree = (projectId: string) => <QueryClientProvider client={cache}><WritingGoals projectId={projectId} onSaved={onSaved} /></QueryClientProvider>;
  const app = render(tree('p1')); return { cache, app, switchProject: () => app.rerender(tree('p2')), onSaved };
}

it('shows saved work, candidate items and author completion separately without inventing weekly completion', async () => {
  const fetcher = vi.fn(async () => json(original)); vi.stubGlobal('fetch', fetcher); mount();
  expect(await screen.findByText('已保存正文')).toBeVisible();
  expect(screen.getByLabelText('已保存正文字数')).toHaveTextContent('1,200');
  expect(screen.getByLabelText('待审候选数量')).toHaveTextContent('3 份');
  expect(screen.getByLabelText('作者已完成章节数')).toHaveTextContent('1 / 4 章');
  expect(screen.getByText('本周完成数：暂无可靠历史')).toBeVisible();
  expect(screen.getByText(/2026-09-07.*2026-09-13/)).toBeVisible();
  expect(fetcher.mock.calls).toHaveLength(1);
});

it('saves editable goals and nullable deadline with the loaded revision, then refreshes dependent progress', async () => {
  let state = original;
  const saved: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_url, init?: RequestInit) => {
    if (init?.method === 'PUT') { const command = JSON.parse(String(init.body)); saved.push(command); state = { ...original, goals: { ...command, revision: 1 } }; }
    return json(state);
  }));
  const page = mount(); await screen.findByLabelText('全书目标字数');
  fireEvent.change(screen.getByLabelText('全书目标字数'), { target: { value: '120000' } });
  fireEvent.change(screen.getByLabelText('每日目标字数'), { target: { value: '0' } });
  fireEvent.change(screen.getByLabelText('每周目标章节数'), { target: { value: '3' } });
  fireEvent.change(screen.getByLabelText('截止日期（可选）'), { target: { value: '2026-12-31' } });
  fireEvent.click(screen.getByRole('button', { name: '保存创作目标' }));
  await screen.findByText('创作目标已保存。');
  expect(saved).toEqual([{ revision: 0, target_words: 120000, daily_goal: 0, weekly_chapters: 3, deadline: '2026-12-31' }]);
  expect(page.onSaved).toHaveBeenCalledOnce();
  page.app.unmount(); mount();
  expect(await screen.findByLabelText('截止日期（可选）')).toHaveValue('2026-12-31');
});

it('preserves local inputs on a revision conflict until explicitly loading the latest goals', async () => {
  let state = original;
  const fetcher = vi.fn(async (_url, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      state = { ...original, goals: { ...original.goals, revision: 1, target_words: 180000, weekly_chapters: 4 } };
      return json({ detail: { code: 'WRITING_GOALS_CHANGED', message: '目标已在其他页面更新', current: state.goals } }, 409);
    }
    return json(state);
  });
  vi.stubGlobal('fetch', fetcher); mount(); await screen.findByLabelText('全书目标字数');
  fireEvent.change(screen.getByLabelText('全书目标字数'), { target: { value: '125000' } });
  fireEvent.click(screen.getByRole('button', { name: '保存创作目标' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('其他页面');
  expect(screen.getByLabelText('全书目标字数')).toHaveValue(125000);
  fireEvent.click(await screen.findByRole('button', { name: '放弃当前输入并载入最新目标' }));
  expect(screen.getByLabelText('全书目标字数')).toHaveValue(180000);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(1);
});

it('cannot let an older in-flight GET overwrite a successful save', async () => {
  let readCount = 0, release!: (value: Response) => void;
  const stale = new Promise<Response>(resolve => { release = resolve; });
  const updated = { ...original, goals: { ...original.goals, revision: 1, weekly_chapters: 2 } };
  vi.stubGlobal('fetch', vi.fn(async (_url, init?: RequestInit) => {
    if (init?.method === 'PUT') return json(updated);
    readCount++; return readCount === 2 ? stale : json(original);
  }));
  const { cache } = mount(); await screen.findByLabelText('每周目标章节数');
  let refreshing!: Promise<void>;
  act(() => { refreshing = cache.refetchQueries({ queryKey: ['writing-goals', 'p1'] }); });
  await waitFor(() => expect(readCount).toBe(2));
  fireEvent.change(screen.getByLabelText('每周目标章节数'), { target: { value: '2' } });
  fireEvent.click(screen.getByRole('button', { name: '保存创作目标' }));
  await screen.findByText('创作目标已保存。');
  await act(async () => { release(json(original)); await refreshing; });
  expect(cache.getQueryData(['writing-goals', 'p1'])).toEqual(updated);
  expect(screen.getByLabelText('每周目标章节数')).toHaveValue(2);
});

it('keeps project inputs isolated and preserves diagnostic IDs when reading fails', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.includes('/p2/')
    ? json({ detail: { message: '暂不可读取', diagnostic_id: 'goals-read-42' } }, 500) : json(original)));
  const page = mount(); await screen.findByLabelText('全书目标字数');
  fireEvent.change(screen.getByLabelText('全书目标字数'), { target: { value: '90000' } });
  page.switchProject();
  expect(await screen.findByRole('alert')).toHaveTextContent('goals-read-42');
  expect(screen.queryByLabelText('全书目标字数')).not.toBeInTheDocument();
});
