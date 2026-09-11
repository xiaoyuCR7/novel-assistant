import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../src/app/App';
import { IdeaBoard } from '../src/features/ideas/IdeaBoard';
import { EntityStudio } from '../src/features/story/EntityStudio';
import { KnowledgeBase } from '../src/features/knowledge/KnowledgeBase';
import { emptyLibraryPage, emptyWorkspaceView } from './library-fixtures';
import type { ChapterDocument, ChapterSummary, StoryNode } from '../src/lib/types';
import type { AIJob } from '../src/lib/api';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const forms = [
  { kind: 'idea', fields: ['灵感标题', '灵感内容', '标签', '来源'], button: '收进灵感匣' },
  { kind: 'entity', fields: ['姓名或名称', '人物简介', '声音特征'], button: '保存人物' },
  { kind: 'canon', fields: ['事实关系', '事实内容', '证据或来源'], button: '确认事实' },
  { kind: 'timeline', fields: ['事件标题', '故事时间', '事件说明'], button: '加入时间线' },
] as const;

function createForm(kind: string) {
  const pending = deferred<void>();
  const create = vi.fn(() => pending.promise);
  const element = kind === 'idea' ? <IdeaBoard ideas={[]} onCreate={create} />
    : kind === 'entity' ? <EntityStudio entities={[]} onCreate={create} />
      : <KnowledgeBase canonFacts={[]} timeline={[]} onCreateCanon={kind === 'canon' ? create : vi.fn()}
        onCreateTimeline={kind === 'timeline' ? create : vi.fn()} />;
  return { ...render(element), pending, create };
}

it.each(forms.flatMap(form => form.fields.map(lateField => ({ ...form, lateField }))))(
  'preserves the entire $kind draft if $lateField changes during create', async ({ kind, fields, button, lateField }) => {
    const form = createForm(kind);
    for (const field of fields) fireEvent.change(screen.getByLabelText(field), { target: { value: field } });
    await userEvent.click(screen.getByRole('button', { name: button }));
    for (const field of fields) expect(screen.getByLabelText(field)).toBeEnabled();
    fireEvent.change(screen.getByLabelText(lateField), { target: { value: `${lateField} later` } });
    await act(async () => form.pending.resolve());
    for (const field of fields) expect(screen.getByLabelText(field)).toHaveValue(field === lateField ? `${field} later` : field);
    expect(form.create).toHaveBeenCalledTimes(1);
  },
);

it.each(forms)('clears unchanged $kind input only after successful create', async ({ kind, fields, button }) => {
  const form = createForm(kind);
  for (const field of fields) fireEvent.change(screen.getByLabelText(field), { target: { value: field } });
  await userEvent.click(screen.getByRole('button', { name: button }));
  expect(screen.getByLabelText(fields[0])).toHaveValue(fields[0]);
  await act(async () => form.pending.resolve());
  for (const field of fields) expect(screen.getByLabelText(field)).toHaveValue('');
});

it.each(forms)('guards same-tick duplicate $kind submits', async ({ kind, fields, button }) => {
  const form = createForm(kind);
  for (const field of fields) fireEvent.change(screen.getByLabelText(field), { target: { value: field } });
  const node = screen.getByRole('button', { name: button }).closest('form')!;
  act(() => { fireEvent.submit(node); fireEvent.submit(node); });
  expect(form.create).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: button })).toBeDisabled();
  await act(async () => form.pending.resolve());
  expect(screen.getByRole('button', { name: button })).toBeEnabled();
});

it.each(forms)('preserves $kind input and reports a rejection without retrying', async ({ kind, fields, button }) => {
  const form = createForm(kind);
  for (const field of fields) fireEvent.change(screen.getByLabelText(field), { target: { value: field } });
  await userEvent.click(screen.getByRole('button', { name: button }));
  await act(async () => form.pending.reject(Error('creation failed')));
  expect(await screen.findByRole('alert')).toHaveTextContent('creation failed');
  for (const field of fields) expect(screen.getByLabelText(field)).toHaveValue(field);
  expect(form.create).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('button', { name: button })).toBeEnabled();
});

