import { useEffect, useMemo, useRef, useState } from 'react';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError } from '../../lib/api';
import { DiagnosticError } from '../../components/DiagnosticError';
import { conversationApi, type ConversationCreate, type ConversationThread, type ConversationUpdate } from '../../lib/conversationApi';
import '../../styles/conversations.css';

export interface ConversationsPanelProps {
  projectId: string;
  chapterId?: string | null;
  selectedId?: string | null;
  branchFromJobId?: string;
  onSelect: (id: string) => void | Promise<void>;
  onLifecycleChange?: (thread: ConversationThread) => void | Promise<void>;
  beforeChange?: () => boolean | Promise<boolean>;
}
type Operation = { kind: 'create'; body: ConversationCreate }
  | { kind: 'branch'; id: string; body: ConversationCreate & { from_job_id: string } }
  | { kind: 'update'; id: string; body: ConversationUpdate };
type Editor = 'create' | 'branch' | 'rename' | 'delete' | null;
const errors: Record<string, string> = {
  CONVERSATION_CHANGED: '会话已在其他窗口更新，请刷新后再操作。',
  CONVERSATION_NOT_FOUND: '会话已不存在，请刷新会话列表。',
  CONVERSATION_DELETED: '会话已在回收站，请先恢复。',
  CONVERSATION_ARCHIVED: '会话已归档，请先恢复。',
  BRANCH_POINT_NOT_FOUND: '选中的消息不属于此会话，或还未完成，请重新选择。',
  IDEMPOTENCY_CONFLICT: '此操作的回执与请求不一致，请刷新核对会话。',
};
function message(error: unknown) {
  return error instanceof ApiError && error.code && errors[error.code]
    ? errors[error.code] : error instanceof Error ? error.message : '操作失败，请重试。';
}
function readOperation(key: string): Operation | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? 'null') as Operation | null;
    if (!value || typeof value.body !== 'object' || !value.body) return null;
    if (value.kind === 'create' && typeof value.body.id === 'string' && typeof value.body.title === 'string') return value;
    if (value.kind === 'branch' && typeof value.id === 'string' && typeof value.body.id === 'string'
      && typeof value.body.from_job_id === 'string' && typeof value.body.title === 'string') return value;
    if (value.kind === 'update' && typeof value.id === 'string' && Number.isInteger(value.body.expected_revision)) return value;
  } catch { /* A damaged local navigation record must not dispatch anything. */ }
  return null;
}

export function ConversationsPanel(props: ConversationsPanelProps) {
  return <ConversationScope key={`${props.projectId}:${props.chapterId ?? 'global'}`} {...props} />;
}

