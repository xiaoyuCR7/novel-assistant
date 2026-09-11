import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../src/app/App';
import { ChatWorkspace } from '../src/features/ai/ChatWorkspace';
import { QualityWorkspace } from '../src/features/quality/QualityWorkspace';
import { emptyLibraryPage, emptyWorkspaceView } from './library-fixtures';
import { readWorkspaceState, saveWorkspaceJob, saveWorkspaceState } from '../src/lib/workspaceState';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.forEach(client => client.clear()); clients.length = 0; vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); sessionStorage.clear(); });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const storageKey = (project = 'p') => `studio:workspace:${encodeURIComponent(project)}`;
const save = (value: object, project = 'p') => localStorage.setItem(storageKey(project), JSON.stringify({ version: 1, ...value }));
const read = () => JSON.parse(localStorage.getItem(storageKey()) ?? '{}');
const project = { id: 'p', title: '雾城来信', premise: '', genre: '', target_words: 1000, daily_goal: 100, status: 'active' };
const nodes = ['c1', 'c2'].map((id, index) => ({ id, kind: 'chapter', title: `章节 ${id}`, order_index: index, status: 'drafting', parent_id: null }));
const job = (id: string, chapter: string | null = null) => ({ id, project_id: 'p', chapter_id: chapter, task_type: 'chat', status: 'succeeded',
  instructions: `原请求 ${id}`, preview: `预览 ${id}`, created_at: id, control_revision: 1, accepted_version_id: null, allowed_actions: [], context_snapshot: {}, result: { reply: `完整回复 ${id}` } });
const quality = (id: string) => ({ ...job(id), task_type: 'quality_workflow', result: { mode: 'polish' as const, chapters: [],
  messages: [{ role: 'writer' as const, chapter_id: 'c1', kind: 'handoff' as const, content: `交接 ${id}` }], completed_chapters: 0, total_chapters: 1 } });
type Handler = (url: URL, init?: RequestInit) => Response | Promise<Response> | undefined;
function fixture(handler?: Handler) {
  const calls: { url: URL; init?: RequestInit }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local'); calls.push({ url, init });
    const handled = handler?.(url, init); if (handled !== undefined) return handled;
    if (url.pathname.endsWith('/projects')) return response([project]);
    if (url.pathname.endsWith('/workspace/navigation')) return response({ project, nodes });
    const page = emptyLibraryPage(String(url)); if (page) return response(page);
    const view = emptyWorkspaceView(String(url), nodes); if (view) return response(view);
    if (url.pathname.endsWith('/settings/model')) return response({ mode: 'demo', base_url: '', model: '', has_api_key: false, external_consent: false });
    if (url.pathname.endsWith('/ai/jobs/page')) return response({ items: [], next_cursor: null });
    if (/\/chapters\/c[12]$/.test(url.pathname)) return response({ chapter_id: url.pathname.split('/').at(-1), project_id: 'p', content: '已保存正文', revision: 1, contract: {}, current_version_id: null });
    if (url.pathname.endsWith('/versions/page')) return response({ items: [], next_cursor: null });
    if (url.pathname.endsWith('/summary')) return response(null);
    if (url.pathname.endsWith('/progress')) return response({ current_words: 0, target_words: 1000, completion_ratio: 0, chapter_count: 2, completed_chapters: 0, daily_goal: 100 });
    if (url.pathname.endsWith('/quality/runs')) return response({ items: url.searchParams.get('active_only') === 'true' ? [] : [quality('new')], next_cursor: null });
    if (url.pathname.endsWith('/quality/runs/new')) return response(quality('new'));
    return response([]);
  }));
  return calls;
}
function mount(element = <App />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } }); clients.push(client);
  return { ...render(<QueryClientProvider client={client}>{element}</QueryClientProvider>), client };
}
const qualityView = <QualityWorkspace projectId="p" chapters={nodes} initialChapterId="c1" />;