it('treats entity kind as part of the full submitted draft', async () => {
  const form = createForm('entity');
  fireEvent.change(screen.getByLabelText('姓名或名称'), { target: { value: 'Alice' } });
  await userEvent.click(screen.getByRole('button', { name: '保存人物' }));
  await userEvent.selectOptions(screen.getByLabelText('实体类型'), 'location');
  await act(async () => form.pending.resolve());
  expect(screen.getByLabelText('姓名或名称')).toHaveValue('Alice');
  expect(screen.getByLabelText('实体类型')).toHaveValue('location');
});

it.each(forms)('does not mutate a new $kind form when an old unmounted request settles', async ({ kind, fields, button }) => {
  const old = createForm(kind);
  for (const field of fields) fireEvent.change(screen.getByLabelText(field), { target: { value: 'old' } });
  await userEvent.click(screen.getByRole('button', { name: button }));
  old.unmount();
  createForm(kind);
  fireEvent.change(screen.getByLabelText(fields[0]), { target: { value: 'new draft' } });
  await act(async () => old.pending.resolve());
  expect(screen.getByLabelText(fields[0])).toHaveValue('new draft');
});

function studio({ empty = false, pendingSave = false }: { empty?: boolean; pendingSave?: boolean } = {}) {
  const project = { id: 'draft-p', title: 'Novel', premise: '', genre: '', target_words: 1000, daily_goal: 100, status: 'active' };
  const a: StoryNode = { id: 'chapter-a', kind: 'chapter', title: 'Chapter A', status: 'drafting', order_index: 1, parent_id: null };
  const b: StoryNode = { ...a, id: 'chapter-b', title: 'Chapter B', order_index: 2 };
  let nodes = empty ? [] : [a, b];
  const documents: Record<string, ChapterDocument> = {
    [a.id]: { content: 'original A', contract: { purpose: 'purpose A', forbidden_revelations: ['secret A'] }, current_version_id: 'v1', revision: 1 },
    [b.id]: { content: 'original B', contract: {}, current_version_id: 'v2', revision: 1 },
  };
  const summary: ChapterSummary = { id: 'summary-a', chapter_id: a.id, version_id: 'v1', title: a.title,
    recap: 'recap A', details: { character_states: ['home'], end_state: 'night' }, origin: 'ai_generated',
    provider: 'demo', status: 'valid', revision: 1, content_hash: 'hash' };
  const candidate: AIJob = { id: 'candidate', project_id: project.id, chapter_id: a.id, task_type: 'rewrite', status: 'succeeded',
    context_snapshot: {}, result: { candidate_text: 'candidate A' }, allowed_actions: [], control_revision: 1, created_at: '2026-08-28T12:00:00' };
  const failed: AIJob = { ...candidate, id: 'paused', status: 'recovery_required', result: {}, allowed_actions: ['resume', 'replace', 'cancel'], created_at: '2026-08-28T11:00:00' };
  const summaryJob: AIJob = { ...failed, id: 'summary-paused', task_type: 'chapter_summary' };
  const writes: Array<{ url: string; body: unknown }> = [];
  const pending = deferred<Response>();
  const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, request?: RequestInit) => {
    const url = new URL(String(input), 'http://fixture');
    const path = url.pathname;
    if (request?.method && request.method !== 'GET') {
      writes.push({ url: path, body: request.body ? JSON.parse(String(request.body)) : undefined });
      if (path.endsWith(`/chapters/${a.id}`) && request.method === 'PUT') {
        if (pendingSave) return pending.promise;
        documents[a.id] = { ...documents[a.id], ...JSON.parse(String(request.body)), revision: 2 };
        return response(documents[a.id]);
      }
      return response(candidate);
    }
    if (path.endsWith('/projects')) return response([project]);
    if (path.endsWith('/quality/runs')) return response({ items: [], next_cursor: null });
    if (path.endsWith('/workspace/navigation')) return response({ project, nodes });
    const page = emptyLibraryPage(String(url)); if (page) return response(page);
    const view = emptyWorkspaceView(String(url), nodes); if (view) return response(view);
    if (path.endsWith('/ai/jobs/page')) {
      const current = url.searchParams.get('chapter_id');
      const jobs = current === a.id ? url.searchParams.get('kind') === 'summary' ? [summaryJob] : [failed, candidate] : [];
      return response({ items: jobs.filter(job => url.searchParams.get('active_only') !== 'true' || job.status === 'recovery_required')
        .map(({ result, context_snapshot, ...job }) => ({ ...job, preview: result.candidate_text ?? '' })), next_cursor: null });
    }
    if (path.endsWith('/ai/jobs/candidate')) return response(candidate);
    if (path.endsWith('/ai/jobs/paused')) return response(failed);
    if (path.endsWith('/ai/jobs/summary-paused')) return response(summaryJob);
    const chapterId = path.match(/\/chapters\/([^/]+)/)?.[1];
    if (chapterId) {
      if (!nodes.some(node => node.id === chapterId)) return response({ detail: { message: 'chapter missing', code: 'NOT_FOUND' } }, 404);
      if (path.endsWith('/versions')) return response([{ id: 'v1', summary: 'first', source: 'manual', word_count: 10, created_at: '2026-08-28' }]);
      if (path.endsWith('/summary')) return response(chapterId === a.id ? summary : null);
      if (path.endsWith(`/chapters/${chapterId}`)) return response(documents[chapterId]);
    }
    if (path.endsWith('/settings/model')) return response({ mode: 'demo', model: '', base_url: '', has_api_key: false, external_consent: false, context_capacity: 32768, output_token_budget: 4096 });
    if (path.endsWith('/rag/health')) return response({ vectors: 'disabled', documents: 0, ledger_pending: false });
    if (path.endsWith('/progress')) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: nodes.length, completed_chapters: 0, daily_goal: 100 });
    if (path.endsWith('/conflicts')) return response([]);
    throw Error(`Unexpected request ${url}`);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const rendered = render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return { a, b, client, writes, rendered,
    navigation: async (next: StoryNode[]) => {
      nodes = next;
      await act(async () => client.invalidateQueries({ queryKey: ['workspace', project.id, 'navigation'] }));
      await waitFor(() => expect(screen.getByRole('navigation', { name: '书脊轨道' }).querySelectorAll('li')).toHaveLength(next.length));
    },
    finishSave: async () => { await act(async () => pending.resolve(response({ ...documents[a.id], content: 'saved A', revision: 2 }))); },
    restoreA: async () => { nodes = [a, b]; documents[a.id] = { ...documents[a.id], content: 'restored server A', revision: 3 };
      await act(async () => Promise.all([client.invalidateQueries({ queryKey: ['workspace', project.id] }), client.invalidateQueries({ queryKey: ['chapter', project.id] })]));
      await screen.findByRole('button', { name: 'Chapter A' }); },
  };
}

