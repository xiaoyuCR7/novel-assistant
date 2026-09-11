import { useEffect, useMemo, useRef, useState } from 'react';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { api as appApi, ApiError, projectApi, type JobSummary } from '../../lib/api';
import { qualityApi, type QualityChapter, type QualityCommand, type QualityDimension, type QualityMode, type QualityRun } from '../../lib/qualityApi';
import type { StoryNode } from '../../lib/types';
import { readWorkspaceState, saveWorkspaceState } from '../../lib/workspaceState';
import { acknowledge, pendingSubmissions, prepareSubmission, type PendingSubmission } from '../ai/pendingSubmission';
import { JobStatus } from '../ai/JobStatus';
import { MAX_INPUT_BUDGET, modelInputBudget, readInputBudget, resolveInputBudget } from '../ai/inputBudget';
import '../../styles/quality.css';
import { QualityDiffPanel } from './QualityDiffPanel';
import { DiagnosticError } from '../../components/DiagnosticError';

const active = (job?: JobSummary | null) => !!job && ['queued', 'running', 'cancel_requested'].includes(job.status);
const unfinished = (job?: JobSummary | null) => active(job) || job?.status === 'recovery_required';
const dimensions: Record<QualityDimension, string> = { readability: '可读性', engagement: '吸引力', pacing: '叙事节奏', clarity: '表达清晰', consistency: '情节一致' };
const statuses: Record<string, string> = { queued: '等待开始', running: '正在协作', cancel_requested: '正在取消',
  cancelled: '已取消', failed: '执行失败', recovery_required: '等待处理', succeeded: '已完成' };
const isQualityPending = (item: PendingSubmission) => item.operation === 'quality' || item.operation.startsWith('quality-resume:');

export interface QualityWorkspaceProps {
  projectId: string;
  chapters: Pick<StoryNode, 'id' | 'title'>[];
  initialChapterId?: string;
  initialRunId?: string;
  startNew?: boolean;
  sourceUnavailable?: boolean;
  beforeStart?: () => boolean | Promise<boolean>;
  beforeAccept?: (chapterId: string) => boolean | Promise<boolean>;
  onAccepted?: (chapterId: string) => void | Promise<void>;
  onOpenChapter?: (chapterId: string) => void;
}

export function QualityWorkspace(props: QualityWorkspaceProps) {
  // Remount project-owned inputs and request identities even when the host reuses this view.
  return <QualityProject key={props.projectId} {...props} />;
}