it('remembers the last chapter and view with a fresh query cache after reopening', async () => {
  fixture();
  let page = mount();
  await screen.findByRole('heading', { name: '章节 c1', level: 1 });
  fireEvent.click(screen.getAllByRole('button', { name: /章节 c2/ })[0]);
  await screen.findByRole('heading', { name: '章节 c2', level: 1 });
  fireEvent.click(screen.getByRole('button', { name: '质量优化' }));
  await screen.findByRole('region', { name: '文章质量优化' });
  page.unmount(); page = mount();
  expect(await screen.findByRole('region', { name: '文章质量优化' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '返回写作' }));
  expect(await screen.findByRole('heading', { name: '章节 c2', level: 1 })).toBeVisible();
  expect(read()).toMatchObject({ view: 'ai', selectedNodeId: 'c2' });
});

it('keeps whole-book selection and safely falls back from deleted chapters', async () => {
  fixture(); save({ view: 'ai', selectedNodeId: 'project-chat' });
  let page = mount();
  expect(await screen.findByRole('heading', { name: '聊聊这个故事', level: 1 })).toBeVisible();
  page.unmount(); save({ view: 'ai', selectedNodeId: 'deleted' }); page = mount();
  expect(await screen.findByRole('heading', { name: '章节 c1', level: 1 })).toBeVisible();
  await waitFor(() => expect(read().selectedNodeId).toBe('c1'));
});

it('keeps the current chapter conversation selected when navigation order changes', async () => {
  fixture(url => {
    if (url.pathname.endsWith('/ai/jobs/current')) return response(job('current', 'c1'));
    if (url.pathname.endsWith('/ai/jobs/page') && url.searchParams.get('kind') === 'writing' && url.searchParams.get('chapter_id') === 'c1')
      return response({ items: url.searchParams.get('active_only') === 'true' ? [] : [job('current', 'c1')], next_cursor: null });
  });
  const page = mount();
  await screen.findByText('完整回复 current', { selector: 'p' });
  act(() => { page.client.setQueryData(['workspace', 'p', 'navigation'], { project, nodes: [nodes[1], nodes[0]] }); });
  expect(screen.getByRole('heading', { name: '章节 c1', level: 1 })).toBeVisible();
  expect(screen.getByText('完整回复 current', { selector: 'p' })).toBeVisible();
  expect(screen.getAllByRole('button', { name: /章节 c1/ })[0]).toHaveAttribute('aria-current', 'page');
  expect(read().selectedNodeId).toBe('c1');
});

it.each(['chat', 'continue', 'full_chapter', 'plan', 'draft', 'review', 'suggest', 'rewrite', 'scene_description'])('restores a selected %s conversation outside the first history page without submitting anything', async task => {
  save({ view: 'ai', selectedNodeId: 'c2', jobSelections: { 'p:c2': 'old' } });
  const calls = fixture(url => {
    if (url.pathname.endsWith('/ai/jobs/old')) return response({ ...job('old', 'c2'), task_type: task });
    if (url.pathname.endsWith('/ai/jobs/new')) return response(job('new', 'c2'));
    if (url.pathname.endsWith('/ai/jobs/page') && url.searchParams.get('kind') === 'writing' && url.searchParams.get('chapter_id') === 'c2')
      return response({ items: url.searchParams.get('active_only') === 'true' ? [] : [job('new', 'c2')], next_cursor: 'next' });
  });
  mount();
  expect(await screen.findByText('完整回复 old', { selector: 'p' })).toBeVisible();
  expect(screen.queryByText('完整回复 new', { selector: 'p' })).not.toBeInTheDocument();
  expect(calls.some(call => call.init?.method === 'POST')).toBe(false);
  expect(calls.filter(call => call.url.pathname.endsWith('/ai/jobs/old'))).toHaveLength(1);
});

