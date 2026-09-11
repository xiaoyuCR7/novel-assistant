import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { QualityWorkspace } from '../src/features/quality/QualityWorkspace';
import { JobStatus } from '../src/features/ai/JobStatus';
import { pendingSubmissions } from '../src/features/ai/pendingSubmission';
import type { QualityRun, QualityReview } from '../src/lib/qualityApi';
import { readInputBudget, saveInputBudget } from '../src/features/ai/inputBudget';

const clients: QueryClient[] = [];
afterEach(() => {
  clients.forEach(client => client.clear()); clients.length = 0;
  vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear();
});
const review: QualityReview = { scores: { readability: 82, engagement: 80, pacing: 78, clarity: 85, consistency: 90 },
  summary: '人物行动更清楚，结尾保留了悬念。', issues: [], next_guidance: '下一章延续信件的悬念。', preserves_story: true };
const run = (patch: Partial<QualityRun> = {}): QualityRun => ({ id: 'q1', task_type: 'quality_workflow', status: 'succeeded',
  control_revision: 2, context_snapshot: {}, allowed_actions: [], result: { mode: 'collaborate',
    chapters: [{ chapter_id: 'c1', title: '第一封信', source_revision: 3, draft: '原稿内容', candidate_text: '优化后的正文',
      before: { ...review, scores: { ...review.scores, readability: 60 } }, after: review, status: 'ready',
      handoff: { writer_message: '第一章交给你。', optimizer_message: '已完成节奏调整。', next_guidance: '下一章延续信件的悬念。' } }],
    messages: [{ role: 'writer', chapter_id: 'c1', kind: 'handoff', content: '第一章交给你。' },
      { role: 'optimizer', chapter_id: 'c1', kind: 'feedback', content: '已完成节奏调整。' }],
    active_chapter_id: null, active_role: null, completed_chapters: 1, total_chapters: 1 }, ...patch });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
it('starts an explicit fresh review without reopening the latest old run or sending a model request', async () => {
  const state = setup(undefined, { startNew: true });
  await screen.findByRole('button', { name: '检测并优化正文' });
  await waitFor(() => expect(state.client.getQueryData(['quality', 'p', 'active'])).toBeDefined());
  expect(state.requests.some(item => item.url.pathname.endsWith('/quality/runs/q1'))).toBe(false);
  expect(state.requests.some(item => item.init?.method === 'POST')).toBe(false);
});
type Handler = (url: URL, init?: RequestInit) => Response | Promise<Response> | undefined;
function setup(handler?: Handler, options: { detail?: QualityRun; empty?: boolean; startNew?: boolean; beforeStart?: () => boolean; beforeAccept?: (id: string) => boolean } = {}) {
  const requests: Array<{ url: URL; init?: RequestInit }> = [];
  const current = options.detail ?? run();
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local'); requests.push({ url, init });
    const handled = handler?.(url, init); if (handled !== undefined) return handled;
    if (url.pathname.endsWith('/settings/model')) return response({ mode: 'demo', model: '', context_capacity: 32768, output_token_budget: 4096 });
    if (url.pathname.endsWith('/quality/runs') && !init?.method)
      return response({ items: options.empty || url.searchParams.get('active_only') === 'true' ? [] : [current], next_cursor: null });
    if (url.pathname.endsWith('/quality/runs/q1') && !init?.method) return response(current);
    if (/\/chapters\/c[123]$/.test(url.pathname)) return response({ revision: Number(url.pathname.at(-1)) + 2, content: '已保存正文', contract: {}, current_version_id: 'v1' });
    throw Error(`Unexpected request ${url} ${init?.method ?? 'GET'}`);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } }); clients.push(client);
  const beforeStart = vi.fn(options.beforeStart ?? (() => true));
  const beforeAccept = vi.fn(options.beforeAccept ?? (() => true));
  const onAccepted = vi.fn(), onOpenChapter = vi.fn();
  const tree = (key = 1, projectId = 'p') => <QueryClientProvider client={client}>
    <QualityWorkspace key={`${key}:${projectId}`} projectId={projectId} chapters={[
      { id: 'c1', title: '第一封信' }, { id: 'c2', title: '钟楼来客' }, { id: 'c3', title: '雾中回声' },
    ]} initialChapterId="c1" startNew={options.startNew} beforeStart={beforeStart} beforeAccept={beforeAccept} onAccepted={onAccepted} onOpenChapter={onOpenChapter} />
  </QueryClientProvider>;
