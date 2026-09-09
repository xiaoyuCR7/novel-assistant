import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, projectApi, type AIJob } from '../../lib/api';
import { wikiApi, type WikiDetail, type WikiEntry, type WikiKind, type WikiSource, type WikiSummaryCommand } from '../../lib/wikiApi';
import type { MaterialReference, StoryNode } from '../../lib/types';
import { JobStatus } from '../ai/JobStatus';
import { acknowledge, pendingSubmissions, prepareSubmission, type PendingSubmission } from '../ai/pendingSubmission';
import '../../styles/wiki.css';

const kindNames = { entity: '人物与世界', plot: '情节与伏笔', timeline: '时间线' };
const activeJob = (job?: AIJob | null) => !!job && ['queued', 'running', 'cancel_requested'].includes(job.status);
type Selection = Pick<WikiEntry, 'id' | 'type'>;

export function WikiWorkspace({ projectId, chapters, onOpen, onOpenChapter }: {
  projectId: string;
  chapters: Pick<StoryNode, 'id' | 'title'>[];
  onOpen: (source: MaterialReference) => void;
  onOpenChapter?: (chapterId: string) => void;
}) {
  const api = useMemo(() => wikiApi(projectId), [projectId]);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<WikiKind | 'all'>('all');
  const [chapterId, setChapterId] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [selection, setSelection] = useState<Selection | null>(null);
  useEffect(() => {
    const timer = setTimeout(() => { setQuery(search.trim()); setOffset(0); }, 250);
    return () => clearTimeout(timer);
  }, [search]);
  const index = useQuery({
    queryKey: ['library', projectId, 'wiki', 'index', query, kind, chapterId, offset],
    queryFn: ({ signal }) => api.list(query, kind, offset, chapterId, signal),
  });
  const selected = selection ?? (index.data?.items[0] ? {
    type: index.data.items[0].type, id: index.data.items[0].id,
  } : null);
  return <section className="wiki-workspace" aria-label="小说 Wiki">
    <header className="wiki-heading">
      <div><span className="eyebrow">这部小说的知识索引</span><h1>小说 Wiki</h1></div>
      <p>从人物、设定与情节出发，查看每一条依据。</p>
    </header>
    <div className="wiki-filters">
      <label className="wiki-search">搜索条目<input aria-label="搜索 Wiki 条目" placeholder="名称、别名或内容" value={search}
        onChange={event => { setSearch(event.target.value); setSelection(null); }} /></label>
      <label>条目类型<select aria-label="Wiki 条目类型" value={kind} onChange={event => {
        setKind(event.target.value as WikiKind | 'all'); setOffset(0); setSelection(null);
      }}><option value="all">全部类型</option>{Object.entries(kindNames).map(([value, title]) =>
        <option key={value} value={value}>{title}</option>)}</select></label>
      <label>查看范围<select aria-label="Wiki 章节范围" value={chapterId ?? ''} onChange={event => {
        setChapterId(event.target.value || null); setOffset(0); setSelection(null);
      }}><option value="">全书 · 作者视角</option>{chapters.map(chapter =>
        <option key={chapter.id} value={chapter.id}>截至 {chapter.title}</option>)}</select></label>
    </div>
    <p className="wiki-scope-note">{chapterId
      ? '章节视角：仅纳入截至本章的有日期记录；基础档案和未标注章节的设定仍是作者资料，不代表角色已知。'
      : '全书作者视角：包含全书设定和后续情节，可能涉及剧透。'}</p>
    <div className="wiki-columns">
      <aside className="wiki-index" aria-label="Wiki 条目目录">
        <div className="wiki-index-heading"><h2>条目</h2><span>{index.data ? `${index.data.total} 项` : '—'}</span></div>
        {index.isPending && <p role="status">正在整理条目…</p>}
        {index.error && <p role="alert" className="error-note">条目加载失败：{index.error.message}
          <button onClick={() => void index.refetch()}>重试 Wiki 条目</button></p>}
        {index.data?.items.length === 0 && <p className="wiki-empty">没有符合条件的条目。试试其他名称或范围，或先在素材库添加人物、情节与时间线。</p>}
        {index.data && <nav aria-label="Wiki 条目列表">{index.data.items.map(entry =>
          <button className="wiki-entry" key={`${entry.type}:${entry.id}`} aria-current={selected?.id === entry.id && selected.type === entry.type ? 'page' : undefined}
            onClick={() => setSelection(entry)}>
            <span className="wiki-entry-kind">{kindNames[entry.type]}</span><strong>{entry.title}</strong>
            {entry.aliases.length > 0 && <span className="wiki-entry-aliases">别名 · {entry.aliases.join('、')}</span>}
            <span className="wiki-entry-preview">{entry.preview || '打开查看相关资料'}</span>
          </button>)}</nav>}
        {(offset > 0 || index.data?.has_more) && <div className="wiki-pagination">
          <button disabled={offset === 0 || index.isFetching} onClick={() => { setOffset(value => Math.max(0, value - 30)); setSelection(null); }}>上一页</button>
          <span>第 {Math.floor(offset / 30) + 1} 页</span>
          <button disabled={!index.data?.has_more || index.isFetching} onClick={() => { setOffset(value => value + 30); setSelection(null); }}>下一页</button>
        </div>}
      </aside>
      {selected ? <WikiArticle key={`${projectId}:${selected.type}:${selected.id}:${chapterId ?? ''}`} projectId={projectId}
        selected={selected} chapterId={chapterId} onSelect={setSelection} onOpen={onOpen} onOpenChapter={onOpenChapter} />
        : <div className="wiki-detail wiki-empty"><h2>故事的每一面，都有出处</h2><p>选择一个条目，查看原始资料、关联条目与状态记录。</p></div>}
    </div>
  </section>;
}