async function openEditor() {
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '查看正文' }));
  return screen.findByLabelText('章节正文');
}

it('keeps unsaved manuscript in place when opening quality optimization', async () => {
  const app = studio();
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: '我的未保存新段落' } });
  await userEvent.click(screen.getByRole('button', { name: '质量优化' }));
  expect(await screen.findByText('请先保存正文和总结，再进入质量优化。')).toBeVisible();
  expect(editor).toHaveValue('我的未保存新段落');
  expect(screen.queryByRole('region', { name: '文章质量优化' })).not.toBeInTheDocument();
  expect(confirm).not.toHaveBeenCalled();
  expect(app.writes).toHaveLength(0);
});

it('opens quality workspace from creation without invoking a model', async () => {
  const app = studio();
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '质量优化' }));
  expect(await screen.findByRole('region', { name: '文章质量优化' })).toBeVisible();
  expect(screen.getByRole('button', { name: '创作' })).toHaveAttribute('aria-current', 'page');
  expect(screen.queryByLabelText('资料工作区')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '返回写作' }));
  expect(await screen.findByLabelText('给 AI 的消息')).toBeVisible();
  expect(app.writes).toHaveLength(0);
});

it.each([false, true])('retains manuscript and contract in the same editor when dirty A disappears (all=%s)', async all => {
  const app = studio();
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: 'UNSAVED A' } });
  fireEvent.change(screen.getByLabelText('本章目的'), { target: { value: 'UNSAVED purpose' } });
  fireEvent.change(screen.getByLabelText('禁止提前揭示'), { target: { value: 'UNSAVED secrets' } });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await app.navigation(all ? [] : [app.b]);
  expect(screen.getByLabelText('章节正文')).toBe(editor);
  expect(editor).toHaveValue('UNSAVED A');
  expect(editor).toBeEnabled();
  expect(screen.getByLabelText('本章目的')).toHaveValue('UNSAVED purpose');
  expect(screen.getByLabelText('禁止提前揭示')).toHaveValue('UNSAVED secrets');
  expect(screen.getByText(/当前章节.*不可用.*草稿.*保留/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '保存工作副本' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '完成本章' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '写入正文' })).toBeDisabled();
  for (const button of screen.getAllByRole('button', { name: '恢复任务' })) expect(button).toBeDisabled();
  for (const button of screen.getAllByRole('button', { name: '按当前设置新建' })) expect(button).toBeDisabled();
  fireEvent.keyDown(editor, { key: 's', ctrlKey: true });
  expect(app.writes).toHaveLength(0);
  expect(confirm).not.toHaveBeenCalled();
});