const rendered = render(tree());
  return { requests, client, beforeStart, beforeAccept, onAccepted, onOpenChapter,
    remount: () => rendered.rerender(tree(2)), switchProject: () => rendered.rerender(tree(2, 'other')),
    posts: () => requests.filter(item => item.init?.method === 'POST') };
}

it('shows two role conversations, before and after scores, and escaped candidate previews without generation', async () => {
  const app = setup(undefined, { detail: run({ result: { ...run().result, chapters: [{ ...run().result.chapters[0], candidate_text: '<img src=x onerror=alert(1)>优化后的正文' }] } }) });
  expect(within(await screen.findByRole('region', { name: '写作会话' })).getByText('第一章交给你。')).toBeVisible();
  expect(within(screen.getByRole('region', { name: '优化会话' })).getByText('已完成节奏调整。')).toBeVisible();
  expect(screen.getByText(/评分是辅助判断/)).toBeVisible();
  expect(screen.getByRole('table', { name: '第一封信质量评分' })).toHaveTextContent('60');
  await userEvent.click(screen.getByText('查看优化后的正文'));
  expect(screen.getByText('<img src=x onerror=alert(1)>优化后的正文')).toBeVisible();
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
  expect(app.posts()).toHaveLength(0);
});

it('starts selected chapters in story order with freshly read revisions', async () => {
  const app = setup((url, init) => url.pathname.endsWith('/quality/runs') && init?.method === 'POST' ? response(run()) : undefined, { empty: true });
  await screen.findByText('还没有质量优化记录');
  await userEvent.click(screen.getByRole('button', { name: '交替写作与优化' }));
  await userEvent.click(screen.getByLabelText('雾中回声'));
  await userEvent.click(screen.getByLabelText('钟楼来客'));
  fireEvent.change(screen.getByLabelText('优化要求'), { target: { value: '保持克制，提高悬念。' } });
  await userEvent.click(screen.getByRole('button', { name: '开始交替协作' }));
  await waitFor(() => expect(app.posts()).toHaveLength(1));
  expect(JSON.parse(String(app.posts()[0].init?.body))).toEqual({ mode: 'collaborate',
    chapters: [{ chapter_id: 'c1', expected_revision: 3 }, { chapter_id: 'c2', expected_revision: 4 }, { chapter_id: 'c3', expected_revision: 5 }],
    instructions: '保持克制，提高悬念。', token_budget: 28672, quality_target: 75 });
  expect(app.beforeStart).toHaveBeenCalledOnce();
});

it('uses the configured large model window and freezes it before an uncertain submission', async () => {
  let attempts = 0;
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/settings/model')) return response({ mode: 'demo', model: 'small-name-is-not-a-capacity', context_capacity: 1048576, output_token_budget: 8192 });
    if (url.pathname.endsWith('/quality/runs') && init?.method === 'POST') {
      if (++attempts === 1) throw Error('连接中断');
      return response(run());
    }
  }, { empty: true });
  await screen.findByText('还没有质量优化记录');
  expect(screen.getByLabelText('质量优化输入预算')).toHaveValue(1040384);
  await userEvent.click(screen.getByRole('button', { name: '检测并优化正文' }));
  await screen.findByText('连接中断');
  await act(async () => { app.client.setQueryData(['model-settings'], { mode: 'demo', model: '', context_capacity: 32768, output_token_budget: 4096 }); });
  await waitFor(() => expect(screen.getByLabelText('质量优化输入预算')).toHaveValue(28672));
  await userEvent.click(screen.getByRole('button', { name: '用原请求确认提交' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(JSON.parse(String(app.posts()[0].init?.body)).token_budget).toBe(1040384);
  expect(app.posts()[1].init?.body).toBe(app.posts()[0].init?.body);
  expect(new Headers(app.posts()[1].init?.headers).get('Idempotency-Key')).toBe(new Headers(app.posts()[0].init?.headers).get('Idempotency-Key'));
});

it('starts from the project cost limit and can follow the model without changing the project preference', async () => {
  saveInputBudget('p', '12000');
  const app = setup((url, init) => url.pathname.endsWith('/quality/runs') && init?.method === 'POST' ? response(run()) : undefined, { empty: true });
  await screen.findByText('还没有质量优化记录');
  expect(screen.getByLabelText('质量优化输入预算')).toHaveValue(12000);
  await userEvent.click(screen.getByText('质量目标与输入预算'));
  await userEvent.click(screen.getByRole('button', { name: '跟随模型' }));
  expect(screen.getByLabelText('质量优化输入预算')).toHaveValue(28672);
  expect(readInputBudget('p')).toBe('12000');
  fireEvent.change(screen.getByLabelText('质量优化输入预算'), { target: { value: '20000' } });
  await userEvent.click(screen.getByRole('button', { name: '检测并优化正文' }));
  await waitFor(() => expect(app.posts()).toHaveLength(1));
  expect(JSON.parse(String(app.posts()[0].init?.body)).token_budget).toBe(20000);
  expect(readInputBudget('p')).toBe('12000');
});

it('retains quality history but blocks new generation when model capacity is missing', async () => {
  const app = setup(url => url.pathname.endsWith('/settings/model') ? response({ mode: 'demo', model: 'large-model-name' }) : undefined);
  await screen.findByRole('table', { name: '第一封信质量评分' });
  expect(screen.getByRole('button', { name: '检测并优化正文' })).toBeDisabled();
  expect(screen.getByText(/填写有效的上下文容量/)).toBeVisible();
  fireEvent.submit(screen.getByLabelText('优化要求').closest('form')!);
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/填写有效的上下文容量/));
  expect(app.posts()).toHaveLength(0);
});

