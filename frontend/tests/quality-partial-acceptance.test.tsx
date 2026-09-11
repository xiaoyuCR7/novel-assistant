import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { QualityWorkspace } from '../src/features/quality/QualityWorkspace';
import { saveWorkspaceState } from '../src/lib/workspaceState';

const clients: QueryClient[] = [];
afterEach(() => { clients.forEach(c => c.clear()); clients.length = 0; vi.unstubAllGlobals(); localStorage.clear(); });
function setup(mode = 'polish', rejected = false, lostReceipt = false, diagnosticFailure = false) {
  const posts: Record<string, unknown>[] = [];
  let accepted = false;
  const review = { scores: { readability: 85, engagement: 85, pacing: 85, clarity: 85, consistency: 85 }, summary: '完整候选报告', issues: [], next_guidance: '', preserves_story: true };
  const run = () => ({ id: 'target-run', task_type: 'quality_workflow', status: 'succeeded', control_revision: 2,
    context_snapshot: {}, effects: accepted ? { accepted_chapters: { c1: 'saved-version' }, accepted_selections: { c1: ['hunk-one'] } } : {},
    result: { mode, chapters: [{ chapter_id: 'c1', title: '第一章', source_revision: 3, draft: '旧首段\n旧尾段', candidate_text: '新首段\n新尾段', before: review, after: review, status: 'ready', handoff: {} }], messages: [], completed_chapters: 1, total_chapters: 1 } });
  const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local');
    if (url.pathname.endsWith('/settings/model')) return response({ mode: 'demo', context_capacity: 32768, output_token_budget: 4096 });
    if (url.pathname.endsWith('/quality/runs')) return response({ items: [], next_cursor: null });
    if (url.pathname.endsWith('/target-run')) return response(run());
    if (url.pathname.endsWith('/target-run/chapters/c1/diff')) return response({ chapter_id: 'c1', mode, source_revision: 3,
      hunks: [{ id: 'hunk-one', old_text: '旧首段', new_text: '新首段' }, { id: 'hunk-two', old_text: '旧尾段', new_text: '新尾段' }] });
    if (url.pathname.endsWith('/target-run/accept')) {
      posts.push(JSON.parse(String(init?.body)));
      if (diagnosticFailure) return response({ detail: { code: 'INTERNAL_ERROR', message: '保存暂时失败', diagnostic_id: 'diag-quality-500' } }, 500);
      if (rejected) return response({ detail: { code: 'SOURCE_CHANGED', message: '正文版本已变化' } }, 409);
      accepted = true;
      if (lostReceipt) throw Error('采纳回执中断');
      return response({ id: 'saved-version' });
    }
    throw Error(`Unexpected request ${url}`);
  }));
  saveWorkspaceState('partial-project', { qualityRunId: 'old-run' });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } }); clients.push(client);
  const onAccepted = vi.fn();
  render(<QueryClientProvider client={client}><QualityWorkspace projectId="partial-project" initialRunId="target-run"
    chapters={[{ id: 'c1', title: '第一章' }]} onAccepted={onAccepted} /></QueryClientProvider>);
  return { posts, onAccepted };
}

it('previews server hunks and submits only selected ids, with explicit partial-review status', async () => {
  const app = setup();
  fireEvent.click(await screen.findByRole('button', { name: '查看正文差异' }));
  const first = await screen.findByRole('checkbox', { name: '采用修改 1' });
  expect(screen.getByRole('button', { name: '采纳选中修改' })).toBeDisabled();
  fireEvent.click(first);
  fireEvent.click(screen.getByRole('button', { name: '采纳选中修改' }));
  await waitFor(() => expect(app.onAccepted).toHaveBeenCalledWith('c1'));
  expect(app.posts).toEqual([{ chapter_id: 'c1', confirmed: false, selected_hunk_ids: ['hunk-one'] }]);
  expect(await screen.findByText(/已局部采纳.*尚未整体复核/)).toBeVisible();
});

it('shows comparison but disables partial acceptance for collaboration handoffs', async () => {
  const app = setup('collaborate');
  fireEvent.click(await screen.findByRole('button', { name: '查看正文差异' }));
  expect(await screen.findByText('新首段')).toBeVisible();
  expect(screen.getByRole('button', { name: '采纳选中修改' })).toBeDisabled();
  expect(screen.getByText(/交替协作仅支持整章采纳/)).toBeVisible();
  expect(app.posts).toHaveLength(0);
});

it('retains selection and reports a stale source without claiming successful adoption', async () => {
  const app = setup('polish', true);
  fireEvent.click(await screen.findByRole('button', { name: '查看正文差异' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: '采用修改 1' }));
  fireEvent.click(screen.getByRole('button', { name: '采纳选中修改' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('正文版本已变化');
  expect(screen.getByRole('checkbox', { name: '采用修改 1' })).toBeChecked();
  expect(app.onAccepted).not.toHaveBeenCalled();
});

it('reconciles a lost partial acceptance receipt only with the same saved selection', async () => {
  const app = setup('polish', false, true);
  fireEvent.click(await screen.findByRole('button', { name: '查看正文差异' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: '采用修改 1' }));
  fireEvent.click(screen.getByRole('button', { name: '采纳选中修改' }));
  await waitFor(() => expect(app.onAccepted).toHaveBeenCalledWith('c1'));
  expect(app.posts).toHaveLength(1);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('preserves the backend diagnostic id when partial acceptance fails', async () => {
  setup('polish', false, false, true);
  fireEvent.click(await screen.findByRole('button', { name: '查看正文差异' }));
  fireEvent.click(await screen.findByRole('checkbox', { name: '采用修改 1' }));
  fireEvent.click(screen.getByRole('button', { name: '采纳选中修改' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('诊断编号：diag-quality-500');
});
