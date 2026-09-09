import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../src/app/App';
import { ContextLens } from '../src/features/ai/ContextLens';
import { refreshLibrary } from '../src/features/library/refreshLibrary';

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); localStorage.clear(); });

const longText = '完整人物设定不能截断。'.repeat(1900) + '最后一句必须保留';
const summary = (id: string) => ({ id, type: 'idea', title: `素材${id}`, preview: '命中片段',
  revision: 1, is_pinned: false, status: 'captured', origin: null, deleted_at: null, purge_after: null });
const detail = (id: string) => ({ ...summary(id), content: id === 'A' ? longText : `全文${id}`, record: {} });

function studio(options: { deferred?: boolean; detailError?: boolean; deferMutation?: boolean; pages?: boolean; conflictError?: boolean; summary?: boolean } = {}) {
  const requests: string[] = [], writes: Record<string, unknown>[] = [];
  const releases = new Map<string, () => void>();
  let failDetail = !!options.detailError;
  let failView = false;
  let summaryStatus = 'valid';
  let changedContent = longText;
  const summaryRow = { ...summary('S'), type: 'summary', title: '章节总结', status: 'valid', origin: 'ai_generated' };
  let releaseMutation: (() => void) | undefined;
  const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  const project = { id: 'p', title: 'Novel', premise: '', genre: '', target_words: 1000, daily_goal: 100, status: 'active' };
  const nodes = [{ id: 'chapter', title: 'Chapter', kind: 'chapter', status: 'drafting' }];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input), path = new URL(url, 'http://local').pathname;
    requests.push(url);
    if (path.endsWith('/projects')) return response([project, { ...project, id: 'other', title: 'Other' }]);
    if (path.endsWith('/workspace/navigation')) return response({ project: url.includes('/projects/other/') ? { ...project, id: 'other', title: 'Other' } : project, nodes });
    if (path.endsWith('/workspace/views/library')) return options.conflictError
      ? new Response(JSON.stringify({ detail: { message: 'conflicts unavailable' } }), { status: 503 })
      : response({ memory_conflicts: [] });
    if (path.endsWith('/workspace/views/ideas')) return failView
      ? new Response(JSON.stringify({ detail: { message: 'ideas refresh unavailable' } }), { status: 503 })
      : response({ ideas: [{ id: 'saved-idea', title: '已保存的灵感', content: '保留缓存内容', tags: [], source: '', status: 'captured' }] });
    if (path.endsWith('/wiki')) return response({ items: [{ id: 'e', type: 'entity', title: 'Wiki 人物', preview: '人物简介', aliases: ['小渡'] }], total: 1, has_more: false });
    if (path.endsWith('/wiki/entity/e')) return response({ id: 'e', type: 'entity', title: 'Wiki 人物', aliases: ['小渡'], scope_chapter_id: null,
      sources: [{ id: 'A', type: 'idea', title: '素材A', content: 'Wiki 来源片段', revision: 1, source_hash: 'source', reason: '关联资料' }],
      links: [], state_history: [{ id: 'wiki-state', chapter_id: 'chapter', chapter_title: 'Chapter', data: { location: '雾城' }, revision: 1 }],
      fingerprint: 'a'.repeat(64), truncated: false, summary: null, job: null });
    if (path.endsWith('/library/search')) return response({ items: [summary('A')], total: 1, total_kind: 'bounded_candidates' });
    if (path.endsWith('/library/summary/S')) return summaryStatus === 'deleted'
      ? new Response(JSON.stringify({ detail: { code: 'MATERIAL_NOT_FOUND' } }), { status: 404 })
      : response({ ...summaryRow, status: summaryStatus, content: '已保存总结正文', record: { chapter_id: 'chapter', version_id: 'version' } });
    if (/\/library\/idea\/[AB]$/.test(path)) {
      const id = path.slice(-1);
      if (init?.method === 'PATCH' || init?.method === 'DELETE') {
        writes.push(init.body ? JSON.parse(String(init.body)) : { deleted: id });
        if (options.deferMutation) return new Promise<Response>(resolve => { releaseMutation = () => resolve(response(detail(id))); });
        return response(detail(id));
      }
      if (failDetail) return new Response(JSON.stringify({ detail: { message: 'detail unavailable' } }), { status: 503 });
      if (options.deferred) return new Promise<Response>(resolve => { releases.set(id, () => resolve(response(detail(id)))); });
      return response(id === 'A' ? { ...detail(id), content: changedContent } : detail(id));
    }
    if (path.endsWith('/library/page') || path.endsWith('/trash/page')) {
      const params = new URL(url, 'http://local').searchParams;
      const independent = params.get('category') === 'summary' || params.get('pinned') === 'true';
      const rows = independent ? [] : options.summary ? [summaryRow] : options.pages ? [summary(params.has('cursor') ? 'B' : 'A')] : [summary('A'), summary('B')];
      return response({ items: rows, total: independent ? 0 : 2, counts: { idea: 2, summary: 0 }, next_cursor: options.pages && !independent && !params.has('cursor') ? 'next' : null });
    }
    if (path.endsWith('/chapters/chapter')) return response({ content: 'saved', revision: 2, contract: {}, current_version_id: null });
    if (path.endsWith('/summary')) return response(null);
    if (path.endsWith('/ai/jobs/page')) return response({ items: [], next_cursor: null });
    if (path.endsWith('/settings/model')) return response({ mode: 'demo', model: '', output_token_budget: 4096, context_capacity: 131072 });
    if (path.endsWith('/rag/health')) return response({ vectors: 'disabled', documents: 2 });
    if (path.endsWith('/progress')) return response({ current_words: 0, target_words: 1000, daily_goal: 100, completion_ratio: 0, chapter_count: 1, completed_chapters: 0 });
    if (path.endsWith('/versions') || path.endsWith('/conflicts') || path.endsWith('/assets')) return response([]);
    throw Error(`Unexpected request: ${url}`);
  }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const rendered = render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
  return { requests, writes, client, releases, rendered,
    publishSummary: async (status: string) => { summaryStatus = status; await act(async () => refreshLibrary(client, 'p')); },
    refreshContent: async () => { changedContent = '外部更新后的正文'; await act(async () => refreshLibrary(client, 'p')); },
    failViewRefresh: async () => {
      failView = true;
      await act(async () => client.invalidateQueries({ queryKey: ['workspace', 'p', 'ideas'] }));
    },
    recoverView: () => { failView = false; },
    releaseMutation: async () => { await waitFor(() => expect(releaseMutation).toBeTypeOf('function')); await act(async () => releaseMutation!()); },
    switchProject: async () => {
      rendered.unmount(); localStorage.setItem('studio:active-project', 'other');
      render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
      await screen.findByLabelText('给 AI 的消息');
    },
    recover: () => { failDetail = false; },
    release: async (id: string) => { await waitFor(() => expect(releases.has(id)).toBe(true)); await act(async () => releases.get(id)!()); },
  };
}