it('retains original request identity after uncertain transport and remount', async () => {
  let attempts = 0;
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/quality/runs') && init?.method === 'POST') {
      attempts++; if (attempts === 1) throw Error('连接中断'); return response(run());
    }
  }, { empty: true });
  await screen.findByText('还没有质量优化记录');
  await userEvent.click(screen.getByRole('button', { name: '检测并优化正文' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('连接中断');
  expect(pendingSubmissions('p')).toHaveLength(1);
  app.remount();
  await userEvent.click(await screen.findByRole('button', { name: '用原请求确认提交' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(app.posts()[1].init?.body).toBe(app.posts()[0].init?.body);
  expect(new Headers(app.posts()[1].init?.headers).get('Idempotency-Key')).toBe(new Headers(app.posts()[0].init?.headers).get('Idempotency-Key'));
  await waitFor(() => expect(pendingSubmissions('p')).toHaveLength(0));
});

it('stops before submission for dirty documents or empty saved prose', async () => {
  const app = setup(undefined, { empty: true, beforeStart: () => false });
  await screen.findByText('还没有质量优化记录');
  await userEvent.click(screen.getByRole('button', { name: '检测并优化正文' }));
  expect(app.posts()).toHaveLength(0);
  app.beforeStart.mockReturnValue(true);
  vi.mocked(fetch).mockImplementation(async (input) => {
    if (String(input).endsWith('/chapters/c1')) return response({ revision: 3, content: '  ', contract: {} });
    return response({ items: [], next_cursor: null });
  });
  await userEvent.click(screen.getByRole('button', { name: '检测并优化正文' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('请先保存需要优化的正文');
});

it('blocks candidate acceptance when the App guard declines and refreshes after a confirmed acceptance', async () => {
  let accepted = false;
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/q1/accept')) { accepted = true; return response({ id: 'v2' }); }
    if (url.pathname.endsWith('/quality/runs/q1') && !init?.method) return response(run(accepted ? { effects: { accepted_chapters: { c1: 'v2' } } } : {}));
  }, { beforeAccept: () => false });
  await userEvent.click(await screen.findByRole('button', { name: '采纳第一封信的优化稿' }));
  expect(app.posts()).toHaveLength(0);
  app.beforeAccept.mockReturnValue(true);
  await userEvent.click(screen.getByRole('button', { name: '采纳第一封信的优化稿' }));
  await waitFor(() => expect(app.onAccepted).toHaveBeenCalledWith('c1'));
  expect(await screen.findByRole('button', { name: '第一封信已采纳' })).toBeDisabled();
});

it('refreshes a stale control revision instead of repeatedly resuming with it', async () => {
  let conflict = false;
  const paused = run({ status: 'recovery_required', allowed_actions: ['resume', 'cancel'], recovery_reason: 'provider_failed' });
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/quality/runs/q1')) return response({ ...paused, control_revision: conflict ? 4 : 2 });
    if (url.pathname.endsWith('/q1/resume')) {
      if (!conflict) { conflict = true; return response({ detail: { code: 'JOB_CONTROL_CHANGED', message: '状态已变化' } }, 409); }
      return response(run());
    }
  }, { detail: paused });
  await userEvent.click(await screen.findByRole('button', { name: '恢复任务' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('状态已变化');
  await userEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(JSON.parse(String(app.posts()[1].init?.body)).expected_control_revision).toBe(4);
});

it('approves a paused quality candidate before resuming with the new revision', async () => {
  const paused = run({ status: 'recovery_required', allowed_actions: ['resume', 'cancel'], recovery_reason: 'quality_review_required',
    result: { ...run().result, active_chapter_id: 'c1', active_role: 'optimizer', completed_chapters: 0,
      chapters: [{ ...run().result.chapters[0], status: 'needs_review' }] } });
  const approved = run({ ...paused, control_revision: 3, result: { ...paused.result, chapters: run().result.chapters } });
  const app = setup((url) => {
    if (url.pathname.endsWith('/q1/approve')) return response(approved);
    if (url.pathname.endsWith('/q1/resume')) return response(run({ status: 'queued' }));
  }, { detail: paused });
  await userEvent.click(await screen.findByRole('button', { name: '认可此稿并继续' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(app.posts()[0].url.pathname).toMatch(/\/approve$/);
  expect(JSON.parse(String(app.posts()[1].init?.body))).toEqual({ expected_control_revision: 3, confirm_unknown: false });
});

it('does not show stale project output after switching projects', async () => {
  let release: ((value: Response) => void) | undefined;
  const app = setup((url) => {
    if (url.pathname.startsWith('/api/v1/projects/other/')) return response({ items: [], next_cursor: null });
    if (url.pathname.endsWith('/quality/runs/q1')) return new Promise<Response>(resolve => { release = resolve; });
  });
  await waitFor(() => expect(release).toBeDefined());
  app.switchProject();
  await act(async () => release?.(response(run())));
  expect(screen.queryByText('原稿内容')).not.toBeInTheDocument();
  expect(await screen.findByText('还没有质量优化记录')).toBeVisible();
});

it('refreshes after an uncertain approval so the same candidate does not need approval again', async () => {
  let approved = false;
  const paused = run({ status: 'recovery_required', allowed_actions: ['resume', 'cancel'], recovery_reason: 'quality_review_required',
    result: { ...run().result, active_chapter_id: 'c1', chapters: [{ ...run().result.chapters[0], status: 'needs_review' }] } });
  const app = setup((url) => {
    if (url.pathname.endsWith('/q1/approve')) { approved = true; throw Error('审批回执中断'); }
    if (url.pathname.endsWith('/quality/runs/q1')) return response(approved ? { ...paused, control_revision: 3, effects: { quality_approvals: { c1: 'approved-hash' } } } : paused);
    if (url.pathname.endsWith('/q1/resume')) return response(run());
  }, { detail: paused });
  await userEvent.click(await screen.findByRole('button', { name: '认可此稿并继续' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('审批回执中断');
  expect(screen.queryByRole('button', { name: '认可此稿并继续' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '恢复任务' }));
  await waitFor(() => expect(app.posts()).toHaveLength(2));
  expect(JSON.parse(String(app.posts()[1].init?.body)).expected_control_revision).toBe(3);
});

it('keeps later candidates disabled until preceding chapters have been accepted', async () => {
  let accepted = false;
  const detail = run({ result: { ...run().result, chapters: [...run().result.chapters,
    { ...run().result.chapters[0], chapter_id: 'c2', title: '钟楼来客' }], completed_chapters: 2, total_chapters: 2 } });
  setup((url) => {
    if (url.pathname.endsWith('/q1/accept')) { accepted = true; return response({ id: 'v2' }); }
    if (url.pathname.endsWith('/quality/runs/q1')) return response(accepted ? { ...detail, effects: { accepted_chapters: { c1: 'v2' } } } : detail);
  }, { detail });
  expect(await screen.findByRole('button', { name: '采纳钟楼来客的优化稿' })).toBeDisabled();
  expect(screen.getByText('请先采纳前一章的优化稿')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '采纳第一封信的优化稿' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '采纳钟楼来客的优化稿' })).toBeEnabled());
});

it('requires explicit confirmation before accepting a candidate with severe findings', async () => {
  const confirmed = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const app = setup((url, init) => {
    if (url.pathname.endsWith('/q1/accept')) {
      if (!JSON.parse(String(init?.body)).confirmed) return response({ detail: { code: 'SEVERE_CONFIRMATION_REQUIRED', message: '请核对低质量稿件后再采纳。' } }, 409);
      return response({ id: 'v2' });
    }
  });
  await userEvent.click(await screen.findByRole('button', { name: '采纳第一封信的优化稿' }));
  expect(confirmed).toHaveBeenCalledOnce();
  expect(app.onAccepted).not.toHaveBeenCalled();
  confirmed.mockReturnValue(true);
  await userEvent.click(screen.getByRole('button', { name: '采纳第一封信的优化稿' }));
  await waitFor(() => expect(app.onAccepted).toHaveBeenCalledWith('c1'));
  expect(JSON.parse(String(app.posts().at(-1)?.init?.body))).toEqual({ chapter_id: 'c1', confirmed: true });
});

it('does not accept a candidate while the workflow is running or paused', async () => {
  setup(undefined, { detail: run({ status: 'recovery_required', allowed_actions: ['cancel', 'resume'] }) });
  expect(await screen.findByRole('button', { name: '采纳第一封信的优化稿' })).toBeDisabled();
  expect(screen.getByText('流程结束或取消后可以采纳')).toBeVisible();
});

it('does not automatically resume an unknown model result without confirmation', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  const app = setup(undefined, { detail: run({ status: 'recovery_required', allowed_actions: ['resume', 'cancel'], recovery_reason: 'result_unknown' }) });
  await userEvent.click(await screen.findByRole('button', { name: '恢复任务' }));
  expect(confirm).toHaveBeenCalledOnce();
  expect(app.posts()).toHaveLength(0);
});

it.each([['quality.0.write', '第1章 · 写作'], ['quality.1.review', '第2章 · 质量检测'],
  ['quality.2.rewrite', '第3章 · 正文优化'], ['quality.3.final', '第4章 · 优化复核']])('translates the real workflow stage %s into a readable label', (stage, label) => {
  render(<JobStatus job={{ id: 'q1', task_type: 'quality_workflow', status: 'running', current_stage: stage }} />);
  expect(screen.getByRole('status')).toHaveTextContent(label);
  expect(screen.getByRole('status')).not.toHaveTextContent(stage);
});

it('reconciles a lost cancellation receipt instead of leaving a stopped run displayed as paused', async () => {
  let cancelled = false;
  const paused = run({ status: 'recovery_required', allowed_actions: ['resume', 'cancel'], recovery_reason: 'stage_failed' });
  const app = setup((url) => {
    const current = cancelled ? run({ status: 'cancelled' }) : paused;
    if (url.pathname.endsWith('/q1/cancel')) { cancelled = true; throw Error('取消回执中断'); }
    if (url.pathname.endsWith('/quality/runs/q1')) return response(current);
    if (url.pathname.endsWith('/quality/runs')) return response({ items: url.searchParams.get('active_only') === 'true' && cancelled ? [] : [current], next_cursor: null });
  }, { detail: paused });
  await userEvent.click(await screen.findByRole('button', { name: '取消任务' }));
  expect(await screen.findByRole('heading', { name: '已取消' })).toBeVisible();
  expect(screen.queryByRole('button', { name: '恢复任务' })).not.toBeInTheDocument();
  expect(app.posts()).toHaveLength(1);
});

it('reconciles a lost acceptance receipt and refreshes the manuscript without posting acceptance twice', async () => {
  let accepted = false;
  const detail = run({ result: { ...run().result, chapters: [...run().result.chapters,
    { ...run().result.chapters[0], chapter_id: 'c2', title: '钟楼来客' }], completed_chapters: 2, total_chapters: 2 } });
  const app = setup((url) => {
    if (url.pathname.endsWith('/q1/accept')) { accepted = true; throw Error('采纳回执中断'); }
    if (url.pathname.endsWith('/quality/runs/q1')) return response(accepted ? { ...detail, effects: { accepted_chapters: { c1: 'v2' } } } : detail);
  }, { detail });
  await userEvent.click(await screen.findByRole('button', { name: '采纳第一封信的优化稿' }));
  await waitFor(() => expect(app.onAccepted).toHaveBeenCalledWith('c1'));
  expect(await screen.findByRole('button', { name: '第一封信已采纳' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '采纳钟楼来客的优化稿' })).toBeEnabled();
  expect(app.posts()).toHaveLength(1);
});

it('opens an older unfinished run directly even when newer history fills the first page', async () => {
  const paused = run({ status: 'recovery_required', allowed_actions: ['resume', 'cancel'] });
  const app = setup((url) => {
    if (url.pathname.endsWith('/quality/runs')) return response({ items: url.searchParams.get('active_only') === 'true' ? [paused] : [run({ id: 'newer' })], next_cursor: null });
    if (url.pathname.endsWith('/quality/runs/newer')) return response(run({ id: 'newer' }));
  }, { detail: paused });
  expect(await screen.findByRole('button', { name: '恢复任务' })).toBeVisible();
  expect(screen.getByRole('button', { name: '检测并优化正文' })).toBeDisabled();
  expect(within(screen.getByRole('navigation', { name: '质量优化记录' })).getAllByRole('button')).toHaveLength(2);
  expect(app.posts()).toHaveLength(0);
});

it('does not claim an acceptance succeeded when its receipt has no saved version and readback is unaccepted', async () => {
  const app = setup(url => url.pathname.endsWith('/q1/accept') ? response({}) : undefined);
  await userEvent.click(await screen.findByRole('button', { name: '采纳第一封信的优化稿' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('未收到有效采纳回执');
  expect(app.onAccepted).not.toHaveBeenCalled();
  expect(app.posts()).toHaveLength(1);
});

it('keeps a confirmed acceptance visible when the following detail refresh fails', async () => {
  let accepted = false;
  const app = setup((url) => {
    if (url.pathname.endsWith('/q1/accept')) { accepted = true; return response({ id: 'v2' }); }
    if (url.pathname.endsWith('/quality/runs/q1') && accepted) return response({ detail: { message: '详情暂不可用' } }, 503);
  });
  await userEvent.click(await screen.findByRole('button', { name: '采纳第一封信的优化稿' }));
  expect(await screen.findByRole('button', { name: '第一封信已采纳' })).toBeDisabled();
  expect(app.onAccepted).toHaveBeenCalledWith('c1');
  expect(app.posts()).toHaveLength(1);
});

it('keeps the viewed older active run selected after it finishes and leaves the active list', async () => {
  let finished = false;
  const old = run({ status: 'running', result: { ...run().result, messages: [{ role: 'optimizer', chapter_id: 'c1', kind: 'feedback', content: '旧协作的独有反馈' }] } });
  const recent = run({ id: 'newer' });
  const app = setup((url) => {
    if (url.pathname.endsWith('/quality/runs')) return response({ items: url.searchParams.get('active_only') === 'true' ? finished ? [] : [old] : [recent], next_cursor: null });
    if (url.pathname.endsWith('/quality/runs/q1')) return response(finished ? { ...old, status: 'succeeded' } : old);
    if (url.pathname.endsWith('/quality/runs/newer')) return response(recent);
  });
  expect(await screen.findByText('旧协作的独有反馈')).toBeVisible();
  finished = true;
  await act(async () => { await app.client.invalidateQueries({ queryKey: ['quality', 'p'] }); });
  await waitFor(() => expect(within(screen.getByRole('navigation', { name: '质量优化记录' })).getAllByRole('button')).toHaveLength(2));
  expect(within(screen.getByRole('navigation', { name: '质量优化记录' })).getByRole('button', { current: 'page' })).toHaveTextContent('已完成');
  expect(screen.getByText('旧协作的独有反馈')).toBeVisible();
  expect(app.requests.some(item => item.url.pathname.endsWith('/quality/runs/newer'))).toBe(false);
});

it('removes stale controls after the resumed run no longer exists on the server', async () => {
  let missing = false;
  const paused = run({ status: 'recovery_required', allowed_actions: ['resume', 'cancel'] });
  const app = setup((url) => {
    if (url.pathname.endsWith('/q1/resume')) { missing = true; return response({ detail: { code: 'QUALITY_RUN_NOT_FOUND', message: '任务已不存在' } }, 404); }
    if (url.pathname.endsWith('/quality/runs/q1') && missing) return response({ detail: { code: 'QUALITY_RUN_NOT_FOUND', message: '任务已不存在' } }, 404);
    if (url.pathname.endsWith('/quality/runs')) return response({ items: missing ? [] : [paused], next_cursor: null });
  }, { detail: paused });
  await userEvent.click(await screen.findByRole('button', { name: '恢复任务' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '恢复任务' })).not.toBeInTheDocument());
  expect(screen.queryByRole('button', { name: '取消任务' })).not.toBeInTheDocument();
  expect(pendingSubmissions('p')).toHaveLength(0);
  expect(app.posts()).toHaveLength(1);
});

it('explains why a partially accepted failed workflow needs a new run while permitting new work', async () => {
  setup(undefined, { detail: run({ status: 'failed', allowed_actions: ['cancel'], effects: { accepted_chapters: { c1: 'v2' } } }) });
  expect(await screen.findByText('本任务已有章节采纳，请对剩余章节新建质量任务。')).toBeVisible();
  expect(screen.queryByRole('button', { name: '恢复任务' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '检测并优化正文' })).toBeEnabled();
});