function QualityProject({ projectId, chapters, initialChapterId, initialRunId, startNew = false, sourceUnavailable = false, beforeStart, beforeAccept, onAccepted, onOpenChapter }: QualityWorkspaceProps) {
  const api = useMemo(() => qualityApi(projectId), [projectId]);
  const jobs = useMemo(() => projectApi(projectId), [projectId]);
  const cache = useQueryClient();
  const [mode, setMode] = useState<QualityMode>('polish');
  const [selectedChapters, setSelectedChapters] = useState<string[]>(() => initialChapterId && chapters.some(chapter => chapter.id === initialChapterId)
    ? [initialChapterId] : chapters[0] ? [chapters[0].id] : []);
  const [instructions, setInstructions] = useState('');
  const [target, setTarget] = useState('75');
  const [budgetOverride, setBudgetOverride] = useState<string | undefined>(() => readInputBudget(projectId));
  const model = useQuery({ queryKey: ['model-settings'], queryFn: appApi.modelSettings });
  let automaticBudget = '', budgetError = '';
  try { automaticBudget = String(modelInputBudget(model.data)); }
  catch (cause) { budgetError = cause instanceof Error ? cause.message : String(cause); }
  const budget = budgetOverride ?? automaticBudget;
  const [selection, setSelection] = useState<string | null>(() => startNew ? '' : initialRunId ?? readWorkspaceState(projectId).qualityRunId ?? null);
  const [missingRuns, setMissingRuns] = useState<Set<string>>(() => new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(() => pendingSubmissions(projectId).filter(isQualityPending));
  const lock = useRef(false), mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const listKey = ['quality', projectId, 'runs'];
  const list = useInfiniteQuery({ queryKey: listKey, initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => api.list(pageParam, false, signal),
    getNextPageParam: page => page.next_cursor ?? undefined,
    refetchInterval: query => query.state.data?.pages.some(page => page.items.some(active)) ? 2000 : false,
  });
  const activeRuns = useQuery({ queryKey: ['quality', projectId, 'active'], queryFn: ({ signal }) => api.list(undefined, true, signal),
    refetchInterval: query => query.state.data?.items.some(active) ? 2000 : false });
  const recentRuns = list.data?.pages.flatMap(page => page.items) ?? [];
  const activeIds = new Set(activeRuns.data?.items.map(item => item.id));
  const allRuns = [...(activeRuns.data?.items ?? []), ...recentRuns.filter(item => !activeIds.has(item.id))].filter(item => !missingRuns.has(item.id));
  const defaultId = activeRuns.isPending ? undefined : allRuns[0]?.id;
  const selectedId = selection ?? defaultId;
  useEffect(() => {
    if (selection === null && defaultId) setSelection(defaultId);
  }, [selection, defaultId]);
  const detailKey = ['quality', projectId, 'detail', selectedId];
  const detail = useQuery({ queryKey: detailKey, enabled: !!selectedId,
    queryFn: ({ signal }) => api.detail(selectedId!, signal),
    refetchInterval: query => query.state.error instanceof ApiError && query.state.error.status === 404 ? false : active(query.state.data) ? 1500 : false });
  const run = detail.error instanceof ApiError && detail.error.status === 404 ? undefined : detail.data;
  const missingRun = detail.error instanceof ApiError && detail.error.status === 404;
  useEffect(() => {
    if (!selectedId) return;
    if (missingRun) {
      setMissingRuns(current => new Set([...current, selectedId]));
      setSelection(current => current === selectedId ? null : current);
      saveWorkspaceState(projectId, { qualityRunId: undefined });
    } else if (run) saveWorkspaceState(projectId, { qualityRunId: selectedId });
  }, [selectedId, missingRun, run, projectId]);
  const visibleRuns = run && !allRuns.some(item => item.id === run.id) ? [...allRuns, run] : allRuns;
  const hasActive = activeRuns.data?.items.some(unfinished) || allRuns.some(unfinished) || unfinished(run);
  const needsReview = run?.status === 'recovery_required'
    ? run.result.chapters?.find(chapter => chapter.status === 'needs_review' && chapter.chapter_id === run.result.active_chapter_id && !run.effects?.quality_approvals?.[chapter.chapter_id]) : undefined;
  const selected = chapters.filter(chapter => selectedChapters.includes(chapter.id));

  async function refresh() {
    await cache.invalidateQueries({ queryKey: ['quality', projectId] });
  }
  function rememberRun(value: QualityRun) {
    if (!value?.id) throw Error('未收到有效回执，请用原请求确认提交。');
    cache.setQueryData(['quality', projectId, 'detail', value.id], value);
    if (mounted.current) setSelection(value.id);
  }
  async function readBack(id: string) {
    const queryKey = ['quality', projectId, 'detail', id];
    await cache.cancelQueries({ queryKey, exact: true });
    const value = await cache.fetchQuery({ queryKey, queryFn: ({ signal }) => api.detail(id, signal), staleTime: 0 });
    rememberRun(value);
    return value;
  }
  async function act(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    try { await action(); }
    catch (cause) {
      if (cause instanceof ApiError && [404, 409].includes(cause.status)) await refresh();
      if (mounted.current) setError(cause);
    } finally {
      lock.current = false;
      if (mounted.current) { setBusy(false); setPending(pendingSubmissions(projectId).filter(isQualityPending)); }
    }
  }
  async function deliver(item: PendingSubmission) {
    try {
      const value = item.operation === 'quality'
        ? await api.start(item.command as unknown as QualityCommand, item.idempotencyKey)
        : await jobs.resumeJob(item.operation.slice('quality-resume:'.length), item.command, item.idempotencyKey) as unknown as QualityRun;
      rememberRun(value); acknowledge(item); await refresh();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) acknowledge(item);
      throw cause;
    }
  }
  async function start() {
    if (sourceUnavailable || hasActive || pending.length || !selected.length) return;
    await act(async () => {
      if (beforeStart && !(await beforeStart())) return;
      if (model.error) throw model.error;
      const qualityTarget = Number(target), tokenBudget = resolveInputBudget(model.data, budgetOverride);
      if (!Number.isInteger(qualityTarget) || qualityTarget < 50 || qualityTarget > 95) throw Error('质量目标请填写 50 到 95 的整数。');
      if (selected.length > 5 || (mode === 'polish' && selected.length !== 1)) throw Error('单章优化请选择一章，交替协作最多选择五章。');
      const documents = await Promise.all(selected.map(chapter => jobs.chapter(chapter.id)));
      if (!mounted.current) return;
      if (documents.some(document => !Number.isInteger(document.revision))) throw Error('无法确认正文版本，请刷新章节后重试。');
      if (mode === 'polish' && !documents[0].content.trim()) throw Error('请先保存需要优化的正文。');
      const command: QualityCommand = { mode, chapters: selected.map((chapter, index) => ({ chapter_id: chapter.id, expected_revision: documents[index].revision! })),
        instructions: instructions.trim(), token_budget: tokenBudget, quality_target: qualityTarget };
      await deliver(prepareSubmission(projectId, null, { ...command }, 'quality'));
    });
  }
  async function resume(confirmUnknown: boolean, value = run) {
    if (!value || sourceUnavailable) return;
    await deliver(prepareSubmission(projectId, null, { expected_control_revision: value.control_revision, confirm_unknown: confirmUnknown }, `quality-resume:${value.id}`));
  }
  async function cancel() {
    if (!run) return;
    await act(async () => {
      try { rememberRun(await jobs.cancelJob(run.id) as unknown as QualityRun); }
      catch (cause) {
        // The server can finish cancellation even if its response is lost.
        const latest = await readBack(run.id).catch(() => undefined);
        if (!latest || !['cancel_requested', 'cancelled'].includes(latest.status)) throw cause;
      } finally { await refresh(); }
    });
  }
  async function accept(chapter: QualityChapter, selectedHunkIds?: string[]) {
    if (!run || sourceUnavailable || !['succeeded', 'failed', 'cancelled'].includes(run.status)) return;
    await act(async () => {
      if (beforeAccept && !(await beforeAccept(chapter.chapter_id))) return;
      const matchesAcceptance = (value?: QualityRun) => !!value?.effects?.accepted_chapters?.[chapter.chapter_id]
        && JSON.stringify(value.effects.accepted_selections?.[chapter.chapter_id]?.slice().sort() ?? null)
          === JSON.stringify(selectedHunkIds?.slice().sort() ?? null);
      let acceptedVersionId: string | undefined;
      try { acceptedVersionId = (await api.accept(run.id, chapter.chapter_id, false, selectedHunkIds)).id; }
      catch (cause) {
        if (cause instanceof ApiError && ['QUALITY_CONFIRMATION_REQUIRED', 'SEVERE_CONFIRMATION_REQUIRED'].includes(cause.code ?? '')) {
          if (!window.confirm(cause.message)) return;
          if (beforeAccept && !(await beforeAccept(chapter.chapter_id))) return;
          try { acceptedVersionId = (await api.accept(run.id, chapter.chapter_id, true, selectedHunkIds)).id; }
          catch (confirmedCause) {
            const latest = await readBack(run.id).catch(() => undefined);
            if (!matchesAcceptance(latest)) throw confirmedCause;
          }
        } else {
          const latest = await readBack(run.id).catch(() => undefined);
          if (!matchesAcceptance(latest)) throw cause;
        }
      }
      if (acceptedVersionId) {
        const queryKey = ['quality', projectId, 'detail', run.id];
        await cache.cancelQueries({ queryKey, exact: true });
        cache.setQueryData<QualityRun>(queryKey, current => {
          const value = current ?? run;
          return { ...value, effects: { ...value.effects, accepted_chapters: { ...value.effects?.accepted_chapters, [chapter.chapter_id]: acceptedVersionId! },
            ...(selectedHunkIds ? { accepted_selections: { ...value.effects?.accepted_selections, [chapter.chapter_id]: selectedHunkIds.slice().sort() } } : {}) } };
        });
      }
      try { await onAccepted?.(chapter.chapter_id); }
      finally { await refresh(); }
    });
  }
  const cannotStart = busy || sourceUnavailable || !!hasActive || !!pending.length || !selected.length || list.isPending || activeRuns.isPending || !!list.error || !!activeRuns.error
    || model.isPending || !!model.error || !!budgetError;
  return <section className="quality-workspace" aria-label="文章质量优化">
    <header className="quality-heading"><div><span className="eyebrow">写作与精修</span><h1>让故事更值得读下去</h1></div>
      <p>写作交稿后暂停，优化完成再继续下一章。</p></header>
    <form className="quality-setup" noValidate onSubmit={event => { event.preventDefault(); void start(); }}>
      <div className="quality-modes" aria-label="质量优化模式">
        {([['polish', '优化已写正文'], ['collaborate', '交替写作与优化']] as const).map(([value, label]) =>
          <button type="button" key={value} aria-pressed={mode === value} disabled={busy} onClick={() => { setMode(value); if (value === 'polish') setSelectedChapters(current => current.slice(0, 1)); }}>{label}</button>)}
      </div>
      {mode === 'polish' ? <label className="quality-field">优化章节<select aria-label="优化章节" value={selected[0]?.id ?? ''} disabled={busy || !chapters.length}
        onChange={event => setSelectedChapters([event.target.value])}><option value="" disabled>选择章节</option>{chapters.map(chapter => <option key={chapter.id} value={chapter.id}>{chapter.title}</option>)}</select></label>
        : <fieldset className="quality-chapter-picker" disabled={busy}><legend>按故事顺序依次完成 · 最多 5 章</legend>
          {chapters.map(chapter => <label key={chapter.id}><input type="checkbox" checked={selectedChapters.includes(chapter.id)}
            disabled={!selectedChapters.includes(chapter.id) && selected.length >= 5} onChange={event => setSelectedChapters(current => event.target.checked ? [...current, chapter.id] : current.filter(id => id !== chapter.id))} />
            <span>{chapter.title}</span></label>)}</fieldset>}
      <label className="quality-field">优化要求<textarea aria-label="优化要求" value={instructions} disabled={busy} maxLength={8000} rows={3}
        placeholder="例如：减少重复解释，让对话更自然，保留人物语气和情节走向。" onChange={event => setInstructions(event.target.value)} /></label>
      <details className="quality-settings"><summary>质量目标与输入预算</summary><div>
        <label className="quality-field">质量目标<input aria-label="质量目标" type="number" min={50} max={95} step={1} value={target} disabled={busy} onChange={event => setTarget(event.target.value)} /></label>
        <label className="quality-field">输入预算 Token<input aria-label="质量优化输入预算" type="number" min={256} max={MAX_INPUT_BUDGET} step={1} value={budget} disabled={busy} onChange={event => setBudgetOverride(event.target.value)} /></label>
        <p className="subtle">{budgetOverride === undefined ? '跟随模型容量，扣除输出预留。' : '当前协作使用自定义省费上限。'}
          <button type="button" disabled={busy} aria-pressed={budgetOverride === undefined} onClick={() => setBudgetOverride(undefined)}>跟随模型</button></p>
      </div></details>
      <footer className="quality-start"><p>{mode === 'polish' ? '优化已保存的正文，完成后由你采纳。' : '已写章节直接优化，空白章节先写再优化；每章交接后再推进。'}<br />评分是辅助判断，最终以你的阅读感受为准。</p>
        <button className="primary-action" disabled={cannotStart}>{busy ? '正在处理…' : mode === 'polish' ? '检测并优化正文' : '开始交替协作'}</button></footer>
      {!chapters.length && <p role="status" className="subtle">先创建章节，再开始质量优化。</p>}
      {sourceUnavailable && <p role="status" className="subtle">正文来源暂不可用，请刷新后重试。</p>}
      {hasActive && <p role="status" className="subtle">已有协作尚未结束，请先在下方继续或取消原任务。</p>}
      {!model.error && !model.isPending && budgetError && <p role="status" className="subtle">{budgetError}</p>}
      {model.error && <div><DiagnosticError error={model.error} message="模型配置读取失败，请重试或打开设置检查。" />
        <button type="button" onClick={() => { void model.refetch(); }}>重试模型配置</button></div>}
    </form>
    {pending.map(item => <div className="quality-notice" role="status" key={item.idempotencyKey}>有一项质量优化请求尚未取得回执。
      <button type="button" disabled={busy || sourceUnavailable} onClick={() => void act(() => deliver(item))}>用原请求确认提交</button>
      {!item.persisted && <p>浏览器无法保存回执身份，请保留此页面以便重试。</p>}</div>)}
    {!!error && <DiagnosticError error={error} />}
    {(list.error || activeRuns.error) && <div><DiagnosticError error={list.error ?? activeRuns.error} message={`协作记录读取失败：${list.error?.message || activeRuns.error?.message}`} />
      <button type="button" onClick={() => void refresh()}>重试协作记录</button></div>}
    <div className="quality-history-heading"><h2>协作记录</h2><button type="button" disabled={list.isFetching || detail.isFetching} onClick={() => void refresh()}>刷新记录</button></div>
    {list.isPending && <p role="status">正在读取协作记录…</p>}
    {!list.isPending && !list.error && !allRuns.length && !run && <p className="quality-empty">还没有质量优化记录</p>}
    {!!visibleRuns.length && <nav className="quality-run-list" aria-label="质量优化记录">{visibleRuns.map((item, index) => <button type="button" key={item.id} aria-current={selectedId === item.id ? 'page' : undefined}
      disabled={busy} onClick={() => setSelection(item.id)}><strong>{statuses[item.status] ?? '状态待确认'}</strong><span>{item.created_at ? new Date(item.created_at).toLocaleString('zh-CN') : `协作 ${visibleRuns.length - index}`}</span></button>)}</nav>}
    {list.hasNextPage && <button type="button" disabled={list.isFetchingNextPage} onClick={() => { void list.fetchNextPage().catch(cause => setError(cause)); }}>加载更早协作</button>}
    {selectedId && detail.isPending && <p role="status">正在读取协作详情…</p>}
    {detail.error && <div><DiagnosticError error={detail.error} message={`协作详情读取失败：${detail.error.message}`} /><button type="button" onClick={() => void detail.refetch()}>重试协作详情</button></div>}
    {run && <div className="quality-run" aria-label="协作详情">
      <div className="quality-run-status"><div><h2>{statuses[run.status] ?? '协作状态'}</h2><p>已完成 {run.result.completed_chapters ?? 0} / {run.result.total_chapters ?? 0} 章 · 优化稿由你确认后写入正文</p></div>
        <JobStatus job={run} sourceUnavailable={sourceUnavailable || busy}
          onCancel={!busy ? cancel : undefined}
          onResume={!needsReview && !busy ? confirmUnknown => act(() => resume(confirmUnknown)) : undefined} />
      </div>
      {['failed', 'recovery_required'].includes(run.status) && !!Object.keys(run.effects?.accepted_chapters ?? {}).length &&
        <p className="subtle" role="status">本任务已有章节采纳，请对剩余章节新建质量任务。</p>}
      {needsReview && <div className="quality-notice" role="status"><strong>本章需要你判断，写作会话已暂停</strong><p>当前稿件未达到质量目标或存在内容问题。查看检测结果后，可以认可此稿继续，也可以取消协作后调整。</p>
        <button type="button" disabled={busy || sourceUnavailable} onClick={() => void act(async () => {
          let approved: QualityRun;
          try { approved = await api.approve(run.id, needsReview.chapter_id, run.control_revision!); }
          catch (cause) { await refresh(); throw cause; }
          rememberRun(approved); await resume(false, approved);
        })}>认可此稿并继续</button></div>}
      <div className="quality-conversations">{(['writer', 'optimizer'] as const).map(role => <section key={role} className="quality-conversation" aria-label={role === 'writer' ? '写作会话' : '优化会话'}>
        <header><h3>{role === 'writer' ? '写作会话' : '优化会话'}</h3><span>{active(run) && run.result.active_role === role ? '正在处理' : active(run) ? '等待交接' : '交接记录'}</span></header>
        {(run.result.messages ?? []).filter(message => message.role === role).map((message, index) => <article key={`${message.chapter_id}:${index}`}><span>{chapters.find(chapter => chapter.id === message.chapter_id)?.title ?? '章节交接'}</span><p>{message.content}</p></article>)}
        {!(run.result.messages ?? []).some(message => message.role === role) && <p className="subtle">{role === 'writer' ? '交稿后将在这里保留写作交接。' : '收到正文后检查、修订，并反馈下一章建议。'}</p>}
      </section>)}</div>
      {(run.result.chapters ?? []).map((chapter, index) => {
        const accepted = !!run.effects?.accepted_chapters?.[chapter.chapter_id];
        const partial = !!run.effects?.accepted_selections?.[chapter.chapter_id];
        const awaitingPrevious = run.result.chapters.slice(0, index).some(prior => !run.effects?.accepted_chapters?.[prior.chapter_id]);
        const canAccept = ['succeeded', 'failed', 'cancelled'].includes(run.status);
        return <article className="quality-chapter-result" key={chapter.chapter_id}>
        <header><div><span className="eyebrow">{({ writing: '正在写作', reviewing: '正在检测', rewriting: '正在优化', ready: '优化完成', needs_review: '等待确认' })[chapter.status]}</span><h3>{chapter.title}</h3></div>
          {onOpenChapter && <button type="button" disabled={busy} onClick={() => onOpenChapter(chapter.chapter_id)}>打开章节</button>}</header>
        {(chapter.before || chapter.after) && <><table aria-label={`${chapter.title}质量评分`} className="quality-scores"><thead><tr><th>检测维度</th><th>优化前</th><th>优化后</th></tr></thead><tbody>
          {(Object.keys(dimensions) as QualityDimension[]).map(dimension => <tr key={dimension}><th scope="row">{dimensions[dimension]}</th><td>{chapter.before?.scores[dimension] ?? '—'}</td><td>{chapter.after?.scores[dimension] ?? '—'}</td></tr>)}</tbody></table>
           <p className="quality-review-summary">{chapter.after?.summary ?? chapter.before?.summary}</p></>}
         {partial && <p role="status" className="subtle">已局部采纳 · 组合稿尚未整体复核；上述评分和建议仍对应完整优化候选。</p>}
        {(chapter.after ?? chapter.before)?.issues?.length ? <details className="quality-findings"><summary>查看检测建议（{(chapter.after ?? chapter.before)!.issues.length}）</summary>
          <ul>{(chapter.after ?? chapter.before)!.issues.map((issue, index) => <li key={index}><strong>{dimensions[issue.dimension] ?? issue.dimension} · {({ note: '建议', warning: '需留意', error: '需处理' })[issue.severity]}</strong>
            {issue.quote && <blockquote>{issue.quote}</blockquote>}<p>{issue.reason}</p><p>{issue.suggestion}</p></li>)}</ul></details> : null}
        {chapter.draft && <details className="quality-prose"><summary>查看交接原稿</summary><p>{chapter.draft}</p></details>}
         {chapter.candidate_text && <details className="quality-prose"><summary>查看优化后的正文</summary><p>{chapter.candidate_text}</p></details>}
         {!!chapter.candidate_text && ['ready', 'needs_review'].includes(chapter.status) && <QualityDiffPanel key={`${run.id}:${chapter.chapter_id}`}
           projectId={projectId} runId={run.id} chapterId={chapter.chapter_id} allowPartial={run.result.mode === 'polish'}
           disabled={busy || sourceUnavailable || !canAccept || awaitingPrevious || accepted} onAccept={ids => accept(chapter, ids)} />}
        {chapter.handoff?.next_guidance && <p className="quality-next-guidance"><strong>给下一章的建议</strong>{chapter.handoff.next_guidance}</p>}
        {!!chapter.candidate_text && ['ready', 'needs_review'].includes(chapter.status) && <footer className="quality-candidate-actions"><span>{accepted ? '已写入正文并保存新版本'
          : !canAccept ? '流程结束或取消后可以采纳' : awaitingPrevious ? '请先采纳前一章的优化稿' : '候选正文 · 采纳时检查源版本'}</span>
          <button type="button" className="primary-action" disabled={busy || sourceUnavailable || !canAccept || awaitingPrevious || accepted}
            aria-label={accepted ? `${chapter.title}已采纳` : `采纳${chapter.title}的优化稿`} onClick={() => void accept(chapter)}>
            {accepted ? '已采纳' : '采纳优化稿'}</button></footer>}
      </article>; })}
    </div>}
  </section>;
}