function WikiArticle({ projectId, selected, chapterId, onSelect, onOpen, onOpenChapter }: {
  projectId: string; selected: Selection; chapterId: string | null;
  onSelect: (entry: Selection) => void; onOpen: (source: MaterialReference) => void;
  onOpenChapter?: (chapterId: string) => void;
}) {
  const api = useMemo(() => wikiApi(projectId), [projectId]);
  const jobs = useMemo(() => projectApi(projectId), [projectId]);
  const cache = useQueryClient();
  const queryKey = ['library', projectId, 'wiki', 'detail', selected.type, selected.id, chapterId];
  const operation = `wiki:${selected.type}:${selected.id}`;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(() => pendingSubmissions(projectId)
    .find(item => item.operation === operation && item.chapterId === chapterId));
  const mounted = useRef(true);
  const submitting = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const detail = useQuery({
    queryKey,
    queryFn: ({ signal }) => api.detail(selected.type, selected.id, chapterId, signal),
    refetchInterval: query => query.state.error instanceof ApiError && query.state.error.status === 404
      ? false : activeJob(query.state.data?.job) ? 1500 : false,
  });
  const page = detail.error instanceof ApiError && detail.error.status === 404 ? undefined : detail.data;
  async function deliver(item: PendingSubmission, action: () => Promise<AIJob>) {
    try {
      const job = await action();
      if (!job?.id) throw Error('未收到有效回执，请重试同一请求。');
      acknowledge(item);
      cache.setQueryData<WikiDetail>(queryKey, current => current ? { ...current, job } : current);
      await cache.invalidateQueries({ queryKey });
      return job;
    } catch (cause) {
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) acknowledge(item);
      if (cause instanceof ApiError && cause.code === 'WIKI_SOURCE_CHANGED') {
        await cache.invalidateQueries({ queryKey });
        throw Error('原始资料已变化，已刷新条目。请查看最新资料后再次点击 AI摘要。');
      }
      if (cause instanceof ApiError && cause.status === 409
        && ['JOB_CONTROL_CHANGED', 'WIKI_JOB_ACTIVE', 'JOB_NOT_RESUMABLE'].includes(cause.code ?? '')) {
        await cache.invalidateQueries({ queryKey });
        throw Error('摘要任务状态已变化，请查看当前任务状态后再操作。');
      }
      throw cause;
    } finally {
      if (mounted.current) setPending(pendingSubmissions(projectId).find(item => item.operation === operation && item.chapterId === chapterId));
    }
  }
  async function summarize() {
    if (submitting.current) return;
    submitting.current = true; setBusy(true); setError('');
    try {
      let item = pendingSubmissions(projectId).find(candidate => candidate.operation === operation && candidate.chapterId === chapterId);
      if (!item) {
        const latest = await detail.refetch({ throwOnError: true });
        if (!mounted.current) return;
        if (!latest.data?.fingerprint) throw Error('无法确认来源，请重新加载条目。');
        const prior = latest.data.job;
        if (prior?.status === 'recovery_required') throw Error('摘要任务已暂停，请先恢复或取消原任务。');
        const unknown = !!prior?.replacement_requires_confirmation
          || (!!prior && ['cancelled', 'failed'].includes(prior.status) && prior.recovery_reason === 'result_unknown');
        if (unknown && !window.confirm('上次请求结果未知，新建摘要可能再次调用模型并产生重复费用。仍要生成吗？')) return;
        item = prepareSubmission(projectId, chapterId, {
          chapter_id: chapterId, fingerprint: latest.data.fingerprint, ...(unknown ? { confirm_unknown: true } : {}),
        }, operation);
      }
      setPending(item);
      await deliver(item, () => api.summarize(selected.type, selected.id, item.command as unknown as WikiSummaryCommand, item.idempotencyKey));
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      submitting.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  async function resume(confirmUnknown: boolean) {
    if (!page?.job) return;
    setError('');
    const task = page.job;
    try {
      const item = prepareSubmission(projectId, chapterId, {
        expected_control_revision: task.control_revision, confirm_unknown: confirmUnknown,
      }, `wiki-resume:${task.id}`);
      await deliver(item, () => jobs.resumeJob(task.id, item.command, item.idempotencyKey));
    } catch (cause) {
      // Keep the notice when a refreshed active job replaces the JobStatus component.
      if (mounted.current) setError(cause instanceof Error ? cause.message : String(cause));
    }
  }
  function openSource(source: WikiSource) {
    onOpen({ id: source.id, type: source.type });
  }
  const sources = new Map(page?.sources.map(source => [`${source.type}:${source.id}`, source]));
  return <article className="wiki-detail" aria-label="Wiki 条目详情">
    {detail.isPending && <p role="status">正在加载条目资料…</p>}
    {detail.error && <p role="alert" className="error-note">条目详情加载失败：{detail.error.message}
      <button onClick={() => void detail.refetch()}>重试 Wiki 详情</button></p>}
    {page && <>
      <header className="wiki-article-heading"><span className="wiki-entry-kind">{kindNames[page.type]}</span><h2>{page.title}</h2>
        {page.aliases.length > 0 && <p className="wiki-aliases">别名<span>{page.aliases.join('、')}</span></p>}
      </header>
      {page.truncated && <p role="status" className="wiki-caution">关联资料较多，本页仅展示部分来源或内容。打开原始资料查看完整内容；AI摘要仅参考本页来源。</p>}
      <section className="wiki-summary" aria-label="AI 摘要">
        <div className="wiki-section-heading"><h3>AI 摘要</h3><button className="primary-action" disabled={busy || activeJob(page.job) || page.job?.status === 'recovery_required' || (!pending && !!page.summary && !page.summary.stale)}
          onClick={() => void summarize()}>{busy ? '提交中…' : 'AI摘要'}</button></div>
        <p className="subtle">按需生成并缓存，仅供辅助阅读。请以原始资料为准。</p>
        {!page.summary && !page.job && <p>还没有摘要。点击 AI摘要后才会调用当前模型。</p>}
        {page.summary && <>
          <p className={page.summary.stale || detail.error ? 'wiki-caution' : 'wiki-cache-note'} role="status">{detail.error
            ? '来源刷新失败 · 当前仅显示旧摘要，暂时无法核对来源是否变化。'
            : page.summary.stale
            ? '来源已变化 · 旧摘要已过期，请勿作为当前设定依据。可重新生成。' : '已缓存 · 来源未变化，无需重复生成。'}</p>
          <ul className="wiki-claims">{page.summary.claims.map((claim, index) => <li key={index}>
            <p>{claim.text}</p><div className="wiki-citations">{claim.source_ids.map(sourceId => {
              const source = sources.get(sourceId);
              return source ? <button key={sourceId} onClick={() => openSource(source)} aria-label={`查看依据：${source.title}`}>{source.title}</button>
                : <span key={sourceId} className="subtle">来源已不可用</span>;
            })}</div>
          </li>)}</ul>
        </>}
        {pending && <p role="status" className="subtle">上次提交尚未收到回执。再次点击 AI摘要会重试同一请求。</p>}
        {error && <p role="alert" className="error-note">{error}</p>}
        {page.job && <JobStatus key={page.job.id} job={page.job} onCancel={async () => {
          const job = await jobs.cancelJob(page.job!.id);
          cache.setQueryData<WikiDetail>(queryKey, current => current ? { ...current, job } : current);
          await cache.invalidateQueries({ queryKey });
        }} onResume={resume} sourceUnavailable={!!detail.error} />}
      </section>
      {page.links.length > 0 && <section className="wiki-links"><h3>关联条目</h3><div>{page.links.map(link =>
        <button key={`${link.type}:${link.id}`} onClick={() => onSelect(link)}>{link.title}<span>{kindNames[link.type]}</span></button>)}</div></section>}
      {(page.state_conflicts?.length ?? 0) > 0 && <p role="status" className="wiki-caution">存在 {page.state_conflicts!.length} 项状态冲突，请对照记录并在连续性问题中处理。</p>}
      {page.state_history.length > 0 && <section className="wiki-history"><h3>状态记录</h3><ol>{page.state_history.map(state =>
        <li key={state.id}><div className="wiki-section-heading">{onOpenChapter
          ? <button onClick={() => onOpenChapter(state.chapter_id)}>{state.chapter_title}</button>
          : <strong>{state.chapter_title}</strong>}<small>修订 {state.revision}</small></div>
          <pre>{JSON.stringify(state.data, null, 2)}</pre></li>)}</ol></section>}
      <section className="wiki-sources"><div className="wiki-section-heading"><h3>原始资料</h3><span>{page.sources.length} 条来源</span></div>
        {page.sources.length === 0 && <p className="subtle">当前范围没有可展示的来源。</p>}
        {page.sources.map(source => <section className="wiki-source" key={`${source.type}:${source.id}`}>
          <div className="wiki-section-heading"><h4><button onClick={() => openSource(source)}>{source.title}</button></h4><small>修订 {source.revision}</small></div>
          {source.reason && <p className="wiki-source-reason">{source.reason}</p>}<p className="wiki-source-content">{source.content || '暂无文字内容'}</p>
          <button className="wiki-source-open" onClick={() => openSource(source)} aria-label={`打开原始资料：${source.title}`}>打开原始资料 ↗</button>
        </section>)}
      </section>
    </>}
  </article>;
}