it('keeps chapter and whole-book historical selections separate when switching scopes', async () => {
  save({ selectedNodeId: 'c2', jobSelections: { 'p:c2': 'chapter-old', 'p:project': 'book-old' } });
  fixture(url => {
    if (url.pathname.endsWith('/ai/jobs/chapter-old')) return response(job('chapter-old', 'c2'));
    if (url.pathname.endsWith('/ai/jobs/book-old')) return response(job('book-old'));
  });
  mount();
  await screen.findByText('完整回复 chapter-old', { selector: 'p' });
  fireEvent.click(screen.getAllByRole('button', { name: '全书讨论' })[0]);
  await screen.findByText('完整回复 book-old', { selector: 'p' });
  expect(screen.queryByText('完整回复 chapter-old', { selector: 'p' })).not.toBeInTheDocument();
  fireEvent.click(screen.getAllByRole('button', { name: /章节 c2/ })[0]);
  expect(await screen.findByText('完整回复 chapter-old', { selector: 'p' })).toBeVisible();
  expect(read().jobSelections).toEqual({ 'p:c2': 'chapter-old', 'p:project': 'book-old' });
});

it('retains an unavailable historical selection on a transient read error and retries only GET', async () => {
  save({ selectedNodeId: 'c2', jobSelections: { 'p:c2': 'old' } });
  let available = false;
  const calls = fixture(url => url.pathname.endsWith('/ai/jobs/old')
    ? available ? response(job('old', 'c2')) : response({ detail: 'offline' }, 503) : undefined);
  mount();
  expect(await screen.findByRole('alert')).toHaveTextContent('上次对话读取失败');
  expect(read().jobSelections['p:c2']).toBe('old');
  available = true;
  fireEvent.click(screen.getByRole('button', { name: '重试读取' }));
  expect(await screen.findByText('完整回复 old', { selector: 'p' })).toBeVisible();
  expect(calls.some(call => call.init?.method === 'POST')).toBe(false);
});

it('a late missing quality response cannot discard a newer selection', async () => {
  save({ qualityRunId: 'old' });
  let resolve!: (value: Response) => void;
  fixture(url => url.pathname.endsWith('/quality/runs/old') ? new Promise<Response>(done => { resolve = done; }) : undefined);
  mount(qualityView);
  await waitFor(() => expect(resolve).toBeTypeOf('function'));
  const history = await screen.findByRole('navigation', { name: '质量优化记录' });
  fireEvent.click(within(history).getByRole('button'));
  await screen.findByText('交接 new');
  await act(async () => { resolve(response({ detail: 'missing' }, 404)); });
  expect(screen.getByText('交接 new')).toBeVisible();
  expect(read().qualityRunId).toBe('new');
});

it('ignores corrupt navigation records, bounds saved IDs and never stores unknown payloads', () => {
  localStorage.setItem(storageKey(), '{broken');
  expect(readWorkspaceState('p')).toEqual({});
  localStorage.setItem(storageKey(), JSON.stringify({ version: 2, selectedNodeId: 'c2' }));
  expect(readWorkspaceState('p')).toEqual({});
  save({ view: 'ai', content: 'should not be retained', jobSelections: Object.fromEntries(Array.from({ length: 120 }, (_, index) => [`scope-${index}`, `id-${index}`])) });
  saveWorkspaceState('p', { qualityRunId: 'quality' });
  expect(read()).not.toHaveProperty('content');
  expect(Object.keys(read().jobSelections)).toHaveLength(100);
  saveWorkspaceJob('p', 'new-scope', 'new-id');
  expect(read().jobSelections['new-scope']).toBe('new-id');
  expect(Object.keys(read().jobSelections)).toHaveLength(100);
  expect(read().qualityRunId).toBe('quality');
});

it('isolates saved navigation per project and tolerates browser storage failures', () => {
  saveWorkspaceState('p', { view: 'quality', selectedNodeId: 'c2' });
  saveWorkspaceState('other', { view: 'ai', selectedNodeId: 'c1' });
  expect(readWorkspaceState('p')).toMatchObject({ view: 'quality', selectedNodeId: 'c2' });
  expect(readWorkspaceState('other')).toMatchObject({ view: 'ai', selectedNodeId: 'c1' });
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw Error('blocked'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw Error('blocked'); });
  expect(readWorkspaceState('p')).toEqual({});
  expect(() => saveWorkspaceState('p', { view: 'quality' })).not.toThrow();
  expect(() => saveWorkspaceJob('p', 'p:c2', 'old')).not.toThrow();
});

