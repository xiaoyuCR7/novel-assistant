import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { apiError } from '../../lib/api';
import { DiagnosticError } from '../../components/DiagnosticError';
import './writing-goals.css';

interface Goals { revision: number; target_words: number; daily_goal: number; weekly_chapters: number; deadline: string | null }
interface GoalReport {
  goals: Goals;
  progress: { saved_words: number; saved_chapters: number; draft_chapters: number;
    pending_review_candidates: number; author_completed_chapters: number; chapter_count: number };
  week: { timezone: string; start_date: string; end_date: string; today: string; completed_chapters: number | null };
}
interface Props { projectId: string; onSaved?: () => void | Promise<void> }
async function request(projectId: string, init?: RequestInit): Promise<GoalReport> {
  const response = await fetch(`/api/v1/projects/${encodeURIComponent(projectId)}/writing-goals`, init);
  if (!response.ok) throw await apiError(response);
  const result = await response.json() as GoalReport;
  if (!result?.goals || !result.progress || !result.week) throw Error('目标和进度返回格式无效，请刷新重试。');
  return result;
}

export function WritingGoals(props: Props) { return <ProjectGoals key={props.projectId} {...props} />; }

function ProjectGoals({ projectId, onSaved }: Props) {
  const cache = useQueryClient();
  const key = ['writing-goals', projectId];
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => request(projectId, { signal }) });
  const report = query.data;
  const [refreshError, setRefreshError] = useState<unknown>(null);
  const percent = report ? Math.min(100, Math.round(report.progress.saved_words / Math.max(1, report.goals.target_words) * 100)) : 0;
  return <section className="writing-goals" aria-label="创作与交稿目标">
    <header><div><h2>创作与交稿目标</h2><p>让目标明确，也让每一步进度有据可查。</p></div>
      <button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>刷新进度</button></header>
    {query.isPending && <p role="status">正在读取创作目标…</p>}
    {query.error && <DiagnosticError error={query.error} />}
    {refreshError != null && <DiagnosticError error={refreshError} message="目标已保存，其他进度刷新失败，请稍后刷新。" />}
    {report && <>
      <dl className="writing-goal-counts">
        <div><dt>已保存正文</dt><dd aria-label="已保存正文字数">{report.progress.saved_words.toLocaleString('en-US')} 字</dd>
          <small>{report.progress.saved_chapters} 章有正文，其中 {report.progress.draft_chapters} 章未完成。</small></div>
        <div><dt>待审候选</dt><dd aria-label="待审候选数量">{report.progress.pending_review_candidates} 份</dd>
          <small>待采纳的写作或优化稿，同一章可有多份。</small></div>
        <div><dt>作者已完成</dt><dd aria-label="作者已完成章节数">{report.progress.author_completed_chapters} / {report.progress.chapter_count} 章</dd>
          <small>依据章节完成状态，模型评分不代表交稿。</small></div>
      </dl>
      <div className="writing-goal-progress"><span>工作稿字数目标 · {percent}%</span>
        <progress max={100} value={percent} aria-label="已保存正文字数目标进度" />
        <small>按已保存的工作副本统计，忽略空白字符；不包含未保存编辑和未采纳候选，不代表完成交稿。</small></div>
      <div className="writing-goal-week"><h3>本周目标：{report.goals.weekly_chapters ? `${report.goals.weekly_chapters} 章` : '未设置'}</h3>
        <p>{report.week.start_date} 至 {report.week.end_date} · 周一至周日 · {report.week.timezone}</p>
        <p>{report.week.completed_chapters === null ? '本周完成数：暂无可靠历史' : `本周已完成 ${report.week.completed_chapters} 章`}</p>
        {report.week.completed_chapters === null && <small>现有章节没有可靠的完成时间，不能将全书已完成章数当作本周成果。</small>}
        <p>{report.goals.deadline ? `截止日期：${report.goals.deadline}${report.goals.deadline < report.week.today ? ' · 已过截止日' : report.goals.deadline === report.week.today ? ' · 今日截止' : ''}` : '尚未设置截止日期'}</p>
      </div>
      <GoalsForm current={report.goals} onSave={async goals => {
        setRefreshError(null);
        try {
          const saved = await request(projectId, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(goals) });
          await cache.cancelQueries({ queryKey: key, exact: true });
          cache.setQueryData(key, saved);
          try { await onSaved?.(); } catch (error) { setRefreshError(error); }
          return saved.goals;
        } catch (error) {
          // Keep local inputs. A new revision is loaded only after the author explicitly chooses it.
          void query.refetch(); throw error;
        }
      }} />
    </>}
  </section>;
}