it('requires explicit confirmation to leave a missing dirty chapter', async () => {
  const app = studio();
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: 'UNSAVED A' } });
  await app.navigation([app.b]);
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(screen.getByRole('button', { name: /Chapter B/ }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText('章节正文')).toBe(editor);
  confirm.mockReturnValue(true);
  await userEvent.click(screen.getByRole('button', { name: /Chapter B/ }));
  await waitFor(() => expect(screen.getByLabelText('章节正文')).toHaveValue('original B'));
  expect(screen.queryByText(/当前章节.*不可用.*草稿.*保留/)).not.toBeInTheDocument();
});

it('restores source availability without replacing unsaved manuscript and contract', async () => {
  const app = studio();
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: 'UNSAVED A' } });
  fireEvent.change(screen.getByLabelText('本章目的'), { target: { value: 'my purpose' } });
  await app.navigation([]);
  await app.restoreA();
  expect(screen.getByLabelText('章节正文')).toBe(editor);
  expect(editor).toHaveValue('UNSAVED A');
  expect(screen.getByLabelText('本章目的')).toHaveValue('my purpose');
  expect(screen.getByRole('button', { name: '保存工作副本' })).toBeEnabled();
  expect(screen.queryByText(/当前章节.*不可用.*草稿.*保留/)).not.toBeInTheDocument();
});

it('retains a summary-only dirty session and disables its save while its source is missing', async () => {
  const app = studio();
  await openEditor();
  await userEvent.click(screen.getByText('章节总结与连续性'));
  await userEvent.click(screen.getByRole('button', { name: '编辑总结' }));
  const recap = screen.getByLabelText('章节总结');
  fireEvent.change(recap, { target: { value: 'UNSAVED summary A' } });
  await app.navigation([]);
  expect(screen.getByLabelText('章节总结')).toBe(recap);
  expect(recap).toHaveValue('UNSAVED summary A');
  expect(recap).toBeEnabled();
  expect(screen.getByRole('button', { name: '保存总结' })).toBeDisabled();
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(screen.getByRole('button', { name: '资料库' }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText('章节总结')).toBe(recap);
  await app.restoreA();
  expect(recap).toHaveValue('UNSAVED summary A');
  expect(screen.getByRole('button', { name: '保存总结' })).toBeEnabled();
});

it.each([false, true])('retains normal clean fallback when the selected chapter disappears (all=%s)', async all => {
  const app = studio();
  await openEditor();
  await app.navigation(all ? [] : [app.b]);
  if (all) expect(await screen.findByText('先在故事地图中建立章节')).toBeInTheDocument();
  else await waitFor(() => expect(screen.getByLabelText('章节正文')).toHaveValue('original B'));
});

it('allows project chat and the initial empty chapter state', async () => {
  studio({ empty: true });
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '查看正文' }));
  expect(screen.getByText('先在故事地图中建立章节')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled();
});