it.each(['missing', 'wrong-scope'])('forgets a %s restored conversation and shows the latest valid reply', async reason => {
  save({ selectedNodeId: 'c2', jobSelections: { 'p:c2': 'old' } });
  fixture(url => {
    if (url.pathname.endsWith('/ai/jobs/old')) return reason === 'missing' ? response({ detail: 'missing' }, 404) : response(job('old', 'c1'));
    if (url.pathname.endsWith('/ai/jobs/new')) return response(job('new', 'c2'));
    if (url.pathname.endsWith('/ai/jobs/page') && url.searchParams.get('kind') === 'writing' && url.searchParams.get('chapter_id') === 'c2')
      return response({ items: url.searchParams.get('active_only') === 'true' ? [] : [job('new', 'c2')], next_cursor: null });
  });
  mount();
  expect(await screen.findByText('完整回复 new', { selector: 'p' })).toBeVisible();
  expect(screen.queryByText('完整回复 old', { selector: 'p' })).not.toBeInTheDocument();
  await waitFor(() => expect(read().jobSelections?.['p:c2']).not.toBe('old'));
});

it('a late recovery response cannot replace a newer conversation selected by the author', async () => {
  save({ selectedNodeId: 'c2', jobSelections: { 'p:c2': 'old' } });
  let resolve!: (value: Response) => void;
  fixture(url => {
    if (url.pathname.endsWith('/ai/jobs/old')) return new Promise<Response>(done => { resolve = done; });
    if (url.pathname.endsWith('/ai/jobs/new')) return response(job('new', 'c2'));
    if (url.pathname.endsWith('/ai/jobs/page') && url.searchParams.get('kind') === 'writing' && url.searchParams.get('chapter_id') === 'c2')
      return response({ items: url.searchParams.get('active_only') === 'true' ? [] : [job('new', 'c2')], next_cursor: null });
  });
  mount();
  await waitFor(() => expect(resolve).toBeTypeOf('function'));
  fireEvent.click(await screen.findByRole('button', { name: '查看完整回复 new' }));
  await screen.findByText('完整回复 new', { selector: 'p' });
  await act(async () => { resolve(response(job('old', 'c2'))); });
  expect(screen.queryByText('完整回复 old', { selector: 'p' })).not.toBeInTheDocument();
  expect(read().jobSelections['p:c2']).toBe('new');
});

it('loads a saved quality conversation outside the first page after reopening', async () => {
  save({ view: 'quality', selectedNodeId: 'c2', qualityRunId: 'old' });
  const calls = fixture(url => url.pathname.endsWith('/quality/runs/old') ? response(quality('old')) : undefined);
  let page = mount(qualityView);
  expect(await screen.findByText('交接 old')).toBeVisible();
  page.unmount(); page = mount(qualityView);
  expect(await screen.findByText('交接 old')).toBeVisible();
  expect(read()).toMatchObject({ view: 'quality', selectedNodeId: 'c2', qualityRunId: 'old' });
  expect(calls.some(call => call.init?.method === 'POST')).toBe(false);
});

it('falls back from a removed quality conversation and retains the latest selection', async () => {
  save({ qualityRunId: 'gone' });
  const calls = fixture(url => url.pathname.endsWith('/quality/runs/gone') ? response({ detail: 'missing' }, 404) : undefined);
  mount(qualityView);
  expect(await screen.findByText('交接 new')).toBeVisible();
  await waitFor(() => expect(read().qualityRunId).toBe('new'));
  expect(calls.filter(call => call.url.pathname.endsWith('/quality/runs/gone'))).toHaveLength(1);
});

it('uses verified full detail for the complete original instruction, beyond the list preview limit', () => {
  const instructions = `${'原始要求'.repeat(250)}必须保留的最后要求`;
  const full = { ...job('long'), instructions };
  mount(<ChatWorkspace jobs={[{ ...full, instructions: instructions.slice(0, 1000) }]} selectedJobId="long" selectedJob={full}
    chapterTitle="雾城" hasChapter running={false} onSend={vi.fn()} onAccept={vi.fn()} onOpenManuscript={vi.fn()} />);
  expect(within(screen.getByRole('region', { name: 'AI 创作对话' })).getByText(instructions)).toBeVisible();
});