it('hydrates search hit full detail before editing and preserves its complete source on save', async () => {
  const app = studio();
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  fireEvent.change(await screen.findByLabelText('搜索本项目素材'), { target: { value: '片段' } });
  await waitFor(() => expect(app.requests.some(url => url.includes('/library/search'))).toBe(true));
  await userEvent.click(screen.getByRole('button', { name: /素材A/ }));
  await userEvent.click(await screen.findByRole('button', { name: '编辑素材' }));
  expect(screen.getByLabelText('设定内容')).toHaveValue(longText);
  fireEvent.change(screen.getByLabelText('素材标题'), { target: { value: '仅改标题' } });
  await userEvent.click(screen.getByRole('button', { name: '保存素材' }));
  await waitFor(() => expect(app.writes).toHaveLength(1));
  expect(app.writes[0].content).toBe(longText);
  expect(app.requests.some(url => url.endsWith('/library/idea/A'))).toBe(true);
});

it('ordinary chat uses projected pages and navigation, never unbounded library/full workspace/assets', async () => {
  const app = studio();
  await screen.findByLabelText('给 AI 的消息');
  expect(app.requests.some(url => url.endsWith('/workspace') || url.endsWith('/library') || url.endsWith('/assets'))).toBe(false);
  expect(app.requests.some(url => url.includes('/wiki'))).toBe(false);
  expect(app.requests.some(url => url.includes('/library/page?'))).toBe(true);
  expect(app.requests.some(url => url.endsWith('/workspace/navigation'))).toBe(true);
  expect(app.requests.some(url => url.endsWith('/chapters/chapter'))).toBe(true);
});