function ConversationScope({ projectId, chapterId, selectedId, branchFromJobId, onSelect, onLifecycleChange, beforeChange }: ConversationsPanelProps) {
  const api = useMemo(() => conversationApi(projectId), [projectId]);
  const cache = useQueryClient();
  const scopeKey = ['conversations', projectId, chapterId ?? null] as const;
  const operationKey = `novel:conversation-operation:${encodeURIComponent(projectId)}:${encodeURIComponent(chapterId ?? 'global')}`;
  const [status, setStatus] = useState<ConversationThread['status']>('active');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [editor, setEditor] = useState<Editor>(null);
  const [editTarget, setEditTarget] = useState<ConversationThread | null>(null);
  const [branchPoint, setBranchPoint] = useState<string>();
  const [name, setName] = useState('新会话');
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [pending, setPending] = useState<Operation | null>(() => readOperation(operationKey));
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const alive = useRef(true);
  const selection = useRef(selectedId);
  selection.current = selectedId;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { const timer = setTimeout(() => setQuery(search.trim()), 250); return () => clearTimeout(timer); }, [search]);
  const history = useInfiniteQuery({ queryKey: [...scopeKey, status, query],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => api.list({ chapterId, status, q: query, before: pageParam }, signal),
    getNextPageParam: page => page.next_cursor ?? undefined, retry: false });
  const rows = Array.from(new Map((history.data?.pages.flatMap(page => page.items) ?? []).map(row => [row.id, row])).values());
  const defaultId = history.data?.pages[0]?.default_conversation_id;
  const currentId = selectedId || defaultId;
  const detail = useQuery({ queryKey: ['conversation', projectId, currentId], enabled: !!currentId,
    queryFn: ({ signal }) => api.detail(currentId!, signal), retry: false });
  const current = detail.data;
  useEffect(() => { setEditor(null); }, [currentId]);
  const canManage = !!current && !detail.isError && !detail.isPending && !busy && !pending;
  const canBranch = canManage && current?.status === 'active' && !!branchFromJobId;
  function persist(operation: Operation | null) {
    setPending(operation);
    try { if (operation) localStorage.setItem(operationKey, JSON.stringify(operation)); else localStorage.removeItem(operationKey); }
    catch { /* The in-memory receipt identity still protects this mounted operation. */ }
  }
  async function refresh() {
    await Promise.all([cache.invalidateQueries({ queryKey: scopeKey }),
      cache.invalidateQueries({ queryKey: ['conversation', projectId] })]);
  }
  async function choose(id: string) {
    if (locked.current || pending) return;
    locked.current = true; setBusy(true); setError(null);
    try {
      if (beforeChange && !await beforeChange()) return;
      if (alive.current) await onSelect(id);
    } catch (caught) { if (alive.current) setError(caught); }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  async function execute(operation: Operation) {
    if (locked.current) return;
    locked.current = true; setBusy(true); setError(null); setNotice('');
    const originalSelection = selection.current;
    try {
      if (beforeChange && !await beforeChange()) return;
      if (!alive.current) return;
      persist(operation);
      const result = operation.kind === 'create' ? await api.create(operation.body)
        : operation.kind === 'branch' ? await api.branch(operation.id, operation.body)
        : await api.update(operation.id, operation.body);
      // A late GET must not overwrite the committed mutation response.
      await cache.cancelQueries({ queryKey: ['conversation', projectId, result.id], exact: true });
      cache.setQueryData(['conversation', projectId, result.id], result);
      if (!alive.current) return;
      persist(null); setEditor(null); setNotice(result.status === 'deleted' ? '会话已移到回收站，可随时恢复。'
        : result.status === 'archived' ? '会话已归档。' : '会话已保存。');
      await onLifecycleChange?.(result);
      if (!alive.current) return;
      await cache.invalidateQueries({ queryKey: scopeKey });
      if (!alive.current || selection.current !== originalSelection) return;
      if (result.status === 'active') {
        setStatus('active'); setSearch(''); setQuery('');
        await onSelect(result.id);
      } else if (currentId === result.id) {
        const fallback = await api.list({ chapterId, status: 'active', limit: 1 });
        if (alive.current && selection.current === originalSelection && fallback.items[0]) await onSelect(fallback.items[0].id);
      }
    } catch (caught) {
      if (!alive.current) return;
      // A 4xx is a definite refusal. A broken connection or 5xx may follow a commit.
      if (caught instanceof ApiError && caught.status >= 400 && caught.status < 500) persist(null);
      setError(caught);
      void refresh();
    } finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  function open(kind: Editor) {
    setEditor(kind); setEditTarget(current ?? null); setBranchPoint(branchFromJobId); setError(null); setNotice('');
    setName(kind === 'create' ? '新会话' : kind === 'branch' ? `${current?.title ?? '当前会话'} · 分支` : current?.title ?? '');
  }
  function submit() {
    const title = name.trim(); if (!title || busy || pending) return;
    if (editor === 'create') void execute({ kind: 'create', body: { id: crypto.randomUUID(), chapter_id: chapterId ?? null, title } });
    if (editor === 'branch' && editTarget && branchPoint) void execute({ kind: 'branch', id: editTarget.id,
      body: { id: crypto.randomUUID(), title, from_job_id: branchPoint } });
    if (editor === 'rename' && editTarget) void execute({ kind: 'update', id: editTarget.id,
      body: { expected_revision: editTarget.revision, title } });
  }
  function lifecycle(row: ConversationThread, next: ConversationThread['status']) {
    void execute({ kind: 'update', id: row.id, body: { expected_revision: row.revision, status: next } });
  }
  return <section className="conversations-panel" aria-label="会话管理" aria-busy={busy}>
    <div className="conversations-heading"><div><h2>会话</h2><p>{chapterId ? '本章的独立话题与创作方案' : '全书的独立话题与创作方案'}</p></div>
      <button type="button" className="primary" disabled={busy || !!pending} onClick={() => open('create')}>新建会话</button></div>
    <label className="conversations-search"><span>搜索会话</span><input type="search" aria-label="搜索会话" maxLength={200}
      placeholder="搜索名称或完整消息" value={search} onChange={event => setSearch(event.target.value)} /></label>
    <div className="conversations-tabs" role="group" aria-label="会话分类">
      {(['active', 'archived', 'deleted'] as const).map(value => <button key={value} type="button"
        aria-pressed={status === value} onClick={() => setStatus(value)}>{({ active: '进行中', archived: '已归档', deleted: '回收站' })[value]}</button>)}
    </div>
    {history.isPending && <p role="status">正在读取会话…</p>}
    {history.isError && <div><DiagnosticError error={history.error} message={message(history.error)} /><button type="button" onClick={() => void history.refetch()}>重试读取会话</button></div>}
    {!history.isPending && !history.isError && rows.length === 0 && <p className="conversations-empty">{query ? '没有找到匹配的会话。' : status === 'deleted' ? '回收站为空。' : status === 'archived' ? '还没有归档会话。' : '还没有会话，可以新建一个话题。'}</p>}
    <ul className="conversations-list">{rows.map(row => <li key={row.id}>
      <button type="button" aria-current={currentId === row.id ? 'true' : undefined} disabled={busy || !!pending || row.status !== 'active'}
        onClick={() => void choose(row.id)}><strong>{row.title}</strong><span>{row.job_count} 条消息{row.parent_conversation_id ? ' · 分支' : ''}</span></button>
      {row.status !== 'active' && <button type="button" aria-label={`恢复 ${row.title}`} disabled={busy || !!pending} onClick={() => lifecycle(row, 'active')}>恢复</button>}
    </li>)}</ul>
    {history.hasNextPage && <button type="button" disabled={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage ? '正在加载…' : '加载更多会话'}</button>}
    {detail.isError && <div><DiagnosticError error={detail.error} message={message(detail.error)} /><button type="button" onClick={() => void detail.refetch()}>刷新当前会话</button></div>}
    {current && <div className="conversations-actions" aria-label="当前会话操作">
      <p>当前：{current.title}{current.parent_conversation_id ? '（从选定消息分支）' : ''}</p>
      <div><button type="button" disabled={!canManage} onClick={() => open('rename')}>重命名</button>
        <button type="button" disabled={!canBranch} onClick={() => open('branch')}>从选中消息分支</button>
        {current.status === 'active' && <button type="button" disabled={!canManage} onClick={() => lifecycle(current, 'archived')}>归档会话</button>}
        {current.status !== 'deleted' && <button type="button" disabled={!canManage} onClick={() => open('delete')}>删除会话</button>}
        {current.status !== 'active' && <button type="button" disabled={!canManage} onClick={() => lifecycle(current, 'active')}>恢复当前会话</button>}</div>
      {!branchFromJobId && current.status === 'active' && <p>选中一条已完成消息，即可从该处尝试另一种方案。</p>}
    </div>}
    {editor && editor !== 'delete' && <form className="conversations-editor" onSubmit={event => { event.preventDefault(); submit(); }}>
      <label>会话名称<input autoFocus maxLength={240} value={name} onChange={event => setName(event.target.value)} disabled={busy || !!pending} /></label>
      {editor === 'branch' && <p>继承选中消息及之前的对话，此后的消息各自独立。</p>}
      <div><button type="submit" className="primary" disabled={busy || !!pending || !name.trim()}>{busy ? '正在保存…' : editor === 'create' ? '创建会话' : editor === 'branch' ? '创建分支' : '保存名称'}</button>
        <button type="button" disabled={busy} onClick={() => setEditor(null)}>取消</button></div>
    </form>}
    {editor === 'delete' && editTarget && <div className="conversations-editor"><p>将“{editTarget.title}”移到回收站？会话可以恢复，章节正文、采纳版本与任务证据都会保留。</p>
      <div><button type="button" disabled={busy || !!pending} onClick={() => lifecycle(editTarget, 'deleted')}>移到回收站</button><button type="button" disabled={busy} onClick={() => setEditor(null)}>取消</button></div></div>}
    {!!error && <DiagnosticError error={error} message={message(error)} />}
    {pending && <div className="conversations-receipt"><p>上次操作尚未取得确定回执。核对时会使用原请求，不会重复新建。</p><button type="button" disabled={busy} onClick={() => void execute(pending)}>{busy ? '正在核对…' : '核对并重试原操作'}</button></div>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