function GoalsForm({ current, onSave }: { current: Goals; onSave: (goals: Goals) => Promise<Goals> }) {
  const [baseline, setBaseline] = useState(current);
  const [target, setTarget] = useState(String(current.target_words)), [daily, setDaily] = useState(String(current.daily_goal));
  const [weekly, setWeekly] = useState(String(current.weekly_chapters)), [deadline, setDeadline] = useState(current.deadline ?? '');
  const [busy, setBusy] = useState(false), [saved, setSaved] = useState(false), [error, setError] = useState<unknown>(null);
  const pending = useRef(false);
  const changed = () => setSaved(false);
  const integer = (value: string, min: number, max: number) => {
    const number = Number(value);
    if (!value.trim() || !Number.isSafeInteger(number) || number < min || number > max) throw Error('目标应为允许范围内的整数，请检查输入。');
    return number;
  };
  return <form className="writing-goal-form" onSubmit={event => {
    event.preventDefault(); if (pending.current) return;
    pending.current = true; setBusy(true); setSaved(false); setError(null);
    void (async () => {
      try {
        const next = await onSave({ revision: baseline.revision, target_words: integer(target, 1, 10000000),
          daily_goal: integer(daily, 0, 1000000), weekly_chapters: integer(weekly, 0, 1000), deadline: deadline || null });
        setBaseline(next); setSaved(true);
      } catch (caught) { setError(caught); }
      finally { pending.current = false; setBusy(false); }
    })();
  }}>
    <fieldset disabled={busy}><legend>调整创作目标</legend><div className="writing-goal-fields">
      <label>全书目标字数<input type="number" min={1} max={10000000} step={1} required value={target} onChange={event => { setTarget(event.target.value); changed(); }} /></label>
      <label>每日目标字数<input type="number" min={0} max={1000000} step={1} required value={daily} onChange={event => { setDaily(event.target.value); changed(); }} /></label>
      <label>每周目标章节数<input type="number" min={0} max={1000} step={1} required value={weekly} onChange={event => { setWeekly(event.target.value); changed(); }} /></label>
      <label>截止日期（可选）<input type="date" value={deadline} onChange={event => { setDeadline(event.target.value); changed(); }} /></label>
    </div><p>每日和每周目标填 0 表示暂不设置；截止日期留空即可取消。修改目标不会启动生成或改变章节状态。</p>
      <div className="writing-goal-actions"><button type="submit" className="primary" disabled={busy}>{busy ? '正在保存…' : '保存创作目标'}</button>
        {current.revision !== baseline.revision && <button type="button" onClick={() => {
          setBaseline(current); setTarget(String(current.target_words)); setDaily(String(current.daily_goal));
          setWeekly(String(current.weekly_chapters)); setDeadline(current.deadline ?? ''); setError(null); setSaved(false);
        }}>放弃当前输入并载入最新目标</button>}</div>
    </fieldset>
    {current.revision !== baseline.revision && <p role="status">已读取其他页面保存的新目标，当前输入仍保留。</p>}
    {saved && <p role="status">创作目标已保存。</p>}
    {error != null && <DiagnosticError error={error} />}
  </form>;
}