it('opens Wiki from the library or toolbar only on demand and routes evidence into the material preview', async () => {
  const app = studio();
  await userEvent.click(await screen.findByRole('button', { name: '资料库' }));
  await screen.findByRole('button', { name: '打开小说 Wiki' });
  expect(app.requests.some(url => url.includes('/wiki'))).toBe(false);
  await userEvent.click(screen.getByRole('button', { name: '打开小说 Wiki' }));
  expect(await screen.findByText('Wiki 来源片段')).toBeVisible();
  expect(screen.getByLabelText('资料工作区')).toHaveValue('wiki');
  await userEvent.click(screen.getByRole('button', { name: '打开原始资料：素材A' }));
  expect(await screen.findByText(longText)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '关闭素材预览' }));
  await userEvent.selectOptions(screen.getByLabelText('资料工作区'), 'library');
  await screen.findByRole('button', { name: '打开小说 Wiki' });
  await userEvent.selectOptions(screen.getByLabelText('资料工作区'), 'wiki');
  expect(await screen.findByText('Wiki 来源片段')).toBeVisible();
});

it('opens a Wiki history chapter even when that chapter was already selected before entering Wiki', async () => {
  studio();
  await screen.findByLabelText('给 AI 的消息');
  for (let visit = 0; visit < 2; visit++) {
    await userEvent.click(screen.getByRole('button', { name: '资料库' }));
    await userEvent.click(await screen.findByRole('button', { name: '打开小说 Wiki' }));
    const article = await screen.findByRole('article', { name: 'Wiki 条目详情' });
    await userEvent.click(await within(article).findByRole('button', { name: 'Chapter' }));
    expect(await screen.findByLabelText('给 AI 的消息')).toBeVisible();
    expect(screen.queryByRole('article', { name: 'Wiki 条目详情' })).not.toBeInTheDocument();
  }
});

it('ignores delayed detail A after B selection, and ignores completion after close', async () => {
  const app = studio({ deferred: true });
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  await userEvent.click(await screen.findByRole('button', { name: /素材A/ }));
  expect(await screen.findByText('正在加载素材详情…')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /素材B/ }));
  await app.release('B');
  expect(await screen.findByText('全文B')).toBeVisible();
  await app.release('A');
  expect(screen.queryByText(longText)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '关闭素材预览' }));
  await userEvent.click(screen.getByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /素材A/ }));
  await userEvent.click(screen.getByRole('button', { name: '关闭素材预览' }));
  await app.release('A');
  expect(screen.queryByRole('button', { name: '编辑素材' })).not.toBeInTheDocument();
});

it('shows detail failure with retry, never mounts an editor from a preview', async () => {
  const app = studio({ detailError: true });
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  await userEvent.click(await screen.findByRole('button', { name: /素材A/ }));
  expect(await screen.findByText(/detail unavailable/)).toBeVisible();
  expect(screen.queryByRole('button', { name: '编辑素材' })).not.toBeInTheDocument();
  app.recover();
  await userEvent.click(screen.getByRole('button', { name: '重试素材详情' }));
  expect(await screen.findByText(longText)).toBeVisible();
});