it('keeps the project discussion title separate from the first chapter', async () => {
  studio();
  await screen.findByLabelText('给 AI 的消息');
  await userEvent.click(screen.getByRole('button', { name: '全书讨论' }));
  expect(screen.getByRole('heading', { name: '聊聊这个故事' })).toBeInTheDocument();
});

it('releases pending save state without losing the dirty missing session', async () => {
  const app = studio({ pendingSave: true });
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: 'saved A' } });
  await userEvent.click(screen.getByRole('button', { name: '保存工作副本' }));
  fireEvent.change(editor, { target: { value: 'saved A plus later input' } });
  await app.navigation([]);
  await app.finishSave();
  expect(screen.getByLabelText('章节正文')).toBe(editor);
  expect(editor).toHaveValue('saved A plus later input');
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(screen.getByRole('button', { name: '资料库' }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText('章节正文')).toBe(editor);
  confirm.mockReturnValue(true);
  await userEvent.click(screen.getByRole('button', { name: '资料库' }));
  expect(await screen.findByText('项目素材库')).toBeInTheDocument();
});

it('keeps a contract-only dirty chapter session when its source disappears', async () => {
  const app = studio();
  const editor = await openEditor();
  fireEvent.change(screen.getByLabelText('禁止提前揭示'), { target: { value: 'author secret' } });
  await app.navigation([app.b]);
  expect(screen.getByLabelText('章节正文')).toBe(editor);
  expect(screen.getByLabelText('禁止提前揭示')).toHaveValue('author secret');
  expect(screen.getByRole('button', { name: '保存工作副本' })).toBeDisabled();
});

it('allows explicitly confirmed project discussion after all dirty chapter sources disappear', async () => {
  const app = studio();
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: 'UNSAVED A' } });
  await app.navigation([]);
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(screen.getByRole('button', { name: '全书讨论' }));
  expect(screen.getByLabelText('章节正文')).toBe(editor);
  confirm.mockReturnValue(true);
  await userEvent.click(screen.getByRole('button', { name: '全书讨论' }));
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(screen.queryByLabelText('章节正文')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled();
});

it('unlocks copyable input if a dirty source disappears during completion and never submits its follow-up generation', async () => {
  const app = studio({ pendingSave: true });
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: 'saved A' } });
  await userEvent.click(screen.getByRole('button', { name: '完成本章' }));
  expect(editor).toBeDisabled();
  await app.navigation([]);
  expect(screen.getByLabelText('章节正文')).toBe(editor);
  expect(editor).toBeEnabled();
  expect(screen.getByLabelText('本章目的')).toBeEnabled();
  expect(screen.getByLabelText('禁止提前揭示')).toBeEnabled();
  await app.finishSave();
  await waitFor(() => expect(screen.getByRole('button', { name: '保存工作副本' })).toBeDisabled());
  expect(app.writes).toHaveLength(1);
  expect(app.writes[0].url).toMatch(/\/chapters\/chapter-a$/);
  expect(editor).toHaveValue('saved A');
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  await userEvent.click(screen.getByRole('button', { name: '资料库' }));
  expect(confirm).toHaveBeenCalledTimes(1);
});