it('does not publish a delayed old-project detail or reopen a dismissed detail dialog', async () => {
  const app = studio({ deferred: true });
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /素材A/ }));
  await screen.findByText('正在加载素材详情…');
  await userEvent.click(screen.getByRole('button', { name: '关闭对话框' }));
  await app.release('A');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /素材B/ }));
  await app.switchProject();
  await app.release('B');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.queryByText('全文B')).not.toBeInTheDocument();
});

it.each(['save', 'pin', 'delete'])('ignores late %s completion after selecting and editing a different material', async action => {
  const app = studio({ deferMutation: true });
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /素材A/ }));
  const edit = await screen.findByRole('button', { name: '编辑素材' });
  if (action === 'pin') await userEvent.click(screen.getByRole('button', { name: '固定素材' }));
  else {
    await userEvent.click(edit);
    if (action === 'save') await userEvent.click(screen.getByRole('button', { name: '保存素材' }));
    else {
      await userEvent.click(screen.getByRole('button', { name: '移到回收站' }));
      await userEvent.click(screen.getByRole('button', { name: '确认移到回收站' }));
    }
  }
  await waitFor(() => expect(app.writes).toHaveLength(1));
  await userEvent.click(screen.getByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /素材B/ }));
  await userEvent.click(await screen.findByRole('button', { name: '编辑素材' }));
  await app.releaseMutation();
  expect(screen.getByLabelText('设定内容')).toHaveValue('全文B');
  expect(screen.getByLabelText('素材标题')).toHaveValue('素材B');
});

it('resets loaded cursors and invalidates all project library projections after an edit', async () => {
  const app = studio({ pages: true });
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: '加载更多素材' }));
  await screen.findByRole('button', { name: /素材B/ });
  await userEvent.click(screen.getByRole('button', { name: /素材A/ }));
  await userEvent.click(await screen.findByRole('button', { name: '编辑素材' }));
  app.requests.length = 0;
  await userEvent.click(screen.getByRole('button', { name: '保存素材' }));
  await waitFor(() => expect(screen.queryByLabelText('设定内容')).not.toBeInTheDocument());
  const cached = app.client.getQueryData<{ pages: unknown[] }>(['library', 'p', 'page', 'all']);
  expect(cached?.pages).toHaveLength(1);
  expect(app.requests.some(url => url.includes('cursor=next'))).toBe(false);
  expect(app.requests.some(url => url.includes('pinned=true'))).toBe(true);
  expect(app.requests.some(url => url.includes('category=summary'))).toBe(true);
  expect(app.requests.some(url => url.endsWith('/workspace/navigation'))).toBe(true);
  await waitFor(() => expect(app.client.getQueryState(['library', 'p', 'detail', 'idea', 'A'])?.isInvalidated).toBe(false));
  expect(app.requests.some(url => url.endsWith('/library/idea/A'))).toBe(true);
});

it('shows failure to load editor conflicts instead of silently treating them as absent', async () => {
  studio({ conflictError: true });
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /素材A/ }));
  await userEvent.click(await screen.findByRole('button', { name: '编辑素材' }));
  expect(await screen.findByText(/conflicts unavailable/)).toBeVisible();
  expect(screen.getByRole('button', { name: '重试素材冲突' })).toBeVisible();
});

it('opens off-page context citations by typed identity and does not request nonmaterial hard context', async () => {
  const open = vi.fn();
  const fragment = { source_type: 'story_entity', source_id: 'offpage', reason: 'included', hard: false, estimated_tokens: 1 };
  render(<ContextLens fragments={[fragment, { ...fragment, source_type: 'chapter_summary', source_id: 'old-summary' },
    { ...fragment, citation: { type: 'canon', id: 'citation-id', title: 'Citation' } },
    { ...fragment, source_type: 'project_core' }, { ...fragment, source_type: 'chapter_contract' },
    { ...fragment, source_type: 'current_draft' }]} references={[]}
    selected={null} onOpen={open} onPin={vi.fn()} onEdit={vi.fn()} onClose={vi.fn()} />);
  const buttons = document.querySelectorAll<HTMLButtonElement>('.context-fragment');
  for (const button of buttons) await userEvent.click(button);
  expect(open.mock.calls).toEqual([[{ type: 'entity', id: 'offpage' }], [{ type: 'summary', id: 'old-summary' }], [{ type: 'canon', id: 'citation-id' }]]);
  expect(within(buttons[3]).getByText('故事核心')).toBeVisible();
});

it('shows the immutable relative path for an imported source detail', () => {
  render(<ContextLens fragments={[]} references={[]}
    selected={{
      id: 'source', type: 'source_document', title: '世界观', preview: '',
      content: '雾钟每天只能敲响七次', revision: 1, is_pinned: false,
      deleted_at: null, purge_after: null,
      record: { relative_path: 'import-book/资料/世界观.md' },
    }}
    onOpen={vi.fn()} onPin={vi.fn()} onEdit={vi.fn()} onClose={vi.fn()} />);

  expect(screen.getByText('import-book/资料/世界观.md')).toBeVisible();
});

it('reports cached specialized-view refresh errors without unmounting unsaved form input', async () => {
  const app = studio();
  await userEvent.click(await screen.findByRole('button', { name: '资料库' }));
  await userEvent.selectOptions(screen.getByLabelText('资料工作区'), 'ideas');
  const title = await screen.findByLabelText('灵感标题');
  fireEvent.change(title, { target: { value: '尚未保存的新灵感' } });
  expect(screen.getByText('已保存的灵感')).toBeVisible();
  await app.failViewRefresh();
  expect(await screen.findByRole('alert')).toHaveTextContent('ideas refresh unavailable');
  expect(screen.getByText('已保存的灵感')).toBeVisible();
  expect(screen.getByLabelText('灵感标题')).toBe(title);
  expect(title).toHaveValue('尚未保存的新灵感');
  app.recoverView();
  await userEvent.click(screen.getByRole('button', { name: '重试资料工作区刷新' }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  expect(screen.getByLabelText('灵感标题')).toBe(title);
  expect(title).toHaveValue('尚未保存的新灵感');
});

it.each(['stale', 'superseded', 'deleted'])('refreshes an open summary after publication changes it to %s', async status => {
  const app = studio({ summary: true });
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /章节总结/ }));
  expect(await screen.findByText('已保存总结正文')).toBeVisible();
  await app.publishSummary(status);
  if (status === 'deleted') {
    expect(await screen.findByText(/MATERIAL_NOT_FOUND/)).toBeVisible();
    expect(screen.queryByText('已保存总结正文')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '编辑素材' })).not.toBeInTheDocument();
  } else expect(await screen.findByText(`历史总结（${status}），不属于当前有效账本。`)).toBeVisible();
  expect(app.requests.filter(url => url.endsWith('/library/summary/S'))).toHaveLength(2);
  await userEvent.click(screen.getByRole('button', { name: '关闭对话框' }));
  await app.publishSummary('valid');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(app.requests.filter(url => url.endsWith('/library/summary/S'))).toHaveLength(2);
});

it('does not overwrite an open editor draft when material detail is invalidated', async () => {
  const app = studio();
  await userEvent.click(await screen.findByRole('button', { name: '参考资料' }));
  await userEvent.click(screen.getByRole('button', { name: /素材A/ }));
  await userEvent.click(await screen.findByRole('button', { name: '编辑素材' }));
  const editor = screen.getByLabelText('设定内容');
  fireEvent.change(editor, { target: { value: '尚未保存的作者草稿' } });
  await app.refreshContent();
  expect(screen.getByLabelText('设定内容')).toBe(editor);
  expect(editor).toHaveValue('尚未保存的作者草稿');
});
